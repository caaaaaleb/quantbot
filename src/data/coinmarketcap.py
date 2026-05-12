"""CoinMarketCap API 客户端 - 补充市场数据"""

import time
import requests
from typing import Dict, Any, List, Optional
from dataclasses import dataclass

from src.utils.logger import logger


@dataclass
class CMCTicker:
    """CMC 单币种数据"""
    cmc_id: int          # CMC coin id
    symbol: str           # BTC
    name: str             # Bitcoin
    price: float
    change_1h: float
    change_24h: float
    change_7d: float
    market_cap: float
    market_cap_dominance: float
    volume_24h: float
    circulating_supply: float
    total_supply: float
    max_supply: float
    ath: float
    atl: float
    rank: int
    # 扩展数据
    fdv: float = 0.0           # 完全稀释估值
    volume_ratio_7d: float = 0.0  # 24h/7d 均量比
    mcap_ratio: float = 0.0      # MCap / FDV


@dataclass
class CMCGlobalMetrics:
    """CMC 全球市场指标"""
    total_market_cap: float
    total_volume_24h: float
    btc_dominance: float
    eth_dominance: float
    altcoin_dominance: float
    market_cap_change_24h: float
    active_cryptocurrencies: int
    # 情绪指标
    fear_greed_index: Optional[int] = None
    fear_greed_classification: str = "neutral"


