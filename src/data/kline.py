"""K线数据获取模块"""

import ccxt
import pandas as pd
from typing import Optional, Dict, Any
from datetime import datetime
import time

from src.utils.logger import logger


class KlineFetcher:
    """K线数据获取器"""
    
    def __init__(self, exchange: ccxt.Exchange, symbol: str = "BTC/USDT"):
        """
        初始化K线获取器
        
        Args:
            exchange: ccxt 交易所实例
            symbol: 交易对 (如 BTC/USDT)
        """
        self.exchange = exchange
        self.symbol = symbol
        self.cache: Dict[str, Any] = {}
        self.cache_ttl = 30  # 缓存30秒
        logger.info(f"KlineFetcher 初始化完成 - 交易对: {symbol}")
    
    def fetch_klines(self, timeframe: str = "1m", limit: int = 100) -> pd.DataFrame:
        """
        获取K线数据
        
        Args:
            timeframe: 时间周期 (1m, 5m, 15m, 1h, 4h, 1d)
            limit: 获取数量
            
        Returns:
            DataFrame: K线数据
        """
        cache_key = f"{self.symbol}_{timeframe}_{limit}"
        
        # 检查缓存
        if cache_key in self.cache:
            cached_data, cached_time = self.cache[cache_key]
            if time.time() - cached_time < self.cache_ttl:
                logger.debug(f"使用缓存数据: {cache_key}")
                return cached_data
        
        try:
            logger.info(f"获取K线数据: {self.symbol} {timeframe} x {limit}")
            
            # 获取K线数据
            klines = self.exchange.fetch_ohlcv(
                symbol=self.symbol,
                timeframe=timeframe,
                limit=limit
            )
            
            # 转换為 DataFrame
            df = pd.DataFrame(
                klines,
                columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']
            )
            
            # 转换时间戳
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df['datetime'] = df['timestamp'].dt.strftime('%Y-%m-%d %H:%M:%S')
            
            # 更新缓存
            self.cache[cache_key] = (df, time.time())
            
            logger.info(f"成功获取 {len(df)} 根K线 - 最新价格: {df['close'].iloc[-1]}")
            return df
            
        except Exception as e:
            logger.error(f"获取K线数据失败: {e}")
            raise
    
    def get_latest_price(self) -> float:
        """
        获取最新价格
        
        Returns:
            float: 最新成交价
        """
        try:
            ticker = self.exchange.fetch_ticker(self.symbol)
            price = ticker['last']
            logger.debug(f"最新价格: {self.symbol} = {price}")
            return price
        except Exception as e:
            logger.error(f"获取最新价格失败: {e}")
            raise
    
    def get_ticker(self) -> Dict[str, Any]:
        """
        获取行情数据
        
        Returns:
            dict: 行情信息
        """
        try:
            ticker = self.exchange.fetch_ticker(self.symbol)
            return {
                'symbol': self.symbol,
                'last': ticker['last'],
                'bid': ticker['bid'],
                'ask': ticker['ask'],
                'high': ticker['high'],
                'low': ticker['low'],
                'volume': ticker['baseVolume'],
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
        except Exception as e:
            logger.error(f"获取行情数据失败: {e}")
            raise
    
    def calculate_ma(self, df: pd.DataFrame, period: int) -> pd.Series:
        """
        计算移动平均线
        
        Args:
            df: K线数据
            period: 周期
            
        Returns:
            Series: MA数据
        """
        return df['close'].rolling(window=period).mean()
    
    def clear_cache(self):
        """清空缓存"""
        self.cache.clear()
        logger.info("缓存已清空")