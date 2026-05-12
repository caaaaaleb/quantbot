"""
多策略融合路由器
根据市场状态动态加权融合各策略信号
"""

from typing import Dict, Optional, Any
import pandas as pd

from src.strategy.base import BaseStrategy, SignalResult, Signal
from src.strategy.market_regime import MarketRegime, MarketRegimeDetector
from src.strategy.multi_factor import MultiFactorStrategy
from src.strategy.rsi_bollinger import RSIBollingerStrategy
from src.strategy.volume_momentum import VolumeMomentumStrategy
from src.strategy.funding_rate import FundingRateStrategy


class StrategyRouter:
    """
    多策略融合路由器

    使用方式:
        router = StrategyRouter()
        result = router.generate_signal(df)

    各市场状态下的权重分配:
        TREND:    MA 为主 (0.6), 资金费率 (0.3)
        SIDEWAYS: RSI+BB 为主 (0.5), 成交量 (0.3)
        HIGH_VOL: 成交量为主 (0.4), MA (0.3), 轻仓
    """

    def __init__(
        self,
        regime_detector: Optional[MarketRegimeDetector] = None,
        enable_regime_detection: bool = True,
        regime_weights: Optional[Dict[str, Dict[str, float]]] = None
    ):
        # 初始化各策略
        self.strategies: Dict[str, BaseStrategy] = {
            'ma': MultiFactorStrategy(),           # 趋势策略（MA 交叉）
            'rsi_bb': RSIBollingerStrategy(),     # 均值回归
            'volume': VolumeMomentumStrategy(),    # 成交量动量
            'funding': FundingRateStrategy(),     # 资金费率
        }

        self.funding_strategy_enabled = True

        # 市场状态检测器
        self.regime_detector = regime_detector or MarketRegimeDetector()
        self.enable_regime_detection = enable_regime_detection
        self._current_regime = MarketRegime.SIDEWAYS

        # 各市场状态下的策略权重（优先使用配置传入的权重）
        if regime_weights:
            # 将配置中的字符串键转换为 MarketRegime 枚举
            self.regime_weights = {
                MarketRegime(k.lower()): v
                for k, v in regime_weights.items()
            }
        else:
            self.regime_weights = {
                MarketRegime.TREND: {
                    'ma': 0.6,
                    'rsi_bb': 0.05,
                    'volume': 0.15,
                    'funding': 0.2
                },
                MarketRegime.SIDEWAYS: {
                    'ma': 0.1,
                    'rsi_bb': 0.5,
                    'volume': 0.3,
                    'funding': 0.1
                },
                MarketRegime.HIGH_VOL: {
                    'ma': 0.25,
                    'rsi_bb': 0.25,
                    'volume': 0.4,
                    'funding': 0.1
                }
            }

    def generate_signal(self, df: pd.DataFrame, funding_rate: float = None) -> SignalResult:
        """
        生成融合信号

        Args:
            df: K线数据
            funding_rate: 资金费率（可选，传递给 MA 策略）

        Returns:
            SignalResult: 统一信号结果（metadata 中含各策略详情和权重）
        """
        # 检测市场状态（只检测一次，避免重复计算 ADX/ATR）
        if self.enable_regime_detection:
            self._current_regime = self.regime_detector.detect(df)
        regime = self._current_regime

        # 获取当前状态的权重
        weights = self.regime_weights[regime]

        # 各策略独立计算
        strategy_results: Dict[str, SignalResult] = {}
        weighted_score = 0.0
        total_weight = 0.0

        for name, strategy in self.strategies.items():
            # MA 策略需要 funding_rate，其他策略忽略
            if name == 'ma' and funding_rate is not None:
                result = strategy.generate_signal(df, funding_rate=funding_rate)
            else:
                result = strategy.generate_signal(df)
            strategy_results[name] = result

            w = weights.get(name, 0.0)
            weighted_score += result.score * w
            total_weight += w

        # 归一化
        if total_weight > 0:
            normalized_score = weighted_score / total_weight
        else:
            normalized_score = 0.0

        # 置信度：取各策略置信度的加权平均
        confidence = sum(
            strategy_results[name].confidence * weights.get(name, 0)
            for name in strategy_results
        ) / total_weight if total_weight > 0 else 0.0

        # 高波动市场降低置信度
        if regime == MarketRegime.HIGH_VOL:
            confidence *= 0.7

        # 最终信号
        threshold = 0.15
        if normalized_score > threshold:
            final_signal = Signal.BUY
        elif normalized_score < -threshold:
            final_signal = Signal.SELL
        else:
            final_signal = Signal.HOLD

        return SignalResult(
            signal=final_signal,
            score=round(normalized_score, 4),
            confidence=round(confidence, 4),
            metadata={
                'regime': regime.value,
                'strategies': {
                    name: result.to_dict()
                    for name, result in strategy_results.items()
                },
                'weights': {name: weights.get(name, 0) for name in self.strategies.keys()}
            }
        )

    def get_current_regime(self) -> MarketRegime:
        """获取当前市场状态"""
        return self._current_regime

    def set_regime(self, regime: MarketRegime):
        """手动设置市场状态（用于回测）"""
        self._current_regime = regime
