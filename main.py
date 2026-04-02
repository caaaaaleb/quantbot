"""
QuantBot 主程序 - 支持多交易对 + WebSocket实时价格
"""

import asyncio
import os
import time
from pathlib import Path
from typing import Optional, Dict

import ccxt
import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import JSONResponse
import uvicorn

from src.data.kline import KlineFetcher
from src.data.websocket import MultiWebSocket
from src.strategy.multi_factor import MultiFactorStrategy
from src.risk.risk_manager import RiskManager
from src.execution.trader import Trader
from src.backtest.backtest import Backtester
from src.utils.logger import setup_logger, logger

# ═══════════════════════════════════════════════════════════════
# 1. 加载配置
# ═══════════════════════════════════════════════════════════════

load_dotenv()

CONFIG_PATH = Path(__file__).parent / "config" / "config.yaml"

def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

cfg = load_config()

# 日志初始化
setup_logger(
    log_file=cfg["logging"]["file"],
    level=cfg["logging"]["level"]
)

logger.info("=" * 60)
logger.info("🚀 QuantBot 启动中...")
logger.info("=" * 60)

# ═══════════════════════════════════════════════════════════════
# 2. 初始化交易所 & 全局组件
# ═══════════════════════════════════════════════════════════════

def create_exchange() -> ccxt.Exchange:
    api_key = os.getenv("BINANCE_API_KEY", "")
    secret_key = os.getenv("BINANCE_SECRET_KEY", "")

    exchange = ccxt.binance({
        "apiKey": api_key,
        "secret": secret_key,
        "enableRateLimit": True,
        "options": {"defaultType": "spot"},
    })

    if cfg["trading"].get("testnet", False):
        exchange.set_sandbox_mode(True)
        logger.warning("⚠️ 已切换到 Binance 测试网")

    return exchange

# 多交易对配置
SYMBOLS = cfg["trading"]["symbols"]  # ["BTC/USDT", "ETH/USDT", ...]
TIMEFRAME = cfg["trading"]["timeframe"]

exchange = create_exchange()

# 为每个交易对创建独立组件
kline_fetchers: Dict[str, KlineFetcher] = {}
strategies: Dict[str, MultiFactorStrategy] = {}
traders: Dict[str, Trader] = {}

for symbol in SYMBOLS:
    kline_fetchers[symbol] = KlineFetcher(exchange, symbol)
    strategies[symbol] = MultiFactorStrategy(
        ma_short=cfg["strategy"]["ma_short"],
        ma_long=cfg["strategy"]["ma_long"],
        weights=cfg["strategy"]["weights"],
    )
    traders[symbol] = Trader(exchange=exchange, max_retries=3, retry_delay=1.0)

# 共享风控管理器
risk_manager = RiskManager(
    max_position_size=cfg["risk"]["max_position_size"],
    stop_loss=cfg["risk"]["stop_loss"],
    take_profit=cfg["risk"]["take_profit"],
    max_daily_loss=cfg["risk"]["max_daily_loss"],
    max_trades_per_day=cfg["risk"]["max_trades_per_day"],
    max_positions=cfg["risk"].get("max_positions", 3),
)

# 模拟模式检测
if not os.getenv("BINANCE_API_KEY") or "your_api_key" in os.getenv("BINANCE_API_KEY", ""):
    for trader in traders.values():
        trader.set_dry_run(True)
    logger.warning("🧪 未检测到真实 API Key，已自动开启模拟交易模式")

# 多交易对 WebSocket
ws_client: Optional[MultiWebSocket] = None
if cfg["websocket"]["enabled"]:
    ws_client = MultiWebSocket(SYMBOLS)

# 回测引擎
backtester = Backtester(
    initial_capital=cfg["backtest"]["initial_capital"],
    commission=cfg["backtest"]["commission"],
)

# ═══════════════════════════════════════════════════════════════
# 3. 核心策略循环（支持多交易对）
# ═══════════════════════════════════════════════════════════════

async def run_strategy_once():
    """执行一次完整策略流程（多交易对）"""
    logger.info("=" * 60)
    logger.info("🔄 策略执行开始")

    for symbol in SYMBOLS:
        await process_symbol(symbol)

    # 输出统计
    stats = risk_manager.get_stats()
    logger.info(
        f"📊 统计 | 总交易={stats['total_trades']} | "
        f"胜率={stats['win_rate']} | 总盈亏={stats['total_pnl']:+.4f} USDT"
    )
    logger.info("=" * 60)


