"""策略执行审计日志模块

每次下单/止损/止盈都记录完整上下文，方便复盘和对齐策略理解。
"""

import json
import os
import threading
from datetime import datetime
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field, asdict

from src.utils.logger import logger


@dataclass
class TradeContext:
    """交易上下文——记录一笔交易的所有决策信息"""
    # 基础信息
    symbol: str
    signal: str               # BUY / SELL / HOLD
    signal_strength: float    # 信号强度 (0~1)
    timestamp: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    # 市场状态
    price: float = 0.0
    atr_pct: float = 0.0
    boll_deviation: float = 0.0

    # 风控决策
    risk_check_passed: bool = True
    risk_check_reasons: List[str] = field(default_factory=list)
    risk_check_warnings: List[str] = field(default_factory=list)

    # 新闻过滤
    news_paused: bool = False
    news_reason: str = ""

    # 持仓状态
    has_position: bool = False
    position_side: str = ""   # long / short / none
    position_size: float = 0.0

    # 订单执行
    order_side: str = ""      # long / short / none
    order_quantity: float = 0.0
    order_price: float = 0.0  # 0 表示市价
    order_result: str = ""    # FILLED / REJECTED / SKIPPED / CANCELLED
    order_error: str = ""

    # 持仓变化（开仓/平仓）
    action: str = ""          # OPEN / CLOSE / SKIP / STOP_LOSS / TAKE_PROFIT
    pnl: float = 0.0
    pnl_pct: float = 0.0
    entry_price: float = 0.0
    close_price: float = 0.0

    # 策略参数快照
    strategy_params: Dict[str, Any] = field(default_factory=dict)

    # K线快照（最近3根）
    kline_snapshot: List[Dict[str, float]] = field(default_factory=list)


class AuditLogger:
    """
    审计日志记录器

    - 线程安全，支持多线程并发写入
    - 每天一个新文件，便于归档和查询
    - 同时输出到标准 logger（INFO级别）
    """

    def __init__(self, log_dir: str = "logs/audit"):
        self.log_dir = log_dir
        os.makedirs(self.log_dir, exist_ok=True)
        self._lock = threading.Lock()

    def _today_file(self) -> str:
        today = datetime.now().strftime("%Y-%m-%d")
        return os.path.join(self.log_dir, f"audit_{today}.jsonl")

    def _write(self, entry: dict):
        """写入单条 JSONL 日志"""
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with self._lock:
            with open(self._today_file(), "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def log_trade(self, ctx: TradeContext) -> None:
        """
        记录一笔完整的交易决策

        ctx: TradeContext 对象
        """
        entry = asdict(ctx)
        entry["_type"] = "trade"

        # 同时打印到标准日志
        msg = (
            f"[AUDIT] {ctx.action} {ctx.symbol} | "
            f"信号={ctx.signal}({ctx.signal_strength:.2f}) | "
            f"价格={ctx.price} | "
            f"结果={ctx.order_result} | "
            f"盈亏={ctx.pnl:+.4f}({ctx.pnl_pct*100:+.2f}%)"
        )

        if ctx.order_result == "REJECTED":
            logger.warning(msg)
            self._write(entry)
            return

        if ctx.action in ("SKIP",):
            logger.info(msg)
        else:
            logger.info(msg)

        self._write(entry)

    def log_signal(
        self,
        symbol: str,
        signal: str,
        strength: float,
        price: float,
        params: Dict[str, Any],
        kline_snapshot: List[Dict[str, float]],
        atr_pct: float = 0.0,
        boll_dev: float = 0.0,
        risk_check: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        记录一个信号周期（不一定会下单）

        risk_check: pre_trade_check 返回的字典
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        entry = {
            "_type": "signal",
            "timestamp": now,
            "symbol": symbol,
            "signal": signal,
            "signal_strength": strength,
            "price": price,
            "atr_pct": atr_pct,
            "boll_deviation": boll_dev,
            "strategy_params": params,
            "kline_snapshot": kline_snapshot,
        }

        if risk_check:
            entry["risk_allowed"] = risk_check.get("allowed", True)
            entry["risk_reasons"] = risk_check.get("reasons", [])
            entry["risk_warnings"] = risk_check.get("warnings", [])
        else:
            entry["risk_allowed"] = True
            entry["risk_reasons"] = []
            entry["risk_warnings"] = []

        self._write(entry)

    def get_today_trades(self) -> List[Dict]:
        """读取今日所有交易记录"""
        fpath = self._today_file()
        if not os.path.exists(fpath):
            return []

        trades = []
        with open(fpath, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("_type") == "trade":
                        trades.append(entry)
                except json.JSONDecodeError:
                    continue
        return trades

    def get_daily_summary(self, date_str: str = None) -> Dict[str, Any]:
        """
        获取指定日期的交易摘要

        date_str: YYYY-MM-DD 格式，默认今天
        """
        if date_str is None:
            date_str = datetime.now().strftime("%Y-%m-%d")

        fpath = os.path.join(self.log_dir, f"audit_{date_str}.jsonl")
        if not os.path.exists(fpath):
            return {"date": date_str, "trades": [], "summary": {}}

        trades = []
        with open(fpath, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("_type") == "trade":
                        trades.append(entry)
                except json.JSONDecodeError:
                    continue

        total = len(trades)
        filled = [t for t in trades if t.get("order_result") == "FILLED"]
        rejected = [t for t in trades if t.get("order_result") == "REJECTED"]
        skipped = [t for t in trades if t.get("order_result") in ("SKIPPED", "")]
        winners = [t for t in filled if t.get("pnl", 0) > 0]
        losers = [t for t in filled if t.get("pnl", 0) < 0]

        total_pnl = sum(t.get("pnl", 0) for t in filled)
        win_rate = len(winners) / len(filled) * 100 if filled else 0

        return {
            "date": date_str,
            "total_signals": total,
            "filled": len(filled),
            "rejected": len(rejected),
            "skipped": len(skipped),
            "winners": len(winners),
            "losers": len(losers),
            "win_rate": f"{win_rate:.1f}%",
            "total_pnl": f"{total_pnl:+.4f} USDT",
            "rejections_by_reason": self._summarize_rejections(rejected),
        }

    def _summarize_rejections(self, rejected: List[Dict]) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for t in rejected:
            for reason in (t.get("risk_check_reasons") or []):
                counts[reason] = counts.get(reason, 0) + 1
        return counts
