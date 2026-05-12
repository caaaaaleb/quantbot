"""行情数据模块 - 市场数据获取"""

import ccxt
from typing import Dict, Any, List, Optional
from datetime import datetime

from src.utils.logger import logger


class MarketData:
    """行情数据获取器"""

    def __init__(self, exchange: ccxt.Exchange):
        self.exchange = exchange
        logger.info("MarketData 初始化成功")

    def get_all_tickers(self) -> Dict[str, Any]:
        """
        获取所有交易对实时行情

        Returns:
            dict: 所有交易对的行情数据
        """
        try:
            tickers = self.exchange.fetch_tickers()
            result = {}
            for symbol, data in tickers.items():
                # 只保留 USDT 交易对
                if "/USDT" in symbol:
                    result[symbol] = {
                        "symbol": symbol,
                        "last": data.get("last", 0),
                        "high": data.get("high", 0),
                        "low": data.get("low", 0),
                        "open": data.get("open", 0),
                        "close": data.get("close", 0),
                        "volume": data.get("baseVolume", 0),
                        "quote_volume": data.get("quoteVolume", 0),
                        "change": data.get("percentage", 0),
                        "change_abs": data.get("change", 0),
                        "timestamp": data.get("timestamp", 0),
                        "datetime": data.get("datetime", ""),
                    }
            logger.info(f"获取全市场行情: {len(result)} 个交易对")
            return result
        except Exception as e:
            logger.error(f"获取全市场行情失败: {e}")
            return {}

    def get_ticker(self, symbol: str) -> Dict[str, Any]:
        """
        获取指定交易对行情

        Args:
            symbol: 交易对，如 "BTC/USDT"

        Returns:
            dict: 交易对行情数据
        """
        try:
            data = self.exchange.fetch_ticker(symbol)
            return {
                "symbol": symbol,
                "last": data.get("last", 0),
                "high": data.get("high", 0),
                "low": data.get("low", 0),
                "open": data.get("open", 0),
                "close": data.get("close", 0),
                "volume": data.get("baseVolume", 0),
                "quote_volume": data.get("quoteVolume", 0),
                "change": data.get("percentage", 0),
                "change_abs": data.get("change", 0),
                "bid": data.get("bid", 0),
                "ask": data.get("ask", 0),
                "timestamp": data.get("timestamp", 0),
                "datetime": data.get("datetime", ""),
            }
        except Exception as e:
            logger.error(f"获取 {symbol} 行情失败: {e}")
            return {"error": str(e)}

    def get_top_gainers(self, limit: int = 20) -> List[Dict[str, Any]]:
        """
        获取涨幅榜

        Args:
            limit: 返回数量

        Returns:
            list: 涨幅最大的交易对列表
        """
        tickers = self.get_all_tickers()
        if not tickers:
            return []

        # 按涨幅排序
        sorted_tickers = sorted(
            tickers.values(),
            key=lambda x: x.get("change", 0) or 0,
            reverse=True
        )

        result = []
        for t in sorted_tickers[:limit]:
            # 过滤掉成交量太小的（可能是垃圾币）
            if t.get("quote_volume", 0) > 100000:  # 10万U以上
                result.append(t)

        logger.info(f"涨幅榜: 获取 {len(result)} 个交易对")
        return result

    def get_top_losers(self, limit: int = 20) -> List[Dict[str, Any]]:
        """
        获取跌幅榜

        Args:
            limit: 返回数量

        Returns:
            list: 跌幅最大的交易对列表
        """
        tickers = self.get_all_tickers()
        if not tickers:
            return []

        # 按跌幅排序（从小到大）
        sorted_tickers = sorted(
            tickers.values(),
            key=lambda x: x.get("change", 0) or 0
        )

        result = []
        for t in sorted_tickers[:limit]:
            # 过滤掉成交量太小的
            if t.get("quote_volume", 0) > 100000:
                result.append(t)

        logger.info(f"跌幅榜: 获取 {len(result)} 个交易对")
        return result

    def get_klines(
        self,
        symbol: str,
        timeframe: str = "1h",
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        获取K线数据

        Args:
            symbol: 交易对
            timeframe: 时间周期 (1m, 5m, 15m, 1h, 4h, 1d, 1w)
            limit: K线数量

        Returns:
            list: K线数据列表
        """
        try:
            ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            result = []
            for candle in ohlcv:
                result.append({
                    "timestamp": candle[0],
                    "datetime": datetime.fromtimestamp(candle[0] / 1000).strftime("%Y-%m-%d %H:%M:%S"),
                    "open": candle[1],
                    "high": candle[2],
                    "low": candle[3],
                    "close": candle[4],
                    "volume": candle[5],
                })
            logger.info(f"K线: {symbol} {timeframe} 获取 {len(result)} 根")
            return result
        except Exception as e:
            logger.error(f"获取 {symbol} K线失败: {e}")
            return []

    def get_order_book(
        self,
        symbol: str,
        limit: int = 20
    ) -> Dict[str, Any]:
        """
        获取订单簿（深度）

        Args:
            symbol: 交易对
            limit: 深度数量

        Returns:
            dict: 买卖深度
        """
        try:
            orderbook = self.exchange.fetch_order_book(symbol, limit)
            return {
                "symbol": symbol,
                "bids": orderbook.get("bids", [])[:limit],
                "asks": orderbook.get("asks", [])[:limit],
                "timestamp": orderbook.get("timestamp", 0),
            }
        except Exception as e:
            logger.error(f"获取 {symbol} 深度失败: {e}")
            return {"error": str(e)}

    def get_market_overview(self) -> Dict[str, Any]:
        """
        获取市场总览

        Returns:
            dict: 市场概览数据
        """
        tickers = self.get_all_tickers()
        if not tickers:
            return {"error": "无法获取市场数据"}

        total_volume = sum(t.get("quote_volume", 0) for t in tickers.values())
        gainers = len([t for t in tickers.values() if (t.get("change", 0) or 0) > 0])
        losers = len([t for t in tickers.values() if (t.get("change", 0) or 0) < 0])

        return {
            "total_symbols": len(tickers),
            "gainers": gainers,
            "losers": losers,
            "neutral": len(tickers) - gainers - losers,
            "total_volume_24h": total_volume,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    def get_recent_trades(self, symbol: str, limit: int = 50) -> List[Dict[str, Any]]:
        """
        获取近期成交

        Args:
            symbol: 交易对
            limit: 成交数量

        Returns:
            list: 近期成交列表
        """
        try:
            trades = self.exchange.fetch_trades(symbol, limit=limit)
            result = []
            for t in trades:
                result.append({
                    "id": t.get("id", ""),
                    "price": t.get("price", 0),
                    "amount": t.get("amount", 0),
                    "side": t.get("side", ""),
                    "timestamp": t.get("timestamp", 0),
                    "datetime": t.get("datetime", ""),
                })
            return result
        except Exception as e:
            logger.error(f"获取 {symbol} 成交失败: {e}")
            return []