async def process_symbol(symbol: str):
    """处理单个交易对"""
    try:
        # 1. 获取K线数据
        df = kline_fetchers[symbol].fetch_klines(
            timeframe=TIMEFRAME,
            limit=cfg["data"]["kline_limit"],
        )

        # 2. 获取实时价格（优先WS，回退REST）
        current_price = 0.0
        if ws_client:
            current_price = ws_client.get_price(symbol)
        if current_price == 0.0:
            current_price = kline_fetchers[symbol].get_latest_price()

        if current_price == 0.0:
            logger.warning(f"{symbol} 无法获取价格，跳过")
            return

        # 3. 止损/止盈检查
        sl_tp = risk_manager.check_stop_loss_take_profit(symbol, current_price)
        if sl_tp in ("STOP_LOSS", "TAKE_PROFIT"):
            position = risk_manager.get_position(symbol)
            if position:
                label = "止损" if sl_tp == "STOP_LOSS" else "止盈"
                logger.info(f"{symbol} 触发{label}，执行平仓")
                order = traders[symbol].market_sell(symbol, position.quantity, current_price)
                if order["status"] != "failed":
                    risk_manager.close_position(symbol, current_price)
            return

        # 4. 生成策略信号
        signal_result = strategies[symbol].generate_signal(df)
        signal = signal_result["signal"]
        strength = signal_result["strength"]

        logger.info(
            f"{symbol} 信号: {signal} | 强度={strength:.3f} | 价格={current_price:.2f}"
        )

        # 5. 风控检查
        balance_info = traders[symbol].get_balance("USDT")
        free_balance = balance_info["free"]

        check = risk_manager.check_trade_allowed(
            symbol=symbol,
            balance=free_balance,
            price=current_price,
            signal=signal,
        )

        if not check["allowed"]:
            logger.debug(f"{symbol} 风控拦截: {check['reason']}")
            return

        # 6. 执行交易
        quantity = check["size"]

        if signal == "BUY":
            order = traders[symbol].market_buy(symbol, quantity, current_price)
            if order["status"] != "failed":
                risk_manager.open_position(
                    symbol=symbol,
                    side="long",
                    entry_price=order.get("price") or current_price,
                    quantity=order.get("amount") or quantity,
                )

        elif signal == "SELL":
            position = risk_manager.get_position(symbol)
            if position and position.side == "long":
                order = traders[symbol].market_sell(symbol, position.quantity, current_price)
                if order["status"] != "failed":
                    risk_manager.close_position(symbol, current_price)
            else:
                order = traders[symbol].market_sell(symbol, quantity, current_price)
                if order["status"] != "failed":
                    risk_manager.open_position(
                        symbol=symbol,
                        side="short",
                        entry_price=order.get("price") or current_price,
                        quantity=order.get("amount") or quantity,
                    )

    except Exception as e:
        logger.error(f"{symbol} 策略执行异常: {e}", exc_info=True)


async def strategy_loop():
    """策略主循环"""
    interval = cfg["strategy"]["interval"]
    logger.info(f"🚀 策略循环启动，执行间隔: {interval}s | 交易对: {SYMBOLS}")

    while True:
        await run_strategy_once()
        logger.info(f"⏳ 等待 {interval}s 后执行下一轮...")
        await asyncio.sleep(interval)


# ═══════════════════════════════════════════════════════════════
# 4. WebSocket 监听
# ═══════════════════════════════════════════════════════════════

async def ws_loop():
    """WebSocket 实时价格监听"""
    if not ws_client:
        logger.info("WebSocket 已禁用")
        return

    logger.info("📡 MultiWebSocket 启动...")
    await ws_client.connect()
    await ws_client.listen()


# ═══════════════════════════════════════════════════════════════
# 5. FastAPI 应用
# ═══════════════════════════════════════════════════════════════

app = FastAPI(
    title="QuantBot API",
    description="多交易对自动量化交易系统",
    version="1.1.0",
)


