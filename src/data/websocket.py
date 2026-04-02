"""WebSocket 实时价格模块 - 支持多交易对"""

import asyncio
import json
from typing import Callable, Dict, Optional
import aiohttp
from datetime import datetime

from src.utils.logger import logger


class MultiWebSocket:
    """多交易对 WebSocket 管理器"""
    
    def __init__(self, symbols: list[str]):
        """
        初始化
        
        Args:
            symbols: 交易对列表 ["BTC/USDT", "ETH/USDT", ...]
        """
        # 转换为小写并去掉斜杠: BTC/USDT -> btcusdt
        self.symbols = [s.replace("/", "").lower() for s in symbols]
        self.ws_url = "wss://stream.binance.com:9443/stream"
        
        self.ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self.session: Optional[aiohttp.ClientSession] = None
        self.is_running = False
        self.reconnect_interval = 5
        
        # 价格缓存: {symbol: price}
        self.latest_prices: Dict[str, float] = {s: 0.0 for s in self.symbols}
        self.price_callbacks: list[Callable] = []
        
        logger.info(f"MultiWebSocket 初始化 - 交易对: {self.symbols}")
    
    async def connect(self):
        """建立 WebSocket 连接"""
        try:
            # 构建多流订阅 URL
            streams = "/".join([f"{s}@ticker" for s in self.symbols])
            self.ws_url = f"wss://stream.binance.com:9443/stream?streams={streams}"
            
            self.session = aiohttp.ClientSession()
            self.ws = await self.session.ws_connect(self.ws_url)
            self.is_running = True
            logger.info(f"✅ MultiWebSocket 连接成功 - {len(self.symbols)} 个交易对")
            
        except Exception as e:
            logger.error(f"❌ MultiWebSocket 连接失败: {e}")
            await self.reconnect()
    
    async def reconnect(self):
        """重连机制"""
        logger.info(f"⏳ {self.reconnect_interval}秒后尝试重连...")
        await asyncio.sleep(self.reconnect_interval)
        await self.connect()
    
    async def subscribe_price(self, callback: Callable):
        """订阅价格更新"""
        self.price_callbacks.append(callback)
    
    async def listen(self):
        """监听多交易对消息"""
        if not self.ws:
            await self.connect()
        
        try:
            async for msg in self.ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    await self._handle_message(data)
                    
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error(f"WebSocket 错误: {self.ws.exception()}")
                    break
                    
                elif msg.type == aiohttp.WSMsgType.CLOSED:
                    logger.warning("WebSocket 连接关闭，尝试重连...")
                    await self.reconnect()
                    break
                    
        except Exception as e:
            logger.error(f"监听消息失败: {e}")
            await self.reconnect()
    
    async def _handle_message(self, data: dict):
        """处理消息"""
        try:
            # Binance 嵌套格式: {"stream": "btcusdt@ticker", "data": {...}}
            stream_data = data.get("data", {})
            
            # 解析交易对
            stream_name = data.get("stream", "")
            symbol = stream_name.replace("@ticker", "").upper()
            
            # 提取价格
            price = float(stream_data.get("c", 0))  # 当前价格
            volume = float(stream_data.get("v", 0))  # 成交量
            price_change = float(stream_data.get("P", 0))  # 涨跌幅
            
            if price > 0:
                self.latest_prices[symbol] = price
                
                price_info = {
                    'symbol': symbol,
                    'price': price,
                    'volume': volume,
                    'price_change': price_change,
                    'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                }
                
                logger.debug(f"实时价格: {symbol} = {price}")
                
                # 触发回调
                for callback in self.price_callbacks:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(price_info)
                    else:
                        callback(price_info)
                        
        except Exception as e:
            logger.error(f"处理消息失败: {e}")
    
    async def close(self):
        """关闭连接"""
        self.is_running = False
        if self.ws:
            await self.ws.close()
        if self.session:
            await self.session.close()
        logger.info("MultiWebSocket 连接已关闭")
    
    def get_price(self, symbol: str) -> float:
        """获取指定交易对价格"""
        sym = symbol.replace("/", "").upper()
        return self.latest_prices.get(sym, 0.0)
    
    def get_all_prices(self) -> Dict[str, float]:
        """获取所有交易对价格"""
        return self.latest_prices.copy()


# 向后兼容：保留单交易对接口
class BinanceWebSocket:
    """单交易对 WebSocket（向后兼容）"""
    
    def __init__(self, symbol: str = "btcusdt"):
        self.symbol = symbol.lower()
        self._multi_ws: Optional[MultiWebSocket] = None
    
    async def connect(self):
        # 懒加载：实际使用 MultiWebSocket
        self._multi_ws = MultiWebSocket([self.symbol.replace("/", "").upper()])
        await self._multi_ws.connect()
    
    async def listen(self):
        if self._multi_ws:
            await self._multi_ws.listen()
    
    async def close(self):
        if self._multi_ws:
            await self._multi_ws.close()
    
    def get_latest_price(self) -> float:
        if self._multi_ws:
            return self._multi_ws.get_price(self.symbol.upper())
        return 0.0


class MockWebSocket:
    """模拟 WebSocket（测试用）"""
    
    def __init__(self, symbols: list = None):
        self.symbols = symbols or ["BTCUSDT"]
        self.latest_prices: Dict[str, float] = {s: 0.0 for s in self.symbols}
        self.is_running = False
    
    async def connect(self):
        self.is_running = True
        logger.info(f"MockWebSocket 已启动 - {self.symbols}")
    
    async def listen(self):
        while self.is_running:
            await asyncio.sleep(1)
    
    async def close(self):
        self.is_running = False
    
    def get_latest_price(self, symbol: str = None) -> float:
        if symbol:
            return self.latest_prices.get(symbol.upper(), 0.0)
        return list(self.latest_prices.values())[0] if self.latest_prices else 0.0