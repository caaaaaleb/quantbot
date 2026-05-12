"""扫描服务 - 主服务整合所有引擎"""

import time
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field

from src.scanner.data_source import DataSource
from src.scanner.feature_engine import FeatureEngine, CoinFeatures
from src.scanner.signal_engine import SignalEngine, SignalResult
from src.scanner.scoring_engine import ScoringEngine, ScoreResult
from src.scanner.filter_engine import FilterEngine, FilterResult
from src.scanner.ranking_engine import RankingEngine, RankedCoin
from src.utils.logger import logger

# CMC 增强（可选依赖）
try:
    from src.data.cmc_data import CMCEnricher
    _CMC_AVAILABLE = True
except ImportError:
    _CMC_AVAILABLE = False
    CMCEnricher = None
    logger.warning("CMC 模块不可用，CoinMarketCap 增强功能已禁用")


@dataclass
class ScannerConfig:
    """扫描配置"""
    # 信号引擎
    momentum_1m_threshold: float = 0.005
    momentum_5m_threshold: float = 0.02
    volume_spike_threshold: float = 2.0
    taker_buy_threshold: float = 0.6
    breakout_threshold: float = 1.01
    # 打分权重
    w_momentum_5m: float = 0.30
    w_momentum_1m: float = 0.20
    w_volume_spike: float = 0.20
    w_taker_buy: float = 0.15
    w_orderbook: float = 0.15
    # 过滤
    min_volume_24h: float = 1_000_000.0
    max_spread: float = 0.005
    max_change_24h: float = 80.0
    max_atr_pct: float = 0.10
    # 排名
    top_n: int = 10


@dataclass
class CoinScanResult:
    """单个币扫描结果"""
    symbol: str
    features: CoinFeatures
    filter_result: FilterResult
    signal_result: Optional[SignalResult]
    score_result: Optional[ScoreResult]
    spread: float = 0.0


