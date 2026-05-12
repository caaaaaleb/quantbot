"""
资金费率策略
独立的资金费率因子策略
"""

import numpy as np
import pandas as pd
from typing import Optional

from src.strategy.base import BaseStrategy, SignalResult, Signal


class FundingRateStrategy(BaseStrategy):
    """
    资金费率策略

    逻辑：
    - 负费率（空头付费给多头）→ 做多有利 → BUY
    - 正费率（多头付费给空头）→ 做空有利 → SELL
    - 费率接近 0 → 中性 → HOLD

    注：资金费率仅适用于永续合约交易，现货模式下无费率数据
    """

    name = "FundingRate"

    def __init__(
        self,
        threshold: float = 0.0001,  # 0.01%
        signal_multiplier: float = 1000.0
    ):
        self.threshold = threshold
        self.signal_multiplier = signal_multiplier

    def generate_signal(
        self,
        _df: pd.DataFrame,
        funding_rate: Optional[float] = None
    ) -> SignalResult:
        """
        生成资金费率信号

        Args:
            df: K线数据（未使用，主要用于接口统一）
            funding_rate: 资金费率（从 Bitget Futures API 获取），None 则返回中性信号
        """
        if funding_rate is None:
            return SignalResult(
                signal=Signal.HOLD,
                score=0.0,
                confidence=0.0,
                metadata={'funding_rate': None, 'description': '无资金费率数据'}
            )

        # 资金费率信号
        # 负费率 -> 多头有利 -> BUY
        # 正费率 -> 空头有利 -> SELL
        signal_value = -funding_rate * self.signal_multiplier
        signal_value = np.clip(signal_value, -1, 1)

        # 描述
        if funding_rate > self.threshold:
            desc = f"正费率({funding_rate*100:.4f}%) -> 做空有利"
        elif funding_rate < -self.threshold:
            desc = f"负费率({funding_rate*100:.4f}%) -> 做多有利"
        else:
            desc = f"中性费率({funding_rate*100:.4f}%)"

        # 置信度：费率绝对值越大置信度越高
        confidence = min(abs(funding_rate) * 10000, 1.0)

        # 信号判断
        if signal_value > 0.3:
            signal = Signal.BUY
        elif signal_value < -0.3:
            signal = Signal.SELL
        else:
            signal = Signal.HOLD

        return SignalResult(
            signal=signal,
            score=round(signal_value, 4),
            confidence=round(confidence, 4),
            metadata={
                'funding_rate': round(funding_rate, 6),
                'description': desc
            }
        )

    def get_required_columns(self) -> list:
        return []  # 不依赖 K 线数据
