"""信号引擎 - 检测启动信号和突破确认"""

from dataclasses import dataclass

from src.scanner.feature_engine import CoinFeatures
from src.utils.logger import logger


@dataclass
class SignalResult:
    """信号结果"""
    signal: float          # 综合信号值 (-1 ~ 1)
    stage: str            # early / mid / late / none
    confidence: float     # 置信度 (0 ~ 1)
    reasons: list         # 信号组成原因


class SignalEngine:
    """
    信号引擎：检测 early breakout / 资金推动 / 突破确认
    """

    def __init__(
        self,
        momentum_1m_threshold: float = 0.005,
        momentum_5m_threshold: float = 0.02,
        volume_spike_threshold: float = 2.0,
        taker_buy_threshold: float = 0.6,
        breakout_threshold: float = 1.01,
    ):
        self.momentum_1m_threshold = momentum_1m_threshold
        self.momentum_5m_threshold = momentum_5m_threshold
        self.volume_spike_threshold = volume_spike_threshold
        self.taker_buy_threshold = taker_buy_threshold
        self.breakout_threshold = breakout_threshold

    def detect(self, f: CoinFeatures) -> SignalResult:
        """
        检测信号

        Args:
            f: 币种特征

        Returns:
            SignalResult: 信号结果
        """
        signal = 0.0
        reasons = []

        # ── 1. 启动信号（Early Breakout） ────────────────────────────
        if f.momentum_1m > self.momentum_1m_threshold and f.momentum_5m > self.momentum_5m_threshold:
            if f.volume_spike > self.volume_spike_threshold:
                signal += 0.4
                reasons.append(f"启动信号: 1m+5m双动量 + 放量({f.volume_spike:.1f}x)")
            elif f.momentum_5m > self.momentum_5m_threshold * 2:
                # 无放量但动量极强
                signal += 0.2
                reasons.append(f"强动量无放量: 1m={f.momentum_1m:.4f} 5m={f.momentum_5m:.4f}")

        # ── 2. 资金推动 ──────────────────────────────────────────────
        if f.taker_buy_ratio > self.taker_buy_threshold:
            signal += 0.3 * (f.taker_buy_ratio - 0.5) * 2  # 归一化到 0~0.3
            reasons.append(f"资金推动: taker_buy={f.taker_buy_ratio:.2f}")

        # ── 3. 突破确认 ──────────────────────────────────────────────
        if f.breakout_ratio > self.breakout_threshold:
            signal += 0.3 * min((f.breakout_ratio - 1.0) / 0.05, 1.0)
            reasons.append(f"突破确认: ratio={f.breakout_ratio:.3f}")

        # ── 4. 订单簿不平衡 ─────────────────────────────────────────
        if f.orderbook_imbalance > 0.2:
            signal += 0.1
            reasons.append(f"订单簿偏买: imbalance={f.orderbook_imbalance:.2f}")

        # ── 5. 风险扣分 ──────────────────────────────────────────────
        if f.change_24h > 30:
            signal -= 0.3 * min((f.change_24h - 30) / 30, 1.0)
            reasons.append(f"24h涨幅过大扣分: {f.change_24h:.1f}%")

        if f.atr_pct > 0.05:
            signal -= 0.2
            reasons.append(f"高波动扣分: ATR%={f.atr_pct*100:.1f}%")

        # ── 阶段分类 ─────────────────────────────────────────────────
        if f.change_24h > 30:
            stage = "late"
        elif f.momentum_5m > self.momentum_5m_threshold * 1.5 and f.volume_spike > self.volume_spike_threshold:
            stage = "early"
        elif f.momentum_5m > self.momentum_5m_threshold:
            stage = "mid"
        else:
            stage = "none"

        # ── 置信度计算 ───────────────────────────────────────────────
        confidence = min(abs(signal) / 0.8, 1.0) if signal != 0 else 0.0

        return SignalResult(
            signal=signal,
            stage=stage,
            confidence=confidence,
            reasons=reasons,
        )