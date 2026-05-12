"""
QuantBot - 多交易对自动量化交易系统
FastAPI + Dashboard + Scanner + 多策略引擎
"""

import traceback
import asyncio
import os
import time
from pathlib import Path
from typing import Optional, Dict, List
from contextlib import asynccontextmanager

import ccxt
import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from src.data.kline import KlineFetcher
from src.data.indicators import calculate_atr
from src.data.websocket import MultiWebSocket
from src.strategy.multi_factor import MultiFactorStrategy
from src.strategy.strategy_router import StrategyRouter
from src.risk.risk_manager import RiskManager
from src.risk.market_filter import MarketFilter
from src.risk.audit_logger import AuditLogger, TradeContext
from src.execution.trader import Trader
from src.account.account_manager import AccountManager
from src.scanner.scanner_service import ScannerService, ScannerConfig
from src.backtest.backtest import Backtester
from src.strategy.base import Signal
from src.bridge.signal_bridge import SignalBridge
from src.utils.logger import setup_logger, logger

load_dotenv()
cfg = yaml.safe_load(open("config/config.yaml", encoding="utf-8"))

setup_logger(
    log_file=cfg["logging"]["file"],
    level=cfg["logging"]["level"],
)

# ────────────────────────────────────────────────────────────────
# 全局状态（lifespan 中初始化）
# ────────────────────────────────────────────────────────────────

exchange: Optional[ccxt.Exchange] = None
DRY_RUN: bool = True
account_manager: Optional[AccountManager] = None
scanner: Optional[ScannerService] = None
scanner_enabled: bool = False
auto_add: bool = False
_scan_interval: int = 3600
SYMBOLS: List[str] = []
TIMEFRAME: str = "1m"
kline_fetchers: Dict[str, KlineFetcher] = {}
strategies: Dict[str, MultiFactorStrategy] = {}
routers: Dict[str, StrategyRouter] = {}
traders: Dict[str, Trader] = {}
risk_manager: Optional[RiskManager] = None
market_filter: Optional[MarketFilter] = None
audit_logger: Optional[AuditLogger] = None
signal_bridge: Optional[SignalBridge] = None
ws_client: Optional[MultiWebSocket] = None
backtester: Optional[Backtester] = None
_last_scan_time: float = 0.0
_scanner_symbols: List[str] = []
_strategy_task: Optional[asyncio.Task] = None


def _sym(symbol: str) -> str:
    """标准化交易对格式：UB/USDT:USDT -> UB/USDT"""
    return symbol.split(":")[0] if ":" in symbol else symbol
_ws_task: Optional[asyncio.Task] = None
_scanner_task: Optional[asyncio.Task] = None


