"""
多因子策略模块
继承 BaseStrategy，统一信号接口
"""

import pandas as pd
import numpy as np
from typing import Dict, Any, Optional

from src.strategy.base import BaseStrategy, SignalResult, Signal
from src.strategy.market_regime import MarketRegimeDetector, MarketRegime
from src.data.indicators import calculate_ma_cross, calculate_volume_ratio
from src.utils.logger import logger


class MultiFactorStrategy(BaseStrategy):
    """
    多因子策略（MA 交叉 + 资金费率 + 成交量动量）

    适用于趋势市场（TREND regime）
    """

    name = "MultiFactor"

    def __init__(
        self,
        ma_short: int = 5,
        ma_long: int = 20,
        weights: Optional[Dict[str, float]] = None
    ):
        """
        初始化策略

        Args:
            ma_short: 短期均线周期
            ma_long: 长期均线周期
            weights: 因子权重
        """
        self.ma_short = ma_short
        self.ma_long = ma_long

        # 默认权重
        self.weights = weights or {
            'ma_signal': 0.5,
            'funding_rate': 0.3,
            'volume_spike': 0.2
        }

        # 市场状态检测器（各币共享同一个 detector 实例，状态各自独立）
        self.regime_detector = MarketRegimeDetector(
            adx_period=14,
            adx_trend_threshold=25.0,
            atr_low_threshold=3.0,
            atr_high_threshold=5.0,
        )

        logger.info(
            f"策略初始化 - MA({ma_short}/{ma_long}) | "
            f"权重: MA={self.weights['ma_signal']}, "
            f"FR={self.weights['funding_rate']}, "
            f"Vol={self.weights['volume_spike']}"
        )

    def calculate_ma_signal(self, df: pd.DataFrame) -> tuple[float, str]:
        """
        计算MA信号
        """
        ma_cross = calculate_ma_cross(df['close'], self.ma_short, self.ma_long)

        ma_short_ma = ma_cross['short_ma']
        ma_long_ma = ma_cross['long_ma']
        crossover = ma_cross['crossover']
        spread = ma_cross['spread']

        # 金叉/死叉信号
        if crossover > 0:
            signal = 1.0
            direction = "金叉"
        elif crossover < 0:
            signal = -1.0
            direction = "死叉"
        else:
            # 无交叉时：spread 必须超过 1% 才认为有趋势，否则视为震荡
            if abs(spread) < 0.01:
                signal = 0.0
                direction = "震荡"
            else:
                signal = np.clip(spread * 100, -1, 1)
                direction = "多头" if signal > 0 else "空头"

        logger.debug(
            f"MA信号: MA{self.ma_short}={ma_short_ma:.2f}, "
            f"MA{self.ma_long}={ma_long_ma:.2f}, "
            f"信号={signal:.3f} ({direction})"
        )

        return signal, direction

    def calculate_funding_rate_signal(self, funding_rate: Optional[float] = None) -> tuple[float, str]:
        """
        计算资金费率信号

        Args:
            funding_rate: 资金费率（从 Bitget API 获取），None 表示无数据
        """
        if funding_rate is None:
            return 0.0, "无资金费率数据（仅在合约交易中可用）"

        # 资金费率解读
        # 正费率 -> 多头付费 -> 做空有利 → signal 偏负
        # 负费率 -> 空头付费 -> 做多有利 → signal 偏正
        # Bitget 常见费率范围 ±0.0001~±0.001，乘以 100 转百分比再 clip
        signal = np.clip(-funding_rate * 100, -1, 1)

        if funding_rate > 0.0001:
            desc = f"正费率({funding_rate*100:.4f}%) -> 偏空"
        elif funding_rate < -0.0001:
            desc = f"负费率({funding_rate*100:.4f}%) -> 偏多"
        else:
            desc = f"中性费率({funding_rate*100:.4f}%)"

        logger.debug(f"资金费率信号: {desc}, 信号={signal:.3f}")

        return signal, desc

    def calculate_volume_signal(self, df: pd.DataFrame) -> tuple[float, str]:
        """
        计算成交量异动信号
        """
        vol_ratio = calculate_volume_ratio(df.get('volume', pd.Series([1]*len(df))), 20)

        if vol_ratio > 2.0:
            signal = 0.8
            desc = f"成交量爆发 ({vol_ratio:.2f}x)"
        elif vol_ratio > 1.5:
            signal = 0.5
            desc = f"成交量放大 ({vol_ratio:.2f}x)"
        elif vol_ratio < 0.5:
            signal = -0.3
            desc = f"成交量萎缩 ({vol_ratio:.2f}x)"
        else:
            signal = 0.0
            desc = f"成交量正常 ({vol_ratio:.2f}x)"

        logger.debug(f"成交量信号: {desc}, 信号={signal:.3f}")

        return signal, desc

    def generate_signal(
        self,
        df: pd.DataFrame,
        funding_rate: Optional[float] = None
    ) -> SignalResult:
        """
        生成交易信号（实现基类接口）
        """
        if len(df) < max(self.ma_long, 21):
            return SignalResult(
                signal=Signal.HOLD,
                score=0.0,
                confidence=0.0,
                metadata={'reason': 'insufficient_data'}
            )

        # ── 1. 检测市场状态 ─────────────────────────────────────────
        regime = self.regime_detector.detect(df)
        regime_details = self.regime_detector.detect_with_details(df)
        adx = regime_details['adx']
        atr_pct = regime_details['atr_pct']

        # ── 2. 计算各因子信号 ───────────────────────────────────────
        ma_signal, ma_desc = self.calculate_ma_signal(df)
        fr_signal, fr_desc = self.calculate_funding_rate_signal(funding_rate)
        vol_signal, vol_desc = self.calculate_volume_signal(df)

        # ── 3. 根据市场状态调整信号强度和门槛 ─────────────────────────
        # 非 TREND 市场：收紧门槛，提高确认要求
        regime_multipliers = {
            MarketRegime.TREND:      {'threshold': 0.3,  'ma_boost': 0.0,   'confidence_boost': 0.0},
            MarketRegime.SIDEWAYS:   {'threshold': 0.45,  'ma_boost': -0.1, 'confidence_boost': -0.1},
            MarketRegime.HIGH_VOL:   {'threshold': 0.6,   'ma_boost': -0.2, 'confidence_boost': -0.15},
        }
        cfg = regime_multipliers[regime]
        threshold = cfg['threshold']

        # SIDEWAYS/HIGH_VOL 时：MA 信号打折，避免假突破
        if regime != MarketRegime.TREND:
            ma_signal_adjusted = ma_signal * (1.0 + cfg['ma_boost'])
        else:
            ma_signal_adjusted = ma_signal

        # 加权综合信号
        total_signal = (
            ma_signal_adjusted * self.weights['ma_signal'] +
            fr_signal * self.weights['funding_rate'] +
            vol_signal * self.weights['volume_spike']
        )

        # 置信度
        if abs(ma_signal) >= 1.0:
            confidence = 0.9 + cfg['confidence_boost']
        else:
            confidence = min(abs(total_signal) + 0.2 + cfg['confidence_boost'], 0.8)

        # 确定交易信号
        if total_signal > threshold:
            signal = Signal.BUY
        elif total_signal < -threshold:
            signal = Signal.SELL
        else:
            signal = Signal.HOLD

        logger.info(
            f"策略信号: {signal.value} | "
            f"强度={total_signal:.3f} | "
            f"价格={df['close'].iloc[-1]:.8g} | "
            f"regime={regime.value} | "
            f"ADX={adx} ATR%={atr_pct:.2f}%"
        )

        return SignalResult(
            signal=signal,
            score=round(total_signal, 4),
            confidence=round(confidence, 4),
            metadata={
                'factors': {
                    'ma': {'signal': round(ma_signal, 3), 'desc': ma_desc},
                    'funding_rate': {'signal': round(fr_signal, 3), 'desc': fr_desc},
                    'volume': {'signal': round(vol_signal, 3), 'desc': vol_desc}
                },
                'price': df['close'].iloc[-1],
                'regime': regime.value,
                'adx': adx,
                'atr_pct': atr_pct,
            }
        )

    def get_required_columns(self) -> list:
        return ['close', 'volume']
