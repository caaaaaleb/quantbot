"""
RSI + 布林带均值回归策略
适用于震荡市场（SIDEWAYS）
"""

import pandas as pd
from typing import Optional

from src.strategy.base import BaseStrategy, SignalResult, Signal
from src.data.indicators import calculate_rsi, calculate_bollinger_bands


class RSIBollingerStrategy(BaseStrategy):
    """
    RSI + 布林带均值回归策略

    逻辑：
    - RSI < 30 且价格触及布林带下轨 → 超卖 → BUY
    - RSI > 70 且价格触及布林带上轨 → 超买 → SELL
    - RSI 在 40-60 区间 → 中性 → HOLD
    - 配合布林带偏离度增强信号
    """

    name = "RSI_Bollinger"

    def __init__(
        self,
        rsi_period: int = 14,
        rsi_oversold: float = 30.0,
        rsi_overbought: float = 70.0,
        bb_period: int = 20,
        bb_std: float = 2.0,
        # 权重
        rsi_weight: float = 0.6,
        bb_weight: float = 0.4
    ):
        self.rsi_period = rsi_period
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        self.bb_period = bb_period
        self.bb_std = bb_std
        self.rsi_weight = rsi_weight
        self.bb_weight = bb_weight

    def generate_signal(self, df: pd.DataFrame) -> SignalResult:
        if len(df) < max(self.rsi_period, self.bb_period) + 1:
            return SignalResult(
                signal=Signal.HOLD,
                score=0.0,
                confidence=0.0,
                metadata={'reason': 'insufficient_data'}
            )

        close = df['close']

        # RSI
        rsi = calculate_rsi(close, self.rsi_period)

        # 布林带
        bb = calculate_bollinger_bands(close, self.bb_period, self.bb_std)

        # RSI 信号
        if rsi < self.rsi_oversold:
            rsi_signal = 1.0  # 超卖 → 多
        elif rsi > self.rsi_overbought:
            rsi_signal = -1.0  # 超买 → 空
        else:
            # 中性区间，RSI 越低越偏多
            rsi_signal = (50 - rsi) / 50  # 归一化到 -1 ~ 1

        # 布林带信号
        # deviation < -0.05 价格严重低于布林带下轨 → 超卖
        # deviation > 0.05 价格严重高于布林带上轨 → 超买
        bb_deviation = bb['deviation']

        if bb_deviation < -0.05:
            bb_signal = 1.0  # 价格超跌 → 多
        elif bb_deviation > 0.05:
            bb_signal = -1.0  # 价格超涨 → 空
        else:
            bb_signal = -bb_deviation  # 回归中性

        # 加权得分
        total_score = rsi_signal * self.rsi_weight + bb_signal * self.bb_weight

        # 置信度：两个指标一致时更高
        if rsi_signal * bb_signal > 0:  # 同向
            confidence = min(abs(total_score) + 0.3, 1.0)
        else:
            confidence = max(abs(total_score) - 0.2, 0.1)

        # 最终信号
        if total_score > 0.3:
            signal = Signal.BUY
        elif total_score < -0.3:
            signal = Signal.SELL
        else:
            signal = Signal.HOLD

        return SignalResult(
            signal=signal,
            score=round(total_score, 4),
            confidence=round(confidence, 4),
            metadata={
                'rsi': round(rsi, 2),
                'bb_upper': round(bb['upper'], 2),
                'bb_middle': round(bb['middle'], 2),
                'bb_lower': round(bb['lower'], 2),
                'bb_deviation': round(bb_deviation * 100, 2),  # 转为百分比
                'rsi_signal': rsi_signal,
                'bb_signal': bb_signal
            }
        )

    def get_required_columns(self) -> list:
        return ['close']