# ────────────────────────────────────────────────────────────────
# Lifespan（启动/关闭）
# ────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global exchange, DRY_RUN, account_manager, scanner
    global scanner_enabled, auto_add, _scan_interval
    global SYMBOLS, TIMEFRAME
    global kline_fetchers, strategies, routers, traders
    global risk_manager, market_filter, audit_logger, signal_bridge, ws_client, backtester
    global _last_scan_time, _scanner_symbols
    global _strategy_task, _ws_task, _scanner_task, _scanner_cache

    logger.info("=" * 60)
    logger.info("🟢 QuantBot 启动中...")

    # 1. 交易所
    api_key = os.getenv("BITGET_API_KEY", "")
    secret_key = os.getenv("BITGET_SECRET_KEY", "")
    password = os.getenv("BITGET_PASSWORD", "")

    # Dry Run 模式检查
    DRY_RUN = cfg["trading"].get("dry_run", False)
    env_dry_run = os.getenv("DRY_RUN", "").lower()
    if env_dry_run in ("true", "1", "yes"):
        DRY_RUN = True
    elif env_dry_run in ("false", "0", "no"):
        DRY_RUN = False
    if not api_key or "your_api_key" in api_key:
        DRY_RUN = True

    # 代理配置（仅当 HTTP_PROXY 环境变量存在时才启用）
    proxy_url = os.getenv("HTTP_PROXY", "")
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None

    exchange_kwargs = {
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},
    }
    if proxies:
        exchange_kwargs["proxies"] = proxies

    if DRY_RUN:
        exchange = ccxt.bitget(exchange_kwargs)
        logger.info("🟡 Dry Run 模式：使用公开接口（无 API 签名）")
    else:
        exchange_kwargs.update({"apiKey": api_key, "secret": secret_key, "password": password})
        exchange = ccxt.bitget(exchange_kwargs)
        exchange.load_markets()

    if cfg["trading"].get("testnet", False):
        exchange.set_sandbox_mode(True)
        logger.warning("⚠️ 已切换到 Bitget 测试网")

    logger.warning(f"🧪 模式: {'模拟盘(dry_run)' if DRY_RUN else '实盘'}")

    # 3. 账户管理器
    account_manager = AccountManager(exchange, dry_run=DRY_RUN)

    # 4. Scanner
    scanner_enabled = cfg.get("scanner", {}).get("enabled", True)
    auto_add = cfg.get("scanner", {}).get("auto_add", False)
    _scan_interval = cfg.get("scanner", {}).get("scan_interval", 3600)

    if scanner_enabled:
        sc_cfg = cfg.get("scanner", {})
        cmc_cfg = cfg.get("coinmarketcap", {})
        config = ScannerConfig(
            # 信号引擎阈值（PRD 标准值）
            momentum_1m_threshold=sc_cfg.get("momentum_1m", 0.005),
            momentum_5m_threshold=sc_cfg.get("momentum_5m", 0.02),
            volume_spike_threshold=sc_cfg.get("volume_spike", 2.0),
            taker_buy_threshold=sc_cfg.get("taker_buy_ratio", 0.6),
            breakout_threshold=sc_cfg.get("breakout_threshold", 1.01),
            # 打分权重
            w_momentum_5m=sc_cfg.get("w_momentum_5m", 0.30),
            w_momentum_1m=sc_cfg.get("w_momentum_1m", 0.20),
            w_volume_spike=sc_cfg.get("w_volume_spike", 0.20),
            w_taker_buy=sc_cfg.get("w_taker_buy", 0.15),
            w_orderbook=sc_cfg.get("w_orderbook", 0.15),
            # 过滤
            min_volume_24h=sc_cfg.get("min_volume", 1_000_000),
            max_spread=sc_cfg.get("max_spread", 0.005),
            max_change_24h=sc_cfg.get("max_change_24h", 80.0),
            max_atr_pct=sc_cfg.get("max_atr_pct", 0.10),
            # 排名
            top_n=sc_cfg.get("top_n", 20),
        )
        scanner = ScannerService(exchange, config=config)
        logger.info(f"🔍 Scanner V2 启用 | auto_add={auto_add}")

    # 5. 确定交易对
    TIMEFRAME = cfg["trading"]["timeframe"]
    SYMBOLS = cfg["trading"]["symbols"]

    logger.info(f"📋 交易对: {SYMBOLS}")

    # 6. 初始化组件
    for symbol in SYMBOLS:
        kline_fetchers[symbol] = KlineFetcher(exchange, symbol)
        strategies[symbol] = MultiFactorStrategy(
            ma_short=cfg["strategy"]["ma"]["short_period"],
            ma_long=cfg["strategy"]["ma"]["long_period"],
        )
        traders[symbol] = Trader(exchange=exchange, max_retries=3, retry_delay=1.0)
        traders[symbol].set_dry_run(DRY_RUN)
        routers[symbol] = StrategyRouter(
            enable_regime_detection=True,
            regime_weights=cfg["strategy"]["regime_weights"]
        )

    risk_manager = RiskManager(
        exchange=exchange,
        max_position_size=cfg["risk"]["max_position_size"],
        stop_loss=cfg["risk"]["stop_loss"],
        take_profit=cfg["risk"]["take_profit"],
        tp_min_profit=cfg["risk"].get("tp_min_profit", 0.0),
        max_daily_loss=cfg["risk"]["max_daily_loss"],
        max_trades_per_day=cfg["risk"]["max_trades_per_day"],
        max_positions=cfg["risk"].get("max_positions", 3),
        atr_stop_loss_enabled=cfg["risk"].get("atr_stop_loss_enabled", True),
        atr_multiplier=cfg["risk"].get("atr_multiplier", 1.5),
        atr_tp_multiplier=cfg["risk"].get("atr_tp_multiplier", 3.0),
        atr_tsl_multiplier=cfg["risk"].get("atr_tsl_multiplier", 1.5),
        vol_adj_enabled=cfg["risk"].get("vol_adj_enabled", True),
        consecutive_loss_limit=cfg["risk"].get("consecutive_loss_limit", 3),
        consecutive_loss_reduction=cfg["risk"].get("consecutive_loss_reduction", 0.5),
        tp1_pct=cfg["risk"].get("tp1_pct", 0.02),
        tp1_portion=cfg["risk"].get("tp1_portion", 0.30),
        tp2_pct=cfg["risk"].get("tp2_pct", 0.05),
        tp2_portion=cfg["risk"].get("tp2_portion", 0.50),
        enable_trailing=cfg["risk"].get("enable_trailing", True),
        time_exit_enabled=cfg["risk"].get("time_exit_enabled", True),
        time_exit_minutes=cfg["risk"].get("time_exit_minutes", 20),
        time_exit_min_profit=cfg["risk"].get("time_exit_min_profit", 0.015),
        leverage=cfg["risk"].get("leverage", 1),
        atr_period=cfg["risk"].get("atr_period", 14),
        vol_threshold=cfg["risk"].get("vol_threshold", 0.03),
        atr_pause_threshold=cfg["risk"].get("atr_pause_threshold", 0.12),
        atr_max_pct=cfg["risk"].get("atr_max_pct", 0.25),
    )

    backtester = Backtester(
        initial_capital=cfg["backtest"]["initial_capital"],
        commission=cfg["backtest"]["commission"],
    )

    market_filter = MarketFilter(
        exchange=exchange,
        news_pause_before_minutes=cfg["risk"].get("news_pause_before_minutes", 30),
        news_pause_after_minutes=cfg["risk"].get("news_pause_after_minutes", 30),
        atr_max_pct=cfg["risk"].get("atr_max_pct", 0.25),
        atr_pause_threshold=cfg["risk"].get("atr_pause_threshold", 0.12),
    )

    audit_logger = AuditLogger(log_dir="logs/audit")

    bridge_cfg = cfg.get("bridge", {})
    signal_bridge = SignalBridge(
        ai_trader_url=bridge_cfg.get("ai_trader_url", "http://localhost:3000"),
        api_key=bridge_cfg.get("api_key", ""),
        publish_signals=bridge_cfg.get("publish_signals", True),
        consume_signals=bridge_cfg.get("consume_signals", True),
        external_weight=bridge_cfg.get("external_weight", 0.15),
    )
    if not bridge_cfg.get("enabled", False):
        signal_bridge.enabled = False
        logger.info("SignalBridge: 未启用（bridge.enabled=false）")

    if cfg["websocket"]["enabled"]:
        ws_client = MultiWebSocket(SYMBOLS)

    logger.info("✅ 组件初始化完成，HTTP 服务就绪")

    # 启动后台任务（延迟启动，避免阻塞事件循环）
    loop = asyncio.get_running_loop()

    async def delayed_ws():
        await asyncio.sleep(3.0)
        await ws_loop()

    async def delayed_strategy():
        await asyncio.sleep(2.0)
        await strategy_loop()

    if ws_client:
        _ws_task = asyncio.create_task(delayed_ws())
    else:
        _ws_task = None

    if auto_add and scanner:
        _scanner_task = asyncio.create_task(scanner_loop())
        # 启动时立即运行一次扫描（后台，不阻塞）
        async def initial_scan():
            await asyncio.sleep(2.0)  # 等待系统就绪
            try:
                logger.info("🔍 启动预热扫描...")
                loop = asyncio.get_running_loop()
                # 在线程池中运行（避免阻塞事件循环）
                candidates = await loop.run_in_executor(
                    None, lambda: scanner.scan(limit=cfg.get("scanner", {}).get("top_n", 20))
                )
                global _scanner_cache, SYMBOLS
                _scanner_cache = {"results": candidates, "timestamp": time.time()}
                blacklist = set(cfg["trading"].get("blacklist", []))
                new_symbols = [c.symbol for c in candidates if c.symbol not in blacklist]
                if new_symbols:
                    logger.info(f"🔄 Scanner 预热完成: {len(new_symbols)} 个币 → {new_symbols}")
                    SYMBOLS = new_symbols
                else:
                    logger.info("🔄 Scanner 预热完成，无候选币")
            except Exception as e:
                logger.error(f"Scanner 预热失败: {e}")
        # 在线程池中运行（避免阻塞事件循环）
        asyncio.create_task(initial_scan())
    else:
        _scanner_task = None

    _strategy_task = asyncio.create_task(delayed_strategy())

    yield  # ← HTTP 服务在这里运行

    # 关闭
    logger.info("🔴 QuantBot 正在关闭...")
    for name, task in [
        ("策略循环", _strategy_task),
        ("WebSocket", _ws_task),
        ("Scanner", _scanner_task),
    ]:
        if task:
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            logger.info(f"   {name} 已停止")

    if ws_client:
        await ws_client.close()
    logger.info("✅ QuantBot 已关闭")