@app.get("/", summary="健康检查")
async def root():
    return {
        "status": "ok",
        "system": "QuantBot",
        "version": "1.1.0",
        "symbols": SYMBOLS
    }


@app.get("/prices", summary="获取所有交易对实时价格")
async def get_all_prices():
    """获取所有交易对最新价格"""
    if ws_client:
        return {"prices": ws_client.get_all_prices()}
    return {"error": "WebSocket 未启用"}


@app.get("/price/{symbol}", summary="获取指定交易对价格")
async def get_price(symbol: str):
    """获取指定交易对价格"""
    sym = symbol.upper()
    price = 0.0
    if ws_client:
        price = ws_client.get_price(sym)
    if price == 0.0:
        price = kline_fetchers.get(sym, kline_fetchers.get("BTC/USDT")).get_latest_price()
    return {"symbol": sym, "price": price}


@app.get("/signal/{symbol}", summary="获取指定交易对策略信号")
async def get_signal(symbol: str):
    """获取指定交易对当前策略信号"""
    sym = symbol.upper()
    if sym not in strategies:
        return JSONResponse(status_code=404, content={"error": f"未配置 {sym}"})

    try:
        df = kline_fetchers[sym].fetch_klines(timeframe=TIMEFRAME, limit=100)
        result = strategies[sym].generate_signal(df)
        return result
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/signals", summary="获取所有交易对信号")
async def get_all_signals():
    """一次性获取所有交易对的策略信号"""
    results = {}
    for symbol in SYMBOLS:
        try:
            df = kline_fetchers[symbol].fetch_klines(timeframe=TIMEFRAME, limit=100)
            result = strategies[symbol].generate_signal(df)
            results[symbol] = {
                "signal": result["signal"],
                "strength": round(result["strength"], 3),
                "price": result["price"]
            }
        except Exception as e:
            results[symbol] = {"error": str(e)}
    return {"signals": results}


@app.get("/positions", summary="查看所有持仓")
async def get_positions():
    """获取所有持仓"""
    positions = risk_manager.get_all_positions()
    result = {}
    for sym, pos in positions.items():
        current_price = ws_client.get_price(sym) if ws_client else 0
        if current_price == 0:
            current_price = kline_fetchers.get(sym).get_latest_price()
        result[sym] = {
            "side": pos.side,
            "entry_price": pos.entry_price,
            "quantity": pos.quantity,
            "entry_time": pos.entry_time,
            "stop_loss": pos.stop_loss,
            "take_profit": pos.take_profit,
            "unrealized_pnl": pos.unrealized_pnl(current_price),
            "pnl_pct": f"{pos.pnl_pct(current_price)*100:.2f}%",
        }
    return {"positions": result, "count": len(result)}


@app.get("/balance", summary="查看账户余额")
async def get_balance():
    try:
        return traders["BTC/USDT"].get_balance("USDT")
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/stats", summary="查看交易统计")
async def get_stats():
    return risk_manager.get_stats()


@app.get("/backtest", summary="快速回测")
async def run_backtest(symbol: str = "BTC/USDT"):
    """运行快速回测（最近30天数据）"""
    sym = symbol.upper()
    if sym not in kline_fetchers:
        return JSONResponse(status_code=404, content={"error": f"未配置 {sym}"})

    try:
        # 获取历史数据（模拟）
        df = kline_fetchers[sym].fetch_klines(timeframe="1h", limit=500)
        
        result = backtester.run(df, strategies[sym], sym)
        report = backtester.generate_report(result)
        
        return report
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/run", summary="手动触发一次策略")
async def manual_run(background_tasks: BackgroundTasks):
    background_tasks.add_task(run_strategy_once)
    return {"message": "策略已触发，后台执行中..."}


@app.on_event("startup")
async def startup_event():
    logger.info("🟢 QuantBot 启动中...")
    asyncio.create_task(ws_loop())
    asyncio.create_task(strategy_loop())
    logger.info("✅ 所有后台任务已启动")


@app.on_event("shutdown")
async def shutdown_event():
    logger.info("🔴 QuantBot 正在关闭...")
    if ws_client:
        await ws_client.close()
    logger.info("✅ 资源已释放")


# ═══════════════════════════════════════════════════════════════
# 6. 入口
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info",
    )