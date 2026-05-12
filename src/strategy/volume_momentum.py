"""
成交量动量策略
适用于所有市场状态，辅助判断趋势强度
"""

import pandas as pd

from src.strategy.base import BaseStrategy, SignalResult, Signal
from src.data.indicators import (
    calculate_rsi,
    calculate_ma_cross,
    calculate_volume_ratio
)


class VolumeMomentumStrategy(BaseStrategy):
    """
    成交量动量策略

    逻辑：
    - 放量 + 价格上涨 → 上涨动量增强
    - 放量 + 价格下跌 → 下跌动量增强
    - 缩量 → 趋势减弱，谨慎

    结合 RSI 过滤超买超卖区域
    """

    name = "Volume_Momentum"

    def __init__(
        self,
        vol_period: int = 20,
        vol_spike_threshold: float = 2.0,  # 2倍均量
        vol_shrink_threshold: float = 0.5,  # 0.5倍均量
        rsi_period: int = 14,
        rsi_upper: float = 70.0,
        rsi_lower: float = 30.0,
        # 动量权重
        volume_weight: float = 0.5,
        price_weight: float = 0.3,
        rsi_weight: float = 0.2
    ):
        self.vol_period = vol_period
        self.vol_spike_threshold = vol_spike_threshold
        self.vol_shrink_threshold = vol_shrink_threshold
        self.rsi_period = rsi_period
        self.rsi_upper = rsi_upper
        self.rsi_lower = rsi_lower
        self.volume_weight = volume_weight
        self.price_weight = price_weight
        self.rsi_weight = rsi_weight

    def generate_signal(self, df: pd.DataFrame) -> SignalResult:
        if len(df) < max(self.vol_period, self.rsi_period) + 1:
            return SignalResult(
                signal=Signal.HOLD,
                score=0.0,
                confidence=0.0,
                metadata={'reason': 'insufficient_data'}
            )

        close = df['close']
        volumes = df.get('volume', pd.Series([1] * len(close)))

        # 成交量比率
        vol_ratio = calculate_volume_ratio(volumes, self.vol_period)

        # MA 交叉信号
        ma_cross = calculate_ma_cross(close, short_period=5, long_period=20)

        # RSI
        rsi = calculate_rsi(close, self.rsi_period)

        # 成交量信号
        if vol_ratio > self.vol_spike_threshold:
            vol_signal = 0.8  # 放量确认
        elif vol_ratio < self.vol_shrink_threshold:
            vol_signal = -0.3  # 缩量，趋势可能减弱
        else:
            vol_signal = 0.0

        # 价格动量信号：基于 MA 交叉
        ma_spread = ma_cross['spread']
        if ma_spread > 0.02:  # 2% 差距
            price_signal = 1.0
        elif ma_spread < -0.02:
            price_signal = -1.0
        else:
            price_signal = ma_spread / 0.02  # 归一化

        # RSI 信号
        if rsi < self.rsi_lower:
            rsi_signal = 1.0  # 超卖
        elif rsi > self.rsi_upper:
            rsi_signal = -1.0  # 超买
        else:
            rsi_signal = (50 - rsi) / 50

        # 加权得分
        total_score = (
            vol_signal * self.volume_weight +
            price_signal * self.price_weight +
            rsi_signal * self.rsi_weight
        )

        # 置信度：成交量异常时置信度更高
        if vol_ratio > self.vol_spike_threshold or vol_ratio < self.vol_shrink_threshold:
            confidence = min(abs(total_score) + 0.3, 1.0)
        else:
            confidence = max(abs(total_score) * 0.7, 0.2)

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
                'vol_ratio': round(vol_ratio, 2),
                'vol_signal': vol_signal,
                'price_signal': price_signal,
                'rsi': round(rsi, 2),
                'rsi_signal': rsi_signal,
                'ma_spread': round(ma_spread * 100, 2)  # 百分比
            }
        )

    def get_required_columns(self) -> list:
        return ['close', 'volume']
