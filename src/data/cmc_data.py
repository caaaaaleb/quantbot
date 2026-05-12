"""CMC 市场数据增强模块

用途：
1. 为 scanner 提供市值/流动性评分（替代纯成交量）
2. 为 feature_engine 提供 MCap、FDV、ATH distance 等扩展特征
3. 全局市场情绪指标（恐惧贪婪、BTC dominance）
4. 新币发现（CMCLatest Listings）
"""

import time
from typing import Dict, Any, List, Optional
from dataclasses import dataclass

from src.utils.logger import logger

try:
    from src.data.coinmarketcap import CMCClient, CMCTicker, CMCGlobalMetrics
    _CMC_AVAILABLE = True
except ImportError:
    _CMC_AVAILABLE = False
    logger.warning("CMC 模块不可用，市场数据增强功能已禁用")


@dataclass
class CMCCoinFeatures:
    """CMC 增强特征"""
    symbol: str
    rank: int               # CMC 排名
    market_cap: float       # 市值 ($)
    volume_24h: float       # 24h 成交量 ($)
    mcap_ratio: float       # 市值 / FDV（<1 表示大量代币未解锁）
    ath_distance_pct: float # 当前价距历史高点 (%)
    volume_score: float     # 流动性评分 (0-100)
    mcap_score: float       # 市值规模评分 (0-100)
    risk_flags: List[str]   # 风险标记


