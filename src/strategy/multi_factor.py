"""多因子策略模块"""

import pandas as pd
import numpy as np
from typing import Dict, Any, Tuple
from enum import Enum

from src.utils.logger import logger


class Signal(Enum):
    """交易信号枚举"""
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class MultiFactorStrategy:
    """多因子策略"""
    
    def __init__(
        self,
        ma_short: int = 5,
        ma_long: int = 20,
        weights: Dict[str, float] = None
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
        
        logger.info(
            f"策略初始化 - MA({ma_short}/{ma_long}) | "
            f"权重: MA={self.weights['ma_signal']}, "
            f"FR={self.weights['funding_rate']}, "
            f"Vol={self.weights['volume_spike']}"
        )
    
    def calculate_ma_signal(self, df: pd.DataFrame) -> Tuple[float, str]:
        """
        计算MA信号
        
        Args:
            df: K线数据
            
        Returns:
            (信号强度, 信号方向)
        """
        # 计算均线
        ma_short = df['close'].rolling(window=self.ma_short).mean()
        ma_long = df['close'].rolling(window=self.ma_long).mean()
        
        # 最新值
        ma_short_latest = ma_short.iloc[-1]
        ma_long_latest = ma_long.iloc[-1]
        ma_short_prev = ma_short.iloc[-2]
        ma_long_prev = ma_long.iloc[-2]
        
        # 计算金叉/死叉
        cross_up = (ma_short_prev <= ma_long_prev) and (ma_short_latest > ma_long_latest)
        cross_down = (ma_short_prev >= ma_long_prev) and (ma_short_latest < ma_long_latest)
        
        # 计算信号强度 (-1 到 1)
        if cross_up:
            signal = 1.0
            direction = "金叉"
        elif cross_down:
            signal = -1.0
            direction = "死叉"
        else:
            # 趋势强度
            ratio = (ma_short_latest - ma_long_latest) / ma_long_latest
            signal = np.clip(ratio * 100, -1, 1)  # 放大并限制范围
            direction = "多头" if signal > 0 else "空头"
        
        logger.debug(
            f"MA信号: MA{self.ma_short}={ma_short_latest:.2f}, "
            f"MA{self.ma_long}={ma_long_latest:.2f}, "
            f"信号={signal:.3f} ({direction})"
        )
        
        return signal, direction
    
    def calculate_funding_rate_signal(self, funding_rate: float = None) -> Tuple[float, str]:
        """
        计算资金费率信号（模拟数据）
        
        Args:
            funding_rate: 资金费率（可选，默认模拟）
            
        Returns:
            (信号强度, 信号描述)
        """
        # 模拟资金费率（实际应从API获取）
        if funding_rate is None:
            # 模拟：正态分布，均值0，标准差0.01%
            funding_rate = np.random.normal(0, 0.0001)
        
        # 资金费率解读
        # 正费率 -> 多头付费 -> 做空有利
        # 负费率 -> 空头付费 -> 做多有利
        signal = -funding_rate * 1000  # 放大信号
        signal = np.clip(signal, -1, 1)
        
        if funding_rate > 0.0001:
            desc = f"正费率({funding_rate*100:.4f}%) -> 偏空"
        elif funding_rate < -0.0001:
            desc = f"负费率({funding_rate*100:.4f}%) -> 偏多"
        else:
            desc = f"中性费率({funding_rate*100:.4f}%)"
        
        logger.debug(f"资金费率信号: {desc}, 信号={signal:.3f}")
        
        return signal, desc
    
    def calculate_volume_signal(self, df: pd.DataFrame) -> Tuple[float, str]:
        """
        计算成交量异动信号
        
        Args:
            df: K线数据
            
        Returns:
            (信号强度, 信号描述)
        """
        # 计算成交量MA
        vol_ma = df['volume'].rolling(window=20).mean()
        vol_latest = df['volume'].iloc[-1]
        vol_ma_latest = vol_ma.iloc[-1]
        
        # 成交量倍数
        vol_ratio = vol_latest / vol_ma_latest if vol_ma_latest > 0 else 1
        
        # 异动判断
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
        funding_rate: float = None
    ) -> Dict[str, Any]:
        """
        生成交易信号
        
        Args:
            df: K线数据
            funding_rate: 资金费率（可选）
            
        Returns:
            dict: 信号详情
        """
        # 计算各因子信号
        ma_signal, ma_desc = self.calculate_ma_signal(df)
        fr_signal, fr_desc = self.calculate_funding_rate_signal(funding_rate)
        vol_signal, vol_desc = self.calculate_volume_signal(df)
        
        # 加权综合信号
        total_signal = (
            ma_signal * self.weights['ma_signal'] +
            fr_signal * self.weights['funding_rate'] +
            vol_signal * self.weights['volume_spike']
        )
        
        # 确定交易信号
        if total_signal > 0.3:
            signal = Signal.BUY
        elif total_signal < -0.3:
            signal = Signal.SELL
        else:
            signal = Signal.HOLD
        
        result = {
            'signal': signal.value,
            'strength': total_signal,
            'factors': {
                'ma': {'signal': ma_signal, 'desc': ma_desc},
                'funding_rate': {'signal': fr_signal, 'desc': fr_desc},
                'volume': {'signal': vol_signal, 'desc': vol_desc}
            },
            'price': df['close'].iloc[-1],
            'timestamp': df['datetime'].iloc[-1]
        }
        
        logger.info(
            f"策略信号: {signal.value} | "
            f"强度={total_signal:.3f} | "
            f"价格={result['price']:.2f}"
        )
        
        return result