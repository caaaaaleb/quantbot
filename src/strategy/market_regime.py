"""
市场状态识别模块
基于 ADX 和 ATR 判断市场状态：trend / sideways / high_vol
"""

from enum import Enum
import pandas as pd

from src.data.indicators import calculate_adx, calculate_atr_percent


class MarketRegime(Enum):
    TREND = "trend"        # 趋势市场：ADX > 25
    SIDEWAYS = "sideways"  # 震荡市场：ADX < 25，低波动
    HIGH_VOL = "high_vol"  # 高波动：ADX < 25，高波动


class MarketRegimeDetector:
    """
    市场状态检测器

    判断逻辑：
    - ADX > 25 → TREND（顺势策略优先）
    - ADX < 25 且 ATR% < 3% → SIDEWAYS（均值回归策略优先）
    - ADX < 25 且 ATR% > 5% → HIGH_VOL（轻仓或观望）
    """

    def __init__(
        self,
        adx_period: int = 14,
        adx_trend_threshold: float = 25.0,
        atr_low_threshold: float = 3.0,
        atr_high_threshold: float = 5.0,
        atr_lookback: int = 20
    ):
        self.adx_period = adx_period
        self.adx_trend_threshold = adx_trend_threshold
        self.atr_low_threshold = atr_low_threshold
        self.atr_high_threshold = atr_high_threshold
        self.atr_lookback = atr_lookback

        # 缓存历史 ATR% 用于计算均值
        self._atr_history: list[float] = []

    def detect(self, df: pd.DataFrame) -> MarketRegime:
        """
        检测市场状态

        Args:
            df: K线数据

        Returns:
            MarketRegime: 市场状态
        """
        if len(df) < self.adx_period * 2:
            return MarketRegime.SIDEWAYS

        high = df['high']
        low = df['low']
        close = df['close']

        # 计算 ADX
        adx = calculate_adx(high, low, close, self.adx_period)

        # 计算 ATR%
        atr_pct = calculate_atr_percent(high, low, close, self.adx_period)

        # 记录历史 ATR%
        self._atr_history.append(atr_pct)
        if len(self._atr_history) > self.atr_lookback * 2:
            self._atr_history.pop(0)

        # 判断市场状态
        if adx > self.adx_trend_threshold:
            return MarketRegime.TREND

        # ADX < 25，根据波动率细分
        if atr_pct > self.atr_high_threshold:
            return MarketRegime.HIGH_VOL
        else:
            return MarketRegime.SIDEWAYS

    def detect_with_details(self, df: pd.DataFrame) -> dict:
        """
        检测市场状态并返回详细信息
        """
        if len(df) < self.adx_period * 2:
            return {
                'regime': MarketRegime.SIDEWAYS,
                'adx': 0.0,
                'atr_pct': 0.0,
                'atr_avg_pct': 0.0
            }

        high = df['high']
        low = df['low']
        close = df['close']

        adx = calculate_adx(high, low, close, self.adx_period)
        atr_pct = calculate_atr_percent(high, low, close, self.adx_period)

        # 计算 ATR% 均值
        atr_avg = sum(self._atr_history) / len(self._atr_history) if self._atr_history else atr_pct

        return {
            'regime': self.detect(df),
            'adx': round(adx, 2),
            'atr_pct': round(atr_pct, 2),
            'atr_avg_pct': round(atr_avg, 2),
            'adx_threshold': self.adx_trend_threshold,
            'atr_low': self.atr_low_threshold,
            'atr_high': self.atr_high_threshold
        }