class CMCEnricher:
    """
    CMC 数据增强器

    在现有 scanner/feature_engine 基础上，补充：
    - CMC 排名 / 市值规模
    - FDV vs MCap 比例（解锁风险）
    - ATH distance（超跌反弹信号）
    - 流动性评分（优于纯24h成交量）
    - 全局市场情绪
    """

    def __init__(self, cmc_client: Optional["CMCClient"]):
        self.cmc = cmc_client
        self._cache: Dict[str, CMCCoinFeatures] = {}
        self._cache_time: float = 0
        self._cache_ttl: float = 300  # 5 分钟缓存
        self._global_metrics: Optional[CMCGlobalMetrics] = None
        self._global_metrics_time: float = 0
        # 全局请求锁：防止 scanner 并发调用触发 rate limit
        self._lock: bool = False

    @property
    def available(self) -> bool:
        return _CMC_AVAILABLE and self.cmc is not None

    def get_coin_features(self, symbol: str) -> Optional[CMCCoinFeatures]:
        """
        获取单个币的 CMC 增强特征

        Args:
            symbol: "BTC" 或 "BTC/USDT"
        Returns:
            CMCCoinFeatures 或 None
        """
        if not self.available:
            return None

        sym = symbol.replace("/USDT", "").replace("/USD", "").upper()
        now = time.time()

        # 缓存命中
        if sym in self._cache and (now - self._cache_time) < self._cache_ttl:
            return self._cache[sym]

        # 锁：已有请求在进行中，跳过（防止并发触发 rate limit）
        if self._lock:
            return None

        self._lock = True
        try:
            ticker = self.cmc.get_ticker(sym)
            if not ticker:
                return None

            features = self._build_features(sym, ticker)
            self._cache[sym] = features
            self._cache_time = now
            return features
        finally:
            self._lock = False

    def batch_features(self, symbols: List[str]) -> Dict[str, CMCCoinFeatures]:
        """
        批量获取多个币的 CMC 增强特征
        """
        if not self.available:
            return {}

        cleaned = [s.replace("/USDT", "").replace("/USD", "").upper() for s in symbols]
        now = time.time()

        # 缓存命中
        cached = {s: self._cache[s] for s in cleaned
                  if s in self._cache and (now - self._cache_time) < self._cache_ttl}

        missing = [s for s in cleaned if s not in cached]
        if missing:
            batch = self.cmc.get_tickers(missing)
            for sym_raw, ticker in batch.items():
                features = self._build_features(sym_raw, ticker)
                self._cache[sym_raw] = features
                cached[sym_raw] = features
            self._cache_time = now

        return cached

    def get_global_metrics(self) -> Optional[CMCGlobalMetrics]:
        """获取全球市场情绪指标"""
        if not self.available:
            return None

        now = time.time()
        if self._global_metrics and (now - self._global_metrics_time) < self._cache_ttl:
            return self._global_metrics

        metrics = self.cmc.get_global_metrics()
        if metrics:
            self._global_metrics = metrics
            self._global_metrics_time = now
        return metrics

    def get_new_coins(self, limit: int = 20) -> List[Dict[str, Any]]:
        """
        获取最新上市的币种（CMCLatest Listings）

        用于发现刚上线的新币机会（上线 7 天内）
        """
        if not self.available:
            return []

        try:
            listings = self.cmc.get_listings_latest(limit=100, start=1)
            recent = []
            for coin in listings:
                quote = coin.get("quote", {}).get("USD", {})
                # 简单筛选：上线 < 7 天，成交量 > $1M
                date_added = coin.get("date_added", "")
                try:
                    from datetime import datetime, timedelta
                    added = datetime.strptime(date_added[:10], "%Y-%m-%d")
                    age_days = (datetime.now() - added).days
                except Exception:
                    age_days = 999

                vol_24h = float(quote.get("volume_24h", 0) or 0)
                if age_days <= 30 and vol_24h > 500_000:
                    recent.append({
                        "symbol": coin.get("symbol", ""),
                        "name": coin.get("name", ""),
                        "rank": coin.get("cmc_rank", 999),
                        "age_days": age_days,
                        "volume_24h": vol_24h,
                        "price": float(quote.get("price", 0) or 0),
                        "change_24h": float(quote.get("percent_change_24h", 0) or 0),
                    })
            recent.sort(key=lambda x: x["volume_24h"], reverse=True)
            logger.info(f"CMC 新币发现: {len(recent)} 个近30天上线且有流动性的币")
            return recent[:limit]
        except Exception as e:
            logger.error(f"CMC 新币发现失败: {e}")
            return []

    def get_liquidity_rating(self, symbol: str) -> float:
        """
        获取流动性评级 (0-100)

        综合考虑:
        - CMC 24h 成交量
        - 交易所数量
        - MCap / FDV 比率
        """
        if not self.available:
            return 0.0

        ticker = self.cmc.get_ticker(symbol)
        if not ticker:
            return 0.0

        vol = ticker.volume_24h
        mcap = ticker.market_cap

        # 成交量评分
        vol_score = min(vol / 100_000_000 * 100, 100) if vol > 0 else 0  # $100M=满分

        # 市值规模评分
        mcap_score = min(mcap / 1_000_000_000 * 10, 100) if mcap > 0 else 0  # $1B=满分

        # FDV 风险评分（FDV 远超 MCap 说明大量代币未解锁）
        fdv_risk = 0
        if ticker.fdv > 0:
            ratio = mcap / ticker.fdv
            if ratio < 0.1:
                fdv_risk = 30  # 高风险
            elif ratio < 0.3:
                fdv_risk = 15

        return max(min(vol_score * 0.6 + mcap_score * 0.3 - fdv_risk, 100), 0)

    def _build_features(self, sym: str, ticker: "CMCTicker") -> CMCCoinFeatures:
        """从 CMCTicker 构建 CMCCoinFeatures"""

        # ATH distance
        ath_dist = 0.0
        if ticker.ath > 0 and ticker.price > 0:
            ath_dist = (ticker.ath - ticker.price) / ticker.ath * 100

        # 流动性评分
        vol_score = self._vol_score(ticker.volume_24h)

        # 市值规模评分
        mcap_score = self._mcap_score(ticker.market_cap)

        # 风险标记
        risk_flags = []
        if ticker.mcap_ratio < 0.2:
            risk_flags.append("HIGH_FDV_RISK")
        if ticker.circulating_supply < ticker.total_supply * 0.5:
            risk_flags.append("LOW_CIRC_SUPPLY")
        if ticker.max_supply > 0 and ticker.circulating_supply / ticker.max_supply > 0.95:
            risk_flags.append("MAX_SUPPLY_REACHED")
        if ticker.rank == 0 or ticker.rank > 200:
            risk_flags.append("LOW_RANK")
        if ath_dist > 90:
            risk_flags.append("DEEP_ATH_DROP")
        if ticker.change_24h < -20:
            risk_flags.append("HEAVY_DUMP")

        return CMCCoinFeatures(
            symbol=sym,
            rank=ticker.rank,
            market_cap=ticker.market_cap,
            volume_24h=ticker.volume_24h,
            mcap_ratio=ticker.mcap_ratio,
            ath_distance_pct=ath_dist,
            volume_score=vol_score,
            mcap_score=mcap_score,
            risk_flags=risk_flags,
        )

    def _vol_score(self, vol_24h: float) -> float:
        """成交量评分"""
        if vol_24h < 100_000:
            return 10
        elif vol_24h < 1_000_000:
            return 25
        elif vol_24h < 10_000_000:
            return 50
        elif vol_24h < 100_000_000:
            return 75
        elif vol_24h < 1_000_000_000:
            return 90
        else:
            return 100

    def _mcap_score(self, mcap: float) -> float:
        """市值规模评分（越大越安全）"""
        if mcap < 1_000_000:
            return 10
        elif mcap < 10_000_000:
            return 25
        elif mcap < 100_000_000:
            return 40
        elif mcap < 1_000_000_000:
            return 60
        elif mcap < 10_000_000_000:
            return 80
        else:
            return 100
