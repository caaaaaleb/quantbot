"""数据源模块 - Bitget API 数据获取"""

import time
from typing import Dict, Any, List, Optional
from dataclasses import dataclass
from src.utils.logger import logger


@dataclass
class TickerData:
    """Ticker 数据"""
    last: float
    change_24h: float
    volume_24h: float
    quote_volume: float
    bid: float
    ask: float
    spread: float


@dataclass
class KlineData:
    """K线数据"""
    timestamps: List[int]
    opens: List[float]
    highs: List[float]
    lows: List[float]
    closes: List[float]
    volumes: List[float]


@dataclass
class OrderBookData:
    """订单簿数据"""
    bids: List[float]  # [price, qty]
    asks: List[List[float]]
    imbalance: float


@dataclass
class TradesData:
    """成交数据"""
    buy_volume: float
    sell_volume: float
    taker_buy_ratio: float


class DataSource:
    """Bitget 数据源"""

    def __init__(self, exchange):
        self.exchange = exchange
        # 缓存
        self._tickers_cache: Dict[str, Any] = {}
        self._tickers_cache_time: float = 0
        self._tickers_cache_ttl: float = 10

    def fetch_ticker(self, symbol: str) -> Optional[TickerData]:
        """获取 ticker 数据"""
        try:
            ticker = self.exchange.fetch_ticker(symbol)
            return TickerData(
                last=float(ticker.get("last", 0) or 0),
                change_24h=float(ticker.get("percentage", 0) or 0),
                volume_24h=float(ticker.get("baseVolume", 0) or 0),
                quote_volume=float(ticker.get("quoteVolume", 0) or 0),
                bid=float(ticker.get("bid", 0) or 0),
                ask=float(ticker.get("ask", 0) or 0),
                spread=float(ticker.get("ask", 0) or 0) - float(ticker.get("bid", 0) or 0) if ticker.get("ask") and ticker.get("bid") else 0,
            )
        except Exception as e:
            return None

    def fetch_klines(self, symbol: str, interval: str, limit: int = 100) -> Optional[KlineData]:
        """获取 K 线数据"""
        try:
            ohlcv = self.exchange.fetch_ohlcv(symbol, interval, limit=limit)
            if not ohlcv:
                return None
            return KlineData(
                timestamps=[c[0] for c in ohlcv],
                opens=[float(c[1]) for c in ohlcv],
                highs=[float(c[2]) for c in ohlcv],
                lows=[float(c[3]) for c in ohlcv],
                closes=[float(c[4]) for c in ohlcv],
                volumes=[float(c[5]) for c in ohlcv],
            )
        except Exception as e:
            return None

    def fetch_orderbook(self, symbol: str, limit: int = 5) -> Optional[OrderBookData]:
        """获取订单簿（前 N 档）"""
        try:
            ob = self.exchange.fetch_order_book(symbol, limit=limit)
            bids = ob.get("bids", [])
            asks = ob.get("asks", [])
            bid_vol = sum(float(b[1]) for b in bids)
            ask_vol = sum(float(a[1]) for a in asks)
            total = bid_vol + ask_vol
            imbalance = (bid_vol - ask_vol) / total if total > 0 else 0
            return OrderBookData(
                bids=[[float(b[0]), float(b[1])] for b in bids],
                asks=[[float(a[0]), float(a[1])] for a in asks],
                imbalance=imbalance,
            )
        except Exception as e:
            return None

    def fetch_recent_trades(self, symbol: str, limit: int = 100) -> Optional[TradesData]:
        """获取最近成交，计算 taker buy ratio"""
        try:
            trades = self.exchange.fetch_recent_trades(symbol, limit=limit)
            buy_vol = 0.0
            sell_vol = 0.0
            for t in trades:
                vol = float(t.get("amount", 0) or 0)
                # ccxt trades may have 'side' field as string
                side = t.get("side", "")
                if side == "buy":
                    buy_vol += vol
                elif side == "sell":
                    sell_vol += vol
                else:
                    # Fallback: estimate from taker/maker if available
                    taker = t.get("takerOrMaker", "")
                    if taker == "maker":
                        sell_vol += vol
                    else:
                        buy_vol += vol
            total = buy_vol + sell_vol
            ratio = buy_vol / total if total > 0 else 0.5
            return TradesData(
                buy_volume=buy_vol,
                sell_volume=sell_vol,
                taker_buy_ratio=ratio,
            )
        except Exception as e:
            return None

    def get_usdt_pairs(self) -> List[str]:
        """获取所有 USDT 交易对"""
        try:
            markets = self.exchange.markets
            return [sym for sym in markets if markets[sym].get("quote") == "USDT"
                    and markets[sym].get("active", False)
                    and sym not in ["USDC/USDT", "USDP/USDT", "BUSD/USDT", "USDt/USDT"]]
        except Exception:
            return []

    def _get_tickers(self) -> Dict[str, Any]:
        """获取全市场 tickers（带缓存）"""
        now = time.time()
        if self._tickers_cache and (now - self._tickers_cache_time) < self._tickers_cache_ttl:
            return self._tickers_cache
        try:
            tickers = self.exchange.fetch_tickers()
            self._tickers_cache = tickers
            self._tickers_cache_time = now
            return tickers
        except Exception as e:
            logger.warning(f"获取 tickers 失败: {e}")
            return self._tickers_cache or {}