class ScannerService:
    """
    扫描服务：整合所有引擎，定期扫描全市场

    流程:
    1. 获取所有 USDT 交易对
    2. 对每个币获取数据 (klines, ticker, orderbook, trades)
    3. 计算特征
    4. 过滤
    5. 信号检测
    6. 评分
    7. 排名输出
    """

    def __init__(self, exchange, config: ScannerConfig = None, cmc_client=None):
        self.config = config or ScannerConfig()
        self.data_source = DataSource(exchange)

        # CMC 增强器（可选）
        self.cmc_enricher: Optional[CMCEnricher] = None
        if _CMC_AVAILABLE and cmc_client:
            try:
                self.cmc_enricher = CMCEnricher(cmc_client)
                logger.info("✅ CoinMarketCap 增强器已启用")
            except Exception as e:
                logger.warning(f"CMC 增强器初始化失败: {e}")

        self.feature_engine = FeatureEngine(
            momentum_threshold_1m=self.config.momentum_1m_threshold,
            momentum_threshold_5m=self.config.momentum_5m_threshold,
            volume_spike_threshold=self.config.volume_spike_threshold,
            cmc_enricher=self.cmc_enricher,
        )
        self.signal_engine = SignalEngine(
            momentum_1m_threshold=self.config.momentum_1m_threshold,
            momentum_5m_threshold=self.config.momentum_5m_threshold,
            volume_spike_threshold=self.config.volume_spike_threshold,
            taker_buy_threshold=self.config.taker_buy_threshold,
            breakout_threshold=self.config.breakout_threshold,
        )
        self.scoring_engine = ScoringEngine(
            w_momentum_5m=self.config.w_momentum_5m,
            w_momentum_1m=self.config.w_momentum_1m,
            w_volume_spike=self.config.w_volume_spike,
            w_taker_buy=self.config.w_taker_buy,
            w_orderbook=self.config.w_orderbook,
            max_change_24h=self.config.max_change_24h,
            min_volume_24h=self.config.min_volume_24h,
        )
        self.filter_engine = FilterEngine(
            min_volume_24h=self.config.min_volume_24h,
            max_spread=self.config.max_spread,
            max_change_24h=self.config.max_change_24h,
            max_atr_pct=self.config.max_atr_pct,
        )
        self.ranking_engine = RankingEngine(top_n=self.config.top_n)

        # 结果缓存
        self._last_results: List[RankedCoin] = []
        self._last_scan_time: float = 0
        self._last_scan_duration: float = 0
        # 启动时执行一次预热扫描（只扫成交量前100的币）
        self._initialized = False

    def scan(self, symbols: List[str] = None, limit: int = 100) -> List[RankedCoin]:
        """
        执行一次完整扫描

        Args:
            symbols: 指定币种列表，None 则扫描成交量前 N 的 USDT 对
            limit: 当 symbols=None 时，最多扫描的币种数量（按成交量排序）
        """
        start = time.time()
        logger.info("🔍 开始 Early Breakout 扫描...")

        # 获取币种列表（按成交量排序，取前 limit 个）
        if symbols is None:
            all_symbols = self.data_source.get_usdt_pairs()
            # 按成交量排序
            try:
                tickers = self.data_source._get_tickers()
                ranked = sorted(
                    all_symbols,
                    key=lambda s: tickers.get(s, {}).get("quoteVolume", 0) or 0,
                    reverse=True
                )
                symbols = ranked[:limit]
                logger.info(f"扫描范围: {len(symbols)} 个币（按成交量 top {limit}）")
            except Exception:
                symbols = all_symbols[:limit]

        results: List[CoinScanResult] = []
        scanned = 0
        skipped = 0

        for symbol in symbols:
            try:
                result = self._scan_single(symbol)
                if result.filter_result.passed and result.score_result and result.score_result.score > 0.05:
                    results.append(result)
                scanned += 1
            except Exception as e:
                skipped += 1
                continue

        # 排名
        scored_coins = [
            (r.symbol, r.score_result, r.features)
            for r in results if r.score_result and r.score_result.score > 0.05
        ]
        ranked = self.ranking_engine.rank(scored_coins)

        duration = time.time() - start
        self._last_results = ranked
        self._last_scan_time = time.time()
        self._last_scan_duration = duration

        # 输出日志
        if ranked:
            logger.info(f"扫描完成: 扫描 {scanned} 个，候选 {len(ranked)} 个 | 耗时 {duration:.1f}s")
            logger.info(f"🥇 冠军: {ranked[0].symbol} score={ranked[0].score:.2f} {ranked[0].stage}")
        else:
            logger.info(f"扫描完成: 扫描 {scanned} 个，候选 0 个 | 耗时 {duration:.1f}s")

        return ranked

    def _scan_single(self, symbol: str) -> CoinScanResult:
        """扫描单个币"""
        # ── 1. 获取数据 ─────────────────────────────────────────────
        ticker = self.data_source.fetch_ticker(symbol)
        klines_1m = self.data_source.fetch_klines(symbol, "1m", limit=25)
        klines_5m = self.data_source.fetch_klines(symbol, "5m", limit=25)
        klines_15m = self.data_source.fetch_klines(symbol, "15m", limit=25)
        orderbook = self.data_source.fetch_orderbook(symbol, limit=5)
        trades = self.data_source.fetch_recent_trades(symbol, limit=100)

        # ── 2. 特征计算 ─────────────────────────────────────────────
        features = self.feature_engine.compute(
            symbol, klines_1m, klines_5m, klines_15m,
            ticker, orderbook, trades
        )

        # ── 3. 过滤 ─────────────────────────────────────────────────
        spread = ticker.spread if ticker else 0.0
        filter_result = self.filter_engine.pass_filter(features, spread)

        if not filter_result.passed:
            return CoinScanResult(
                symbol=symbol,
                features=features,
                filter_result=filter_result,
                signal_result=None,
                score_result=None,
                spread=spread,
            )

        # ── 4. 信号检测 ─────────────────────────────────────────────
        signal_result = self.signal_engine.detect(features)

        # ── 5. 评分 ─────────────────────────────────────────────────
        score_result = self.scoring_engine.score(features, signal_result)

        return CoinScanResult(
            symbol=symbol,
            features=features,
            filter_result=filter_result,
            signal_result=signal_result,
            score_result=score_result,
            spread=spread,
        )

    def get_alerts(self) -> List[RankedCoin]:
        """获取爆发信号（early stage + score > 0.5）"""
        alerts = [c for c in self._last_results
                  if c.stage == "early" and c.score > 0.5]
        return alerts

    def get_detail(self, symbol: str) -> Optional[Dict[str, Any]]:
        """获取单币详细信息"""
        # 在最新结果中查找
        for r in self._last_results:
            if r.symbol == symbol:
                return {
                    "symbol": symbol,
                    "score": r.score,
                    "stage": r.stage,
                    "confidence": r.confidence,
                    "signal": r.signal,
                    "momentum_1m": r.momentum_1m,
                    "momentum_5m": r.momentum_5m,
                    "volume_spike": r.volume_spike,
                    "change_24h": r.change_24h,
                    "reasons": r.reasons,
                }
        return None

    def get_last_results(self) -> List[RankedCoin]:
        """获取上次扫描结果"""
        return self._last_results