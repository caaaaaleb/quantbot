"""
技术指标计算模块
提供 RSI、ADX、布林带、ATR 等标准指标
"""

import numpy as np
import pandas as pd
from typing import Tuple


def calculate_rsi(prices: pd.Series, period: int = 14) -> float:
    """
    计算 RSI (Relative Strength Index)
    RSI > 70 超买，RSI < 30 超卖
    """
    if len(prices) < period + 1:
        return 50.0  # 数据不足返回中性值

    deltas = prices.diff()
    gains = deltas.where(deltas > 0, 0.0)
    losses = -deltas.where(deltas < 0, 0.0)

    avg_gain = gains.rolling(window=period).mean()
    avg_loss = losses.rolling(window=period).mean()

    # 使用 Wilder 平滑
    if len(avg_gain) < period:
        return 50.0

    rs = avg_gain / (avg_loss + 1e-10)
    rsi = 100 - (100 / (1 + rs))

    return float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else 50.0


def calculate_bollinger_bands(
    prices: pd.Series,
    period: int = 20,
    std_dev: float = 2.0
) -> dict:
    """
    计算布林带
    返回: {upper, middle, lower, deviation}
    deviation: 价格偏离布林带的百分比
    """
    if len(prices) < period:
        middle = prices.mean()
        std = prices.std()
        return {
            'upper': float(middle + std_dev * std),
            'middle': float(middle),
            'lower': float(middle - std_dev * std),
            'deviation': 0.0,
            'bandwidth': 0.0
        }

    middle = prices.rolling(window=period).mean()
    std = prices.rolling(window=period).std()

    upper = middle + std_dev * std
    lower = middle - std_dev * std

    latest = prices.iloc[-1]
    mid = middle.iloc[-1]
    bandwidth = ((upper.iloc[-1] - lower.iloc[-1]) / mid) if mid != 0 else 0.0

    # 计算偏离度：正数 = 高于中轨，负数 = 低于中轨
    deviation = ((latest - mid) / mid) if mid != 0 else 0.0

    return {
        'upper': float(upper.iloc[-1]),
        'middle': float(mid),
        'lower': float(lower.iloc[-1]),
        'deviation': float(deviation),
        'bandwidth': float(bandwidth)
    }


def calculate_adx(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14
) -> float:
    """
    计算 ADX (Average Directional Index)
    ADX > 25 表示趋势市场
    ADX < 25 表示盘整/无趋势
    """
    if len(close) < period * 2:
        return 0.0

    # 计算 True Range
    tr1 = high - low
    tr2 = abs(high - close.shift(1))
    tr3 = abs(low - close.shift(1))
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    # 计算 Directional Movement
    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    # Wilder 平滑
    atr = tr.rolling(window=period).mean()
    plus_di = (plus_dm.rolling(window=period).mean() / atr) * 100
    minus_di = (minus_dm.rolling(window=period).mean() / atr) * 100

    # DX = |+DI - -DI| / |+DI + -DI| * 100
    dx = (abs(plus_di - minus_di) / (plus_di + minus_di + 1e-10)) * 100

    # ADX = DX 的 Wilder 平滑
    adx = dx.rolling(window=period).mean()

    return float(adx.iloc[-1]) if not pd.isna(adx.iloc[-1]) else 0.0


def calculate_atr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14
) -> float:
    """
    计算 ATR (Average True Range)
    ATR% = ATR / price * 100，用于跨标的价格比较
    """
    if len(close) < period + 1:
        return float((high - low).mean())

    tr1 = high - low
    tr2 = abs(high - close.shift(1))
    tr3 = abs(low - close.shift(1))
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr = tr.rolling(window=period).mean()
    return float(atr.iloc[-1]) if not pd.isna(atr.iloc[-1]) else float(tr.mean())


def calculate_atr_percent(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14
) -> float:
    """
    计算 ATR 占价格的百分比，用于波动率比较
    """
    atr = calculate_atr(high, low, close, period)
    price = close.iloc[-1]
    return (atr / price * 100) if price != 0 else 0.0


def calculate_ma(prices: pd.Series, period: int) -> float:
    """计算简单移动平均"""
    if len(prices) < period:
        return float(prices.mean())
    return float(prices.rolling(window=period).mean().iloc[-1])


def calculate_ma_cross(prices: pd.Series, short_period: int = 5, long_period: int = 20) -> dict:
    """
    计算 MA 交叉信号
    返回: {signal, short_ma, long_ma, crossover}
    crossover > 0 金叉，< 0 死叉
    """
    short_ma = calculate_ma(prices, short_period)
    long_ma = calculate_ma(prices, long_period)

    # 计算前一时刻的 MA 判断交叉
    if len(prices) >= long_period + 1:
        prev_short_ma = calculate_ma(prices.iloc[:-1], short_period)
        prev_long_ma = calculate_ma(prices.iloc[:-1], long_period)
        crossover = (short_ma - long_ma) - (prev_short_ma - prev_long_ma)
    else:
        crossover = 0

    return {
        'short_ma': short_ma,
        'long_ma': long_ma,
        'crossover': crossover,
        'spread': (short_ma - long_ma) / long_ma if long_ma != 0 else 0
    }


def calculate_volume_ratio(volumes: pd.Series, period: int = 20) -> float:
    """
    计算成交量比率：当前成交量 / 过去 N 期平均成交量
    > 2.0 放量，< 0.5 缩量
    """
    if len(volumes) < period:
        return 1.0

    avg_volume = volumes.rolling(window=period).mean().iloc[-1]
    current_volume = volumes.iloc[-1]

    return float(current_volume / avg_volume) if avg_volume != 0 else 1.0