class CMCClient:
    """
    CoinMarketCap API 客户端

    用途:
    - 获取市值/流通量等交易所订单簿没有的数据
    - 获取全球市场情绪指标
    - 补充流动性/FDV 等风险评估维度
    - 历史波动率（替代缺失数据）
    """

    BASE_URL = "https://pro-api.coinmarketcap.com/v2"

    def __init__(self, api_key: str, cache_ttl: int = 60):
        self.api_key = api_key
        self.cache_ttl = cache_ttl
        self._headers = {"X-CMC_PRO_API_KEY": api_key}
        self._session = requests.Session()
        self._session.headers.update(self._headers)

        # 缓存
        self._ticker_cache: Dict[str, Dict[str, Any]] = {}
        self._ticker_cache_time: float = 0
        self._global_cache: Dict[str, Any] = {}
        self._global_cache_time: float = 0
        self._symbol_map_cache: Dict[str, int] = {}  # BTC -> CMC id

        logger.info(f"CMCClient 初始化完成 (cache_ttl={cache_ttl}s)")

    def _rate_limit_sleep(self):
        """简单 rate limit 保护：每次请求间隔 0.5s"""
        time.sleep(0.5)

    def _get(self, endpoint: str, params: dict = None) -> Optional[dict]:
        """通用 GET 请求"""
        resp = None
        try:
            resp = self._session.get(
                f"{self.BASE_URL}/{endpoint}",
                params=params or {},
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            if resp is not None and resp.status_code == 429:
                logger.warning("CMC API rate limit，30s 后重试")
                time.sleep(30)
                return self._get(endpoint, params)
            logger.error(f"CMC API HTTP 错误 [{resp.status_code}]: {e}")
            return None
        except Exception as e:
            logger.error(f"CMC API 请求失败: {e}")
            return None

    # ── Core Methods ────────────────────────────────────────────────

    def get_ticker(self, symbol: str, convert: str = "USD") -> Optional[CMCTicker]:
        """
        获取单个币种的 CMC 数据 (v2)

        Args:
            symbol: 如 "BTC" 或 "BTC/USDT"
        Returns:
            CMCTicker dataclass 或 None
        """
        sym = symbol.replace("/USDT", "").replace("/USD", "").upper()

        # 检查缓存
        now = time.time()
        if sym in self._ticker_cache and (now - self._ticker_cache_time) < self.cache_ttl:
            return self._cache_to_ticker(sym)

        self._rate_limit_sleep()
        # v2 endpoint: /cryptocurrency/quotes/latest?symbol=BTC
        data = self._get(f"cryptocurrency/quotes/latest", {"symbol": sym, "convert": convert})

        if not data or "data" not in data:
            return None

        # v2 结构: data[id] -> { id, symbol, name, quote: { USD: {...} } }
        coins = data["data"]
        if not coins:
            return None

        coin = list(coins.values())[0]
        self._ticker_cache[sym] = coin
        self._ticker_cache_time = now

        return self._dict_to_ticker(coin)

    def get_tickers(self, symbols: List[str], convert: str = "USD") -> Dict[str, CMCTicker]:
        """
        批量获取多个币种的 CMC 数据 (v2)

        Args:
            symbols: ["BTC", "ETH", "PEPE"]
        Returns:
            {SYMBOL: CMCTicker}
        """
        now = time.time()
        result = {}

        # 缓存命中
        for sym in symbols:
            clean = sym.replace("/USDT", "").replace("/USD", "").upper()
            if clean in self._ticker_cache and (now - self._ticker_cache_time) < self.cache_ttl:
                t = self._cache_to_ticker(clean)
                if t:
                    result[clean] = t

        # 缺失的批量请求
        missing = [s.replace("/USDT", "").replace("/USD", "").upper()
                   for s in symbols
                   if s.replace("/USDT", "").replace("/USD", "").upper() not in result]

        if missing:
            self._rate_limit_sleep()
            comma = ",".join(missing)
            # v2 批量: /cryptocurrency/quotes/latest?symbol=BTC,ETH,PEPE
            data = self._get("cryptocurrency/quotes/latest", {"symbol": comma, "convert": convert})
            if data and "data" in data:
                for cid_str, coin in data["data"].items():
                    sym_raw = coin.get("symbol", "")
                    self._ticker_cache[sym_raw] = coin
                    t = self._dict_to_ticker(coin)
                    if t:
                        result[sym_raw] = t
            self._ticker_cache_time = now

        return result

    def get_global_metrics(self) -> Optional[CMCGlobalMetrics]:
        """获取全球市场指标"""
        now = time.time()
        if self._global_cache and (now - self._global_cache_time) < self.cache_ttl:
            return self._dict_to_global(self._global_cache)

        self._rate_limit_sleep()
        # v2 endpoint: same path
        data = self._get("global-metrics", {"convert": "USD"})
        if not data or "data" not in data:
            return None

        self._global_cache = data["data"]
        self._global_cache_time = now
        return self._dict_to_global(self._global_cache)

    def get_listings_latest(self, limit: int = 100, start: int = 1) -> List[Dict[str, Any]]:
        """
        获取最新上市币种列表（用于发现新币）
        """
        self._rate_limit_sleep()
        data = self._get("cryptocurrency/listings/latest", {
            "start": str(start),
            "limit": str(limit),
            "convert": "USD",
            "sort_dir": "desc",
        })
        if not data or "data" not in data:
            return []
        return data["data"]

    def get_id_map(self, symbol: str) -> Optional[int]:
        """获取 CMC ID"""
        sym = symbol.replace("/USDT", "").replace("/USD", "").upper()
        if sym in self._symbol_map_cache:
            return self._symbol_map_cache[sym]

        self._rate_limit_sleep()
        data = self._get("cryptocurrency/info", {"symbol": sym})
        if data and "data" in data and sym in data["data"]:
            cid = data["data"][sym].get("id")
            if cid:
                self._symbol_map_cache[sym] = cid
                return cid
        return None

    # ── Helper: 数据转换 ───────────────────────────────────────────

    def _dict_to_ticker(self, d: Dict[str, Any]) -> CMCTicker:
        """将 API dict 转 CMCTicker"""
        quote = d.get("quote", {}).get("USD", {})
        return CMCTicker(
            cmc_id=d.get("id", 0),
            symbol=d.get("symbol", ""),
            name=d.get("name", ""),
            price=float(quote.get("price", 0) or 0),
            change_1h=float(quote.get("percent_change_1h", 0) or 0),
            change_24h=float(quote.get("percent_change_24h", 0) or 0),
            change_7d=float(quote.get("percent_change_7d", 0) or 0),
            market_cap=float(quote.get("market_cap", 0) or 0),
            market_cap_dominance=float(d.get("cmc_rank", 0) or 0),
            volume_24h=float(quote.get("volume_24h", 0) or 0),
            circulating_supply=float(d.get("circulating_supply", 0) or 0),
            total_supply=float(d.get("total_supply", 0) or 0),
            max_supply=float(d.get("max_supply", 0) or 0),
            ath=float(quote.get("ath", 0) or 0),
            atl=float(quote.get("atl", 0) or 0),
            rank=int(d.get("cmc_rank", 0) or 0),
            fdv=float(quote.get("fully_diluted_market_cap", 0) or 0),
            mcap_ratio=float(quote.get("market_cap", 1) or 1) / max(float(quote.get("fully_diluted_market_cap", 1) or 1), 1),
        )

    def _cache_to_ticker(self, sym: str) -> Optional[CMCTicker]:
        d = self._ticker_cache.get(sym)
        return self._dict_to_ticker(d) if d else None

    def _dict_to_global(self, d: Dict[str, Any]) -> CMCGlobalMetrics:
        return CMCGlobalMetrics(
            total_market_cap=float(d.get("quote", {}).get("USD", {}).get("total_market_cap", 0) or 0),
            total_volume_24h=float(d.get("quote", {}).get("USD", {}).get("total_volume_24h", 0) or 0),
            btc_dominance=float(d.get("btc_dominance", 0) or 0),
            eth_dominance=float(d.get("eth_dominance", 0) or 0),
            altcoin_dominance=100 - float(d.get("btc_dominance", 0) or 0) - float(d.get("eth_dominance", 0) or 0),
            market_cap_change_24h=float(d.get("quote", {}).get("USD", {}).get("market_cap_change_percentage_24h", 0) or 0),
            active_cryptocurrencies=int(d.get("active_cryptocurrencies", 0) or 0),
        )
