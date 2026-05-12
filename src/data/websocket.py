"""WebSocket 实时价格模块 - 支持多交易对 (Bitget)"""

import asyncio
import json
import os
from typing import Callable, Dict, Optional
import aiohttp
from datetime import datetime

from src.utils.logger import logger

try:
    from aiohttp_socks import ProxyConnector
    _SOCKS_AVAILABLE = True
except ImportError:
    _SOCKS_AVAILABLE = False


def _create_connector():
    """创建 aiohttp connector（支持 SOCKS5 代理）"""
    proxy = os.getenv("HTTP_PROXY", "socks5://127.0.0.1:10808")
    if proxy and _SOCKS_AVAILABLE:
        logger.debug(f"WebSocket 使用代理: {proxy}")
        return ProxyConnector.from_url(proxy)
    return aiohttp.TCPConnector()


class MultiWebSocket:
    """多交易对 WebSocket 管理器 (Bitget V2 WS API)"""

    def __init__(self, symbols: list[str]):
        """
        初始化

        Args:
            symbols: 交易对列表 ["BTC/USDT", "ETH/USDT", ...]
        """
        raw = [s.split(":")[0] for s in symbols]
        self.symbols = [s.replace("/", "").upper() for s in raw]
        self.ws_url = "wss://ws.bitget.com/v2/ws/public"

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
            connector = _create_connector()
            self.session = aiohttp.ClientSession(connector=connector)
            self.ws = await self.session.ws_connect(self.ws_url)
            self.is_running = True

            # 订阅所有交易对的 ticker
            for symbol in self.symbols:
                sub_msg = {
                    "op": "subscribe",
                    "args": [{
                        "instType": "USDT-FUTURES",
                        "channel": "ticker",
                        "instId": symbol,
                    }]
                }
                await self.ws.send_json(sub_msg)
                logger.debug(f"订阅 ticker: {symbol}")

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

                    # Bitget 心跳：响应 ping
                    if data.get("op") == "ping":
                        await self.ws.send_json({"op": "pong"})
                        continue

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
        """处理 Bitget V2 消息"""
        try:
            arg = data.get("arg", {})
            payload = data.get("data", [])

            if not payload or not isinstance(payload, list):
                return

            item = payload[0]
            symbol = arg.get("instId", "")
            if not symbol:
                return

            # 提取价格
            price = float(item.get("lastPr", 0) or 0)
            volume = float(item.get("baseVolume", 0) or 0)
            price_change = float(item.get("change24h", "0") or 0)

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
        sym = symbol.split(":")[0].replace("/", "").upper()
        return self.latest_prices.get(sym, 0.0)

    def get_all_prices(self) -> Dict[str, float]:
        """获取所有交易对价格"""
        return self.latest_prices.copy()


# 向后兼容：保留单交易对接口
class BitgetWebSocket:
    """单交易对 WebSocket（向后兼容）"""

    def __init__(self, symbol: str = "BTCUSDT"):
        self.symbol = symbol.upper()
        self._multi_ws: Optional[MultiWebSocket] = None

    async def connect(self):
        self._multi_ws = MultiWebSocket([self.symbol])
        await self._multi_ws.connect()

    async def listen(self):
        if self._multi_ws:
            await self._multi_ws.listen()

    async def close(self):
        if self._multi_ws:
            await self._multi_ws.close()

    def get_latest_price(self) -> float:
        if self._multi_ws:
            return self._multi_ws.get_price(self.symbol)
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
