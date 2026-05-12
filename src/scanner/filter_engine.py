"""过滤系统 - 过滤低质量币种"""

from dataclasses import dataclass

from src.scanner.feature_engine import CoinFeatures
from src.utils.logger import logger


@dataclass
class FilterResult:
    """过滤结果"""
    passed: bool
    reason: str


class FilterEngine:
    """
    过滤系统：剔除低流动性 / 高价差 / 极端涨跌的币
    """

    def __init__(
        self,
        min_volume_24h: float = 1_000_000.0,
        max_spread: float = 0.005,
        max_change_24h: float = 80.0,
        max_atr_pct: float = 0.10,
    ):
        self.min_volume_24h = min_volume_24h
        self.max_spread = max_spread
        self.max_change_24h = max_change_24h
        self.max_atr_pct = max_atr_pct

    def pass_filter(self, f: CoinFeatures, spread: float = 0.0) -> FilterResult:
        """
        过滤检查

        Returns:
            FilterResult: passed=True 表示通过
        """
        # ── 流动性检查 ───────────────────────────────────────────────
        if f.volume_24h < self.min_volume_24h:
            return FilterResult(
                passed=False,
                reason=f"24h成交量 ${f.volume_24h/1e6:.1f}M < ${self.min_volume_24h/1e6:.0f}M"
            )

        # ── 价差检查 ─────────────────────────────────────────────────
        if f.price > 0 and spread / f.price > self.max_spread:
            return FilterResult(
                passed=False,
                reason=f"买卖价差 {(spread/f.price)*100:.2f}% > {self.max_spread*100:.1f}%"
            )

        # ── 极端涨跌 ─────────────────────────────────────────────────
        if abs(f.change_24h) > self.max_change_24h:
            return FilterResult(
                passed=False,
                reason=f"24h涨跌幅 {f.change_24h:.1f}% 超出范围 ±{self.max_change_24h}%"
            )

        # ── 波动率检查 ───────────────────────────────────────────────
        if f.atr_pct > self.max_atr_pct:
            return FilterResult(
                passed=False,
                reason=f"ATR波动率 {f.atr_pct*100:.1f}% > {self.max_atr_pct*100:.0f}%"
            )

        return FilterResult(passed=True, reason="通过")