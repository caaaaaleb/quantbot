"""
多时间框架趋势检测器
基于 Nerve-Knife 策略核心：
  - M30: Heiken Ashi Smoothed + MACD 柱越零轴 → 大趋势（9h+）
  - M5:  HAS + MACD 柱越 signal 线 → 小趋势（敏捷）
  - M1:  5线管壁突破 3% → 极端行情触发（海豹突击队）
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

import numpy as np
import pandas as pd


class TrendDirection(Enum):
    UP = "up"
    DOWN = "down"
    WEAK = "weak"  # 弱趋势/震荡


@dataclass
class TrendResult:
    big_trend: TrendDirection       # M30 大趋势方向
    small_trend: TrendDirection     # M5 小趋势方向
    big_stable: bool                # 大趋势是否稳定（HAS+MACD 一致且强烈）
    small_oscillating: bool         # 小趋势是否处于震荡期
    tube_breakout: bool             # M1 管壁是否被反向突破 3%
    tube_breakout_direction: Optional[TrendDirection] = None
    big_has_color: str = ""         # "green" or "red"
    small_has_color: str = ""
    m30_macd_above_zero: bool = False
    m5_macd_above_signal: bool = False


class MultiTimeframeDetector:
    """
    多时间框架趋势检测器

    用法:
        detector = MultiTimeframeDetector()
        df_m30 = exchange.fetch_ohlcv(symbol, "30m", limit=100)
        df_m5 = exchange.fetch_ohlcv(symbol, "5m", limit=100)
        df_m1 = exchange.fetch_ohlcv(symbol, "1m", limit=10)
        trend = detector.detect(pd.DataFrame(df_m30), pd.DataFrame(df_m5), pd.DataFrame(df_m1))
    """

    def __init__(
        self,
        has_period: int = 6,             # HAS 平滑周期
        has_smooth_period: int = 3,      # HAS 二次平滑周期
        macd_fast: int = 12,
        macd_slow: int = 26,
        macd_signal: int = 9,
        tube_bars: int = 5,              # 管壁 K 线数
        tube_breakout_pct: float = 3.0,  # 管壁突破阈值 (%)
        adx_period: int = 14,
        adx_weak_threshold: float = 25.0,
    ):
        self.has_period = has_period
        self.has_smooth_period = has_smooth_period
        self.macd_fast = macd_fast
        self.macd_slow = macd_slow
        self.macd_signal = macd_signal
        self.tube_bars = tube_bars
        self.tube_breakout_pct = tube_breakout_pct / 100.0  # 转为小数
        self.adx_period = adx_period
        self.adx_weak_threshold = adx_weak_threshold

    # ── Heiken Ashi Smoothed ──────────────────────────────────────

    @staticmethod
    def heiken_ashi(df: pd.DataFrame) -> pd.DataFrame:
        """计算标准 Heiken Ashi"""
        ha = pd.DataFrame(index=df.index)
        ha['close'] = (df['open'] + df['high'] + df['low'] + df['close']) / 4
        ha['open'] = ha['close'].copy()
        for i in range(1, len(ha)):
            ha.loc[ha.index[i], 'open'] = (
                ha.loc[ha.index[i - 1], 'open'] + ha.loc[ha.index[i - 1], 'close']
            ) / 2
        ha['high'] = df[['high', 'open', 'close']].max(axis=1)
        ha['low'] = df[['low', 'open', 'close']].min(axis=1)
        return ha

    def heiken_ashi_smoothed(self, df: pd.DataFrame) -> pd.DataFrame:
        """计算 Heiken Ashi Smoothed (HAS + 二次平滑)"""
        ha = self.heiken_ashi(df)
        smoothed = pd.DataFrame(index=ha.index)
        # 对 HA close/open 再做一次平滑
        smoothed['open'] = ha['open'].ewm(span=self.has_smooth_period, adjust=False).mean()
        smoothed['close'] = ha['close'].ewm(span=self.has_smooth_period, adjust=False).mean()
        smoothed['high'] = ha['high']
        smoothed['low'] = ha['low']
        # 颜色: 阳线=上升(绿), 阴线=下降(红)
        smoothed['color'] = np.where(
            smoothed['close'] >= smoothed['open'], 'green', 'red'
        )
        return smoothed

    # ── MACD ──────────────────────────────────────────────────────

    @staticmethod
    def macd(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
        """计算 MACD"""
        close = df['close'].astype(float)
        ema_fast = close.ewm(span=fast, adjust=False).mean()
        ema_slow = close.ewm(span=slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal, adjust=False).mean()
        histogram = macd_line - signal_line
        return pd.DataFrame({
            'macd': macd_line,
            'signal': signal_line,
            'histogram': histogram,
        }, index=df.index)

    # ── 大趋势检测 (M30) ──────────────────────────────────────────

    def detect_big_trend(self, df_m30: pd.DataFrame) -> tuple[TrendDirection, str, bool]:
        """
        M30 大趋势：HAS 颜色定方向 + MACD 柱越零轴确认
        使用前一根 K 线判断（稳定），平均 9h+ 一个趋势周期

        Returns:
            (TrendDirection, has_color, has_color_consistent)
        """
        if len(df_m30) < self.macd_slow + 5:
            return TrendDirection.WEAK, "", False

        has = self.heiken_ashi_smoothed(df_m30)
        macd_data = self.macd(df_m30, self.macd_fast, self.macd_slow, self.macd_signal)

        # 前一根 K 线的 HAS 颜色
        prev_has_color = has['color'].iloc[-2]
        # 前一根 K 线的 MACD 柱是否在零轴上方
        prev_macd_above = macd_data['histogram'].iloc[-2] > 0

        # 大趋势判断: HAS 颜色 + MACD 柱零轴位置一致
        if prev_has_color == 'green' and prev_macd_above:
            # 确认颜色一致性（最近 3 根至少 2 根同色）
            recent_colors = has['color'].iloc[-4:-1].tolist()
            consistent = recent_colors.count('green') >= 2
            return TrendDirection.UP, 'green', consistent
        elif prev_has_color == 'red' and not prev_macd_above:
            recent_colors = has['color'].iloc[-4:-1].tolist()
            consistent = recent_colors.count('red') >= 2
            return TrendDirection.DOWN, 'red', consistent
        else:
            return TrendDirection.WEAK, prev_has_color, False

    # ── 小趋势检测 (M5) ───────────────────────────────────────────

    def detect_small_trend(self, df_m5: pd.DataFrame) -> tuple[TrendDirection, str, bool]:
        """
        M5 小趋势：HAS 颜色定方向 + MACD 柱越 signal 线
        使用当前 K 线判断（敏捷），快速反应

        Returns:
            (TrendDirection, has_color, oscillating)
            oscillating=True 表示 HAS 与 MACD 反向 → 震荡期，应减仓
        """
        if len(df_m5) < self.macd_slow + 5:
            return TrendDirection.WEAK, "", True

        has = self.heiken_ashi_smoothed(df_m5)
        macd_data = self.macd(df_m5, self.macd_fast, self.macd_slow, self.macd_signal)

        # 当前 K 线
        cur_has_color = has['color'].iloc[-1]
        cur_macd_above_signal = macd_data['histogram'].iloc[-1] > 0

        # HAS 与 MACD 是否一致
        has_up = cur_has_color == 'green'
        macd_up = cur_macd_above_signal
        oscillating = has_up != macd_up

        if has_up and macd_up:
            return TrendDirection.UP, cur_has_color, False
        elif (not has_up) and (not macd_up):
            return TrendDirection.DOWN, cur_has_color, False
        elif has_up and not macd_up:
            return TrendDirection.UP, cur_has_color, True  # 弱上升
        else:
            return TrendDirection.DOWN, cur_has_color, True  # 弱下降

    # ── 管壁突破检测 (M1) ─────────────────────────────────────────

    def detect_tube_breakout(self, df_m1: pd.DataFrame) -> tuple[bool, Optional[TrendDirection]]:
        """
        M1 × 5 管壁突破检测

        取最近 5 根 M1 K 线，若当前价格反向突破 3%，触发"海豹突击队"
        """
        if len(df_m1) < self.tube_bars:
            return False, None

        recent = df_m1.iloc[-self.tube_bars:]
        highest = recent['high'].max()
        lowest = recent['low'].min()
        current = df_m1['close'].iloc[-1]

        # 向上突破
        if current > highest * (1 + self.tube_breakout_pct):
            return True, TrendDirection.UP
        # 向下突破
        if current < lowest * (1 - self.tube_breakout_pct):
            return True, TrendDirection.DOWN

        return False, None

    # ── 综合检测 ──────────────────────────────────────────────────

    def detect(
        self,
        df_m30: pd.DataFrame,
        df_m5: pd.DataFrame,
        df_m1: pd.DataFrame,
    ) -> TrendResult:
        """综合多时间框架检测"""
        big_trend, big_color, big_stable = self.detect_big_trend(df_m30)
        small_trend, small_color, small_osc = self.detect_small_trend(df_m5)
        tube_bk, tube_dir = self.detect_tube_breakout(df_m1)

        macd_m5 = self.macd(df_m5, self.macd_fast, self.macd_slow, self.macd_signal)
        m5_macd_above_signal = macd_m5['histogram'].iloc[-1] > 0

        macd_m30 = self.macd(df_m30, self.macd_fast, self.macd_slow, self.macd_signal)
        m30_macd_above_zero = macd_m30['histogram'].iloc[-2] > 0

        return TrendResult(
            big_trend=big_trend,
            small_trend=small_trend,
            big_stable=big_stable,
            small_oscillating=small_osc,
            tube_breakout=tube_bk,
            tube_breakout_direction=tube_dir,
            big_has_color=big_color,
            small_has_color=small_color,
            m30_macd_above_zero=m30_macd_above_zero,
            m5_macd_above_signal=m5_macd_above_signal,
        )

    def should_add_to_trend(self, trend: TrendResult) -> bool:
        """
        是否应该加仓趋势单
        条件：大趋势稳定 + 小趋势同向且非震荡 + 无管壁反向突破
        """
        if trend.big_trend == TrendDirection.WEAK:
            return False
        if trend.small_trend != trend.big_trend:
            return False
        if trend.small_oscillating:
            return False
        if trend.tube_breakout:
            # 管壁突破方向与大趋势相反 → 不加仓
            if trend.tube_breakout_direction != trend.big_trend:
                return False
        return True

    def should_reduce_trend(self, trend: TrendResult) -> bool:
        """是否应该减仓（小趋势弱化/震荡）"""
        return trend.small_oscillating or trend.small_trend == TrendDirection.WEAK

    def should_reverse_trend(self, trend: TrendResult) -> bool:
        """
        是否应该反转做单方向（大趋势确认反转）
        大趋势必须稳定地变为相反方向
        """
        return (
            trend.big_stable
            and trend.big_trend != TrendDirection.WEAK
        )

    def should_trigger_seal_team(self, trend: TrendResult) -> bool:
        """是否触发海豹突击队（管壁突破 + 有浮亏趋势仓）"""
        return trend.tube_breakout
