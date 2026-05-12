"""
策略基类
所有策略必须实现 generate_signal() 方法，返回统一的 SignalResult
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
import pandas as pd


class Signal(Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass
class SignalResult:
    """策略信号结果"""
    signal: Signal           # BUY/SELL/HOLD
    score: float           # 得分 -1.0 ~ 1.0
    confidence: float      # 置信度 0.0 ~ 1.0
    metadata: dict         # 附加信息（指标值等）

    def __post_init__(self):
        if isinstance(self.signal, str):
            self.signal = Signal(self.signal.upper())

    def to_dict(self) -> dict:
        return {
            'signal': self.signal.value,
            'score': round(self.score, 4),
            'confidence': round(self.confidence, 4),
            'metadata': self.metadata
        }


class BaseStrategy(ABC):
    """策略基类"""

    name: str = "BaseStrategy"

    @abstractmethod
    def generate_signal(self, df: pd.DataFrame) -> SignalResult:
        """
        生成交易信号

        Args:
            df: K线数据，包含 high/low/close/open/volumes 列

        Returns:
            SignalResult: 信号、得分、置信度
        """
        pass

    def get_required_columns(self) -> list:
        """返回策略需要的列名"""
        return ['close']