# ────────────────────────────────────────────────────────────────
# FastAPI 应用
# ────────────────────────────────────────────────────────────────

app = FastAPI(
    title="QuantBot API",
    description="多交易对自动量化交易系统",
    version="1.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if Path("templates").exists():
    app.mount("/static", StaticFiles(directory="templates"), name="static")


@app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def dashboard():
    p = Path("templates/dashboard.html")
    if not p.exists():
        return "<h1>Not found</h1>"
    html = p.read_text(encoding="utf-8")
    # 注入实际交易对
    symbols_json = str(SYMBOLS)
    html = html.replace(
        "const SYMBOLS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT'];",
        f"const SYMBOLS = {symbols_json};"
    )
    return html


@app.get("/")
async def root():
    return {
        "status": "ok",
        "system": "QuantBot",
        "version": "1.2.0",
        "dry_run": DRY_RUN,
        "symbols": SYMBOLS,
        "scanner_enabled": scanner_enabled,
        "auto_add": auto_add,
    }


@app.get("/api/prices")
async def get_all_prices():
    if ws_client:
        return {"prices": ws_client.get_all_prices()}
    prices = {}
    for sym in SYMBOLS:
        try:
            prices[sym] = kline_fetchers[sym].get_latest_price()
        except Exception:
            prices[sym] = 0
    return {"prices": prices}


@app.get("/api/price/{symbol}")
async def get_price(symbol: str):
    sym = symbol.upper().replace("-", "/")
    price = ws_client.get_price(sym) if ws_client else 0
    if price == 0:
        f = kline_fetchers.get(sym) or kline_fetchers.get("BTC/USDT")
        price = f.get_latest_price() if f else 0
    return {"symbol": sym, "price": price}


@app.get("/api/signal/{symbol}")
async def get_signal(symbol: str):
    sym = symbol.upper().replace("-", "/")
    if sym not in kline_fetchers:
        return JSONResponse(status_code=404, content={"error": f"未配置 {sym}"})
    try:
        df = kline_fetchers[sym].fetch_klines(timeframe=TIMEFRAME, limit=100)
        if sym in routers:
            return routers[sym].generate_signal(df)
        return strategies[sym].generate_signal(df)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/signals")
async def get_all_signals():
    results = {}
    for symbol in SYMBOLS:
        try:
            if symbol not in kline_fetchers:
                results[symbol] = {"error": "未初始化"}
                continue
            df = kline_fetchers[symbol].fetch_klines(timeframe=TIMEFRAME, limit=100)
            r = routers[symbol].generate_signal(df) if symbol in routers else strategies[symbol].generate_signal(df)
            signal_val = r.signal.value if hasattr(r.signal, 'value') else str(r.signal)
            results[symbol] = {
                "signal": signal_val,
                "strength": round(r.score, 3),
                "confidence": round(r.confidence, 3),
                "price": 0,
            }
        except Exception as e:
            results[symbol] = {"error": str(e)}
    return {"signals": results}


@app.get("/api/positions")
async def get_positions():
    positions = risk_manager.get_all_positions()
    result = {}
    for sym, pos_dict in positions.items():
        if not pos_dict:
            continue
        # WS key 需要大写无斜杠格式
        ws_key = sym.replace("/", "").upper()
        price = ws_client.get_price(ws_key) if ws_client else 0
        if price == 0:
            f = kline_fetchers.get(sym)
            if f is None:
                # 懒加载：scanner 自动添加的交易对可能尚未初始化
                kline_fetchers[sym] = KlineFetcher(exchange, sym)
                f = kline_fetchers[sym]
            price = f.get_latest_price() if f else 0
        # 对冲模式：同币可能有多空两个持仓，都列出来
        for side, pos in pos_dict.items():
            key = f"{sym}:{side}"
            result[key] = {
                "symbol": sym,
                "side": side,
                "entry_price": pos.entry_price,
                "quantity": pos.quantity,
                "entry_time": pos.entry_time,
                "stop_loss": pos.stop_loss,
                "take_profit": pos.take_profit,
                "unrealized_pnl": f"{pos.unrealized_pnl(price):+.4f}",
                "pnl_pct": f"{pos.pnl_pct(price)*100:+.2f}%",
            }
    return {"positions": result, "count": len(result)}


@app.get("/api/balance")
async def get_balance():
    try:
        return account_manager.get_account_summary()
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/stats")
async def get_stats():
    return risk_manager.get_stats()


@app.get("/api/backtest")
async def run_backtest(symbol: str = "BTC/USDT"):
    sym = symbol.upper().replace("-", "/")
    if sym not in kline_fetchers:
        return JSONResponse(status_code=404, content={"error": f"未配置 {sym}"})
    try:
        df = kline_fetchers[sym].fetch_klines(timeframe="1h", limit=500)
        result = backtester.run(df, strategies[sym], sym)
        return backtester.generate_report(result)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/scanner/candidates")
async def get_scanner_candidates():
    if not scanner:
        return JSONResponse(status_code=503, content={"error": "Scanner 未启用"})
    try:
        loop = asyncio.get_running_loop()
        candidates = await loop.run_in_executor(
            None, lambda: scanner.scan(limit=cfg.get("scanner", {}).get("top_n", 20))
        )
        return {
            "candidates": [
                {
                    "symbol": c.symbol,
                    "score": round(c.score, 2),
                    "stage": c.stage,
                    "momentum_1m": round(c.momentum_1m, 4),
                    "momentum_5m": round(c.momentum_5m, 4),
                    "volume_spike": round(c.volume_spike, 2),
                    "change_24h": round(c.change_24h, 2),
                    "confidence": round(c.confidence, 2),
                    "reasons": c.reasons,
                }
                for c in candidates
            ],
            "count": len(candidates),
            "auto_add": auto_add,
            "current_symbols": SYMBOLS,
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/api/scanner/scan")
async def trigger_scan():
    if not scanner:
        return JSONResponse(status_code=503, content={"error": "Scanner 未启用"})
    try:
        global _last_scan_time
        candidates = scanner.scan()
        _last_scan_time = time.time()
        return {
            "candidates": [
                {"symbol": c.symbol, "score": round(c.score, 2),
                 "change": c.change_24h, "volume": c.volume_spike,
                 "reasons": c.reasons}
                for c in candidates
            ],
            "count": len(candidates),
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/api/run")
async def manual_run(background_tasks: BackgroundTasks):
    background_tasks.add_task(run_strategy_once)
    return {"message": "策略已触发，后台执行中..."}


@app.post("/api/scanner/apply")
async def apply_candidates(symbols: List[str]):
    global SYMBOLS
    SYMBOLS = symbols
    logger.info(f"📋 手动设置交易币种: {SYMBOLS}")
    return {"symbols": SYMBOLS, "count": len(SYMBOLS)}


# ─── Dashboard 别名路由（兼容 dashboard.html 调用的路径）───

@app.get("/account/summary")
async def account_summary_alias():
    return account_manager.get_account_summary()


@app.get("/market/overview")
async def market_overview_alias():
    return {"markets": [], "timestamp": None}


@app.get("/market/tickers")
async def market_tickers_alias():
    return {"tickers": []}


@app.get("/market/klines")
async def market_klines(symbol: str = "BTC/USDT", timeframe: str = "1h", limit: int = 200):
    sym = symbol.upper().replace("-", "/")
    if sym not in kline_fetchers:
        return JSONResponse(status_code=404, content={"error": f"Symbol {sym} not found"})
    try:
        df = kline_fetchers[sym].fetch_klines(timeframe=timeframe, limit=limit)
        data = df[['datetime', 'open', 'high', 'low', 'close', 'volume']].to_dict(orient='records')
        return {"symbol": sym, "timeframe": timeframe, "klines": data}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/signals")
async def signals_alias():
    return await get_all_signals()


@app.get("/account/trades")
async def account_trades_alias():
    return {"trades": account_manager.get_my_trades(limit=20)}


@app.post("/api/scanner/trigger")
async def trigger_scanner():
    """手动触发 Scanner 立即扫描（后台运行）"""
    if not scanner:
        return JSONResponse(status_code=503, content={"error": "Scanner 未启用"})
    asyncio.create_task(_run_scanner())
    return {"message": "Scanner 已触发，后台扫描中..."}


async def _run_scanner() -> None:
    """后台运行 Scanner"""
    global _scanner_cache, SYMBOLS
    try:
        loop = asyncio.get_running_loop()
        candidates = await loop.run_in_executor(
            None, lambda: scanner.scan(limit=20)
        )
        blacklist = set(cfg["trading"].get("blacklist", []))
        _scanner_cache = {"results": candidates, "timestamp": time.time()}
        new_symbols = [c.symbol for c in candidates if c.symbol not in blacklist]
        if new_symbols:
            logger.info(f"🔄 Scanner 更新: {new_symbols}")
            SYMBOLS = new_symbols
    except Exception as e:
        logger.error(f"Scanner 扫描失败: {e}")


@app.post("/account/transfer")
async def account_transfer(body: dict):
    """划转资金（现货 <-> 合约）"""
    try:
        asset = body.get("asset", "USDT")
        amount = float(body.get("amount", 0))
        from_account = body.get("from_account", "spot")
        to_account = body.get("to_account", "usdt_futures")
        if amount <= 0:
            return JSONResponse(status_code=400, content={"success": False, "error": "金额必须大于0"})
        result = account_manager.transfer(asset, amount, from_account, to_account)
        return result
    except Exception as e:
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})


# Scanner 缓存（避免每次调 API 都全量扫描）
_scanner_cache = {"results": [], "timestamp": 0.0}
_SCANNER_CACHE_TTL = 300  # 5分钟


@app.get("/scanner/long-candidates")
async def scanner_long_alias(top_n: int = 8):
    # 返回缓存结果（后台异步更新）
    results = _scanner_cache["results"] if _scanner_cache["results"] else []
    return {"candidates": [
        {"symbol": c.symbol, "score": round(c.score, 2), "stage": c.stage,
         "momentum_1m": round(c.momentum_1m, 4), "momentum_5m": round(c.momentum_5m, 4),
         "volume_spike": round(c.volume_spike, 2), "change_24h": round(c.change_24h, 2),
         "confidence": round(c.confidence, 2), "reasons": c.reasons}
        for c in results[:top_n]
    ]}


@app.get("/scanner/short-candidates")
async def scanner_short_alias(top_n: int = 8):
    return {"candidates": []}


@app.get("/scanner/alerts")
async def scanner_alerts():
    """获取早期爆发信号（early stage + score > 0.5）"""
    if not scanner:
        return JSONResponse(status_code=503, content={"error": "Scanner 未启用"})
    alerts = scanner.get_alerts()
    return {"alerts": [
        {"symbol": c.symbol, "score": round(c.score, 2), "stage": c.stage,
         "momentum_1m": round(c.momentum_1m, 4), "momentum_5m": round(c.momentum_5m, 4),
         "volume_spike": round(c.volume_spike, 2), "change_24h": round(c.change_24h, 2),
         "confidence": round(c.confidence, 2), "reasons": c.reasons}
        for c in alerts
    ], "count": len(alerts)}


@app.get("/scanner/detail/{symbol}")
async def scanner_detail(symbol: str):
    """获取单币种详细信息"""
    if not scanner:
        return JSONResponse(status_code=503, content={"error": "Scanner 未启用"})
    detail = scanner.get_detail(symbol.upper())
    if detail is None:
        return JSONResponse(status_code=404, content={"error": f"未找到 {symbol} 的详情"})
    return detail


@app.get("/scanner/top")
async def scanner_top(top_n: int = 10):
    """Top N 排名（PRD: /scanner/top）"""
    if not scanner:
        return JSONResponse(status_code=503, content={"error": "Scanner 未启用"})
    results = scanner.get_last_results()
    top = sorted(results, key=lambda c: c.score, reverse=True)[:top_n]
    return {"coins": [
        {"rank": i+1, "symbol": c.symbol, "score": round(c.score, 2),
         "stage": c.stage, "momentum_1m": round(c.momentum_1m, 4),
         "momentum_5m": round(c.momentum_5m, 4), "volume_spike": round(c.volume_spike, 2),
         "change_24h": round(c.change_24h, 2), "confidence": round(c.confidence, 2),
         "reasons": c.reasons}
        for i, c in enumerate(top)
    ], "count": len(top)}


@app.get("/scanner/raw")
async def scanner_raw(top_n: int = 20):
    """原始评分列表（PRD: /scanner/raw）"""
    if not scanner:
        return JSONResponse(status_code=503, content={"error": "Scanner 未启用"})
    results = scanner.get_last_results()
    return {"coins": [
        {"symbol": c.symbol, "score": round(c.score, 4),
         "stage": c.stage, "confidence": round(c.confidence, 4),
         "signal": round(c.signal, 4),
         "momentum_1m": round(c.momentum_1m, 6),
         "momentum_5m": round(c.momentum_5m, 6),
         "volume_spike": round(c.volume_spike, 4),
         "change_24h": round(c.change_24h, 4),
         "cmc_rank": c.cmc_rank,
         "reasons": c.reasons}
        for c in results[:top_n]
    ], "count": len(results[:top_n])}


# ────────────────────────────────────────────────────────────────
# AI-Trader 兼容 API 端点
# ────────────────────────────────────────────────────────────────

@app.post("/api/signals/realtime")
async def receive_realtime_signal(body: dict):
    """接收外部 agent 的实时信号（AI-Trader 兼容）"""
    signal_bridge.store_external_signal(body)
    return {"status": "ok", "received": True}


@app.get("/api/signals/feed")
async def get_signal_feed(symbol: str = None, limit: int = 50):
    """获取信号 feed（AI-Trader 兼容）"""
    feed = signal_bridge.get_feed(limit=limit, symbol=symbol)
    return {"signals": feed, "count": len(feed)}


@app.post("/api/signals/follow")
async def follow_agent(body: dict):
    """订阅 agent 信号（AI-Trader 兼容）"""
    agent_id = body.get("agent_id", "")
    logger.info(f"SignalBridge: 订阅 agent {agent_id}")
    return {"status": "ok", "following": agent_id}


@app.get("/api/agents")
async def list_agents():
    """列出已注册的 agent（AI-Trader 兼容）"""
    return {
        "agents": [
            {
                "agent_id": "quantbot-1",
                "agent_name": "QuantBot",
                "status": "active",
                "strategies": ["multi_factor", "rsi_bollinger", "volume_momentum", "funding_rate"],
            }
        ]
    }


# ────────────────────────────────────────────────────────────────
# 核心策略逻辑
# ────────────────────────────────────────────────────────────────

async def run_strategy_once() -> None:
    logger.info("=" * 60)
    logger.info("🔄 策略执行开始")
    # 每次执行前从交易所同步真实持仓（避免手动平仓后状态不一致）
    # 同步：配置交易对 + scanner缓存候选币（可能有持仓）
    symbols_to_sync = list(kline_fetchers.keys())
    if _scanner_cache["results"]:
        scanner_syms = [c.symbol for c in _scanner_cache["results"][:5]]
        for s in scanner_syms:
            if s not in symbols_to_sync:
                symbols_to_sync.append(s)
    try:
        # 从交易所拉所有活跃持仓（Bitget hedge mode 同一币会有 LONG+SHORT 两条）
        loop = asyncio.get_running_loop()
        exchange_positions = await loop.run_in_executor(None, exchange.fetch_positions)
        all_syms = set()
        for p in exchange_positions:
            if float(p.get('contracts', 0) or 0) != 0:
                # 统一格式化：去掉 :USDT 后缀，保持和 kline_fetchers key 一致
                sym = p.get('symbol', '')
                norm = sym  # keep :USDT suffix for swap markets
                all_syms.add(norm)
        if all_syms:
            logger.info(f"📡 交易所持仓: {all_syms}")
        # 合并 kline_fetchers keys + scanner cache（避免懒加载时缺少 kline fetcher）
        for s in list(kline_fetchers.keys()):
            all_syms.add(s)
        if _scanner_cache["results"]:
            for c in _scanner_cache["results"][:5]:
                all_syms.add(c.symbol)
        risk_manager.sync_from_exchange(exchange, list(all_syms))
    except Exception as e:
        logger.warning(f"持仓同步失败: {e}")
    # 始终合并交易所持仓币，确保止盈止损检查不遗漏
    if auto_add and _scanner_cache["results"]:
        raw_symbols = [c.symbol for c in _scanner_cache["results"][:5]]
        symbols_to_trade = list(dict.fromkeys(raw_symbols + list(all_syms)))
    else:
        symbols_to_trade = list(dict.fromkeys(SYMBOLS + list(all_syms)))
    logger.info(f"📡 处理列表 ({len(symbols_to_trade)}): {symbols_to_trade[:8]}{'...' if len(symbols_to_trade)>8 else ''}")
    for symbol in symbols_to_trade:
        await process_symbol(symbol)
    stats = risk_manager.get_stats()
    logger.info(
        f"📊 统计 | 总交易={stats['total_trades']} | "
        f"胜率={stats['win_rate']} | 总盈亏={stats['total_pnl']:+.4f} USDT"
    )
    logger.info("=" * 60)


async def process_symbol(symbol: str) -> None:
    try:
        # 懒加载：如果 symbol 未初始化，先创建
        if symbol not in kline_fetchers:
            logger.info(f"➕ 懒加载交易组件: {symbol}")
            kline_fetchers[symbol] = KlineFetcher(exchange, symbol)
            strategies[symbol] = MultiFactorStrategy(
                ma_short=cfg["strategy"]["ma"]["short_period"],
                ma_long=cfg["strategy"]["ma"]["long_period"],
            )
            routers[symbol] = StrategyRouter(
                enable_regime_detection=True,
                regime_weights=cfg["strategy"]["regime_weights"]
            )
            traders[symbol] = Trader(exchange=exchange, max_retries=3, retry_delay=1.0)
            traders[symbol].set_dry_run(DRY_RUN)

        df = kline_fetchers[symbol].fetch_klines(
            timeframe=TIMEFRAME,
            limit=cfg["data"]["kline_limit"],
        )

        current_price = 0.0
        if ws_client:
            current_price = ws_client.get_price(symbol)
        if current_price == 0.0:
            current_price = kline_fetchers[symbol].get_latest_price()

        if current_price == 0.0:
            logger.warning(f"{symbol} 无法获取价格，跳过")
            return

        # 计算 ATR 和 ATR%
        atr = calculate_atr(df['high'], df['low'], df['close'])
        atr_pct = atr / current_price if current_price > 0 and atr > 0 else 0.0

        triggered = risk_manager.check_stop_loss_take_profit(
            symbol, current_price, atr=atr
        )
        if triggered:
            for item in triggered:
                side = item["side"]
                position = item["position"]
                action = item["action"]
                close_qty = item.get("close_qty", position.quantity)
                labels = {"STOP_LOSS": "止损", "TAKE_PROFIT": "止盈", "PARTIAL_TP": "分批止盈", "TRAILING_STOP": "追踪止盈", "TIME_EXIT": "时间退出"}
                label = labels.get(action, action)
                logger.info(f"{symbol} {side} 触发{label}，平仓量={close_qty}")
                if side == "long":
                    order = traders[symbol].market_sell(symbol, close_qty, current_price, position_side="LONG", reduce_only=True)
                else:
                    order = traders[symbol].market_buy(symbol, close_qty, current_price, position_side="SHORT", reduce_only=True)
                if order["status"] != "failed":
                    if action == "PARTIAL_TP":
                        position.quantity -= close_qty
                        logger.info(f"{symbol} {side} 部分平仓完成，剩余={position.quantity}")
                    else:
                        risk_manager.close_position(symbol, side, current_price)
                elif action == "PARTIAL_TP":
                    # 部分平仓失败（通常是因为名义价值过低），全额平仓
                    logger.info(f"{symbol} {side} 部分平仓失败，改为全额平仓 {position.quantity}")
                    if side == "long":
                        order2 = traders[symbol].market_sell(symbol, position.quantity, current_price, position_side="LONG", reduce_only=True)
                    else:
                        order2 = traders[symbol].market_buy(symbol, position.quantity, current_price, position_side="SHORT", reduce_only=True)
                    if order2["status"] != "failed":
                        risk_manager.close_position(symbol, side, current_price)
            return

        # 获取资金费率
        funding_rate = None
        try:
            fr_data = exchange.fetch_funding_rate(symbol)
            funding_rate = float(fr_data.get('fundingRate') or fr_data.get('rate', 0.0) or 0.0)
        except Exception:
            pass

        # 使用 StrategyRouter 生成多策略融合信号
        signal_result = routers[symbol].generate_signal(df, funding_rate=funding_rate)
        signal = signal_result.signal
        strength = signal_result.score
        regime = signal_result.metadata.get('regime', 'unknown')

        logger.info(f"{symbol} 信号: {signal.value} | 强度={strength:.3f} | 价格={current_price:.8g} | regime={regime}")

        # SignalBridge: 推送信号到 AI-Trader
        signal_bridge.publish_signal(
            symbol=symbol, signal=signal.value, score=strength,
            price=current_price, confidence=signal_result.confidence,
            metadata=signal_result.metadata,
        )

        # SignalBridge: 拉取外部信号并融合
        external_signals = signal_bridge.fetch_external_signals(symbol)
        if external_signals:
            merged_score, merge_detail = signal_bridge.merge_external_signals(strength, external_signals)
            # 根据融合后的分数调整信号方向
            threshold = 0.15
            if merged_score > threshold:
                signal = Signal.BUY
            elif merged_score < -threshold:
                signal = Signal.SELL
            else:
                signal = Signal.HOLD
            strength = merged_score
            signal_result.metadata['external_merge'] = merge_detail
            logger.info(f"{symbol} 融合后: 信号={signal.value} | 强度={merged_score:.3f}")

        # AuditLogger: 信号快照
        kline_snapshot = []
        if len(df) >= 3:
            for _, row in df.tail(3).iterrows():
                kline_snapshot.append({
                    'open': float(row['open']), 'high': float(row['high']),
                    'low': float(row['low']), 'close': float(row['close']),
                    'volume': float(row['volume'])
                })

        # MarketFilter: 新闻/波动率/布林带综合检查
        filter_check = market_filter.pre_trade_check(symbol)
        if not filter_check["allowed"]:
            logger.info(f"{symbol} MarketFilter 拦截: {'; '.join(filter_check['reasons'])}")
            audit_logger.log_signal(
                symbol=symbol, signal=signal.value, strength=strength,
                price=current_price, params=signal_result.metadata,
                kline_snapshot=kline_snapshot, atr_pct=atr_pct,
                risk_check=filter_check,
            )
            return

        # AuditLogger: 记录所有信号周期
        audit_logger.log_signal(
            symbol=symbol, signal=signal.value, strength=strength,
            price=current_price, params=signal_result.metadata,
            kline_snapshot=kline_snapshot, atr_pct=atr_pct,
        )

        balance_info = traders[symbol].get_balance("USDT")
        free_balance = balance_info["free"] if balance_info else 0

        check = risk_manager.check_trade_allowed(
            symbol=symbol,
            balance=free_balance,
            price=current_price,
            signal=signal,
            regime=regime,
            atr=atr,
            atr_pct=atr_pct,
        )

        if not check["allowed"]:
            logger.debug(f"{symbol} 风控拦截: {check['reason']}")
            return

        quantity = check["size"]

        if signal == Signal.BUY:
            order = traders[symbol].market_buy(symbol, quantity, current_price)
            if order["status"] != "failed":
                risk_manager.open_position(
                    symbol=symbol,
                    side="long",
                    entry_price=order.get("average") or order.get("price") or current_price,
                    quantity=order.get("filled") or order.get("amount") or quantity,
                )
                audit_logger.log_trade(TradeContext(
                    symbol=symbol, signal=signal.value, signal_strength=strength,
                    price=current_price, atr_pct=atr_pct,
                    order_side="long", order_quantity=quantity,
                    order_result="FILLED", action="OPEN",
                    entry_price=order.get("average") or order.get("price") or current_price,
                    strategy_params=signal_result.metadata,
                    kline_snapshot=kline_snapshot,
                ))

        elif signal == Signal.SELL:
            pos_dict = risk_manager.get_position(symbol)
            if pos_dict and "long" in pos_dict:
                position = pos_dict["long"]
                order = traders[symbol].market_sell(symbol, position.quantity, current_price, position_side="LONG", reduce_only=True)
                if order["status"] != "failed":
                    pnl = (current_price - position.entry_price) * position.quantity
                    risk_manager.close_position(symbol, "long", current_price)
                    audit_logger.log_trade(TradeContext(
                        symbol=symbol, signal=signal.value, signal_strength=strength,
                        price=current_price, atr_pct=atr_pct,
                        order_side="short", order_quantity=position.quantity,
                        order_result="FILLED", action="CLOSE",
                        entry_price=position.entry_price, close_price=current_price,
                        pnl=pnl, kline_snapshot=kline_snapshot,
                    ))
            elif pos_dict and "short" in pos_dict:
                logger.info(f"{symbol} 已有空头持仓，不再加仓")
            else:
                order = traders[symbol].market_sell(symbol, quantity, current_price, position_side="SHORT")
                if order["status"] != "failed":
                    risk_manager.open_position(
                        symbol=symbol,
                        side="short",
                        entry_price=order.get("average") or order.get("price") or current_price,
                        quantity=order.get("filled") or order.get("amount") or quantity,
                    )
                    audit_logger.log_trade(TradeContext(
                        symbol=symbol, signal=signal.value, signal_strength=strength,
                        price=current_price, atr_pct=atr_pct,
                        order_side="short", order_quantity=quantity,
                        order_result="FILLED", action="OPEN",
                        entry_price=order.get("average") or order.get("price") or current_price,
                        strategy_params=signal_result.metadata,
                        kline_snapshot=kline_snapshot,
                    ))

    except Exception as e:
        logger.error(f"{symbol} 策略执行异常: {str(e)}\n{traceback.format_exc()}")


async def strategy_loop() -> None:
    interval = cfg["strategy"]["interval"]
    logger.info(f"🚀 策略循环启动 | 间隔: {interval}s | 交易对: {SYMBOLS}")
    while True:
        await run_strategy_once()
        logger.info(f"⏳ 等待 {interval}s 后执行下一轮...")
        await asyncio.sleep(interval)


async def scanner_loop() -> None:
    global SYMBOLS, _scanner_cache, _last_scan_time
    if not scanner or not auto_add:
        return
    logger.info(f"🔍 Scanner 循环 | 扫描间隔: {_scan_interval}s")
    blacklist = set(cfg["trading"].get("blacklist", []))
    global SYMBOLS

    while True:
        try:
            elapsed = time.time() - _last_scan_time
            if elapsed < _scan_interval:
                await asyncio.sleep(_scan_interval - elapsed)
                continue
            logger.debug(f"scanner_loop 开始扫描...")
            _last_scan_time = time.time()
            loop = asyncio.get_running_loop()
            try:
                candidates = await asyncio.wait_for(
                    loop.run_in_executor(
                        None, lambda: scanner.scan(limit=cfg.get("scanner", {}).get("top_n", 20))
                    ),
                    timeout=30.0
                )
            except asyncio.TimeoutError:
                logger.warning("Scanner.scan() 超时，跳过本轮")
                continue
            _scanner_cache = {"results": candidates, "timestamp": time.time()}
            new_symbols = [c.symbol for c in candidates if c.symbol not in blacklist]
            if new_symbols != SYMBOLS:
                logger.info(f"🔄 Scanner 更新: {len(new_symbols)} 个币 → {new_symbols}")
                SYMBOLS = new_symbols
        except Exception as e:
            logger.error(f"Scanner 循环异常: {e}", exc_info=True)


async def ws_loop() -> None:
    if not ws_client:
        return
    logger.info("📡 MultiWebSocket 启动...")
    await ws_client.connect()
    await ws_client.listen()


# ────────────────────────────────────────────────────────────────
# 入口
# ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info",
    )
