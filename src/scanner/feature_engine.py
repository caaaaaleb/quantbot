"""特征计算引擎 - 计算所有技术指标特征"""

from typing import Dict, Any, Optional, List
from dataclasses import dataclass

from src.scanner.data_source import KlineData, TickerData, OrderBookData, TradesData
from src.utils.logger import logger


@dataclass
class CoinFeatures:
    """币种特征数据"""
    symbol: str

    # 动量特征
    momentum_1m: float = 0.0    # 1分钟涨跌幅
    momentum_5m: float = 0.0    # 5分钟涨跌幅
    momentum_15m: float = 0.0   # 15分钟涨跌幅

    # 成交量特征
    volume_spike: float = 0.0   # 当前成交量 / 20期均值
    volume_24h: float = 0.0

    # 价格位置
    breakout_ratio: float = 0.0  # 价格 / 20期高点
    price: float = 0.0

    # 波动率
    atr: float = 0.0
    atr_pct: float = 0.0        # ATR / 价格

    # 资金流
    taker_buy_ratio: float = 0.5

    # 订单簿
    orderbook_imbalance: float = 0.0

    # 长周期参考（过滤用）
    change_24h: float = 0.0

    # 原始数据
    high_20: float = 0.0
    avg_volume_20: float = 0.0

    # ── CMC 增强特征（可选）─────────────────────────────────────────
    cmc_rank: int = 0            # CMC 排名
    market_cap: float = 0.0      # 市值 ($)
    mcap_ratio: float = 1.0      # MCap / FDV
    ath_distance_pct: float = 0.0 # 距历史高点百分比
    cmc_volume_score: float = 0.0 # CMC 流动性评分
    cmc_mcap_score: float = 0.0  # CMC 市值规模评分
    risk_flags: Optional[List[str]] = None  # 风险标记

    def __post_init__(self):
        if self.risk_flags is None:
            self.risk_flags = []


class FeatureEngine:
    """特征计算引擎"""

    def __init__(self, momentum_threshold_1m: float = 0.005,
                 momentum_threshold_5m: float = 0.02,
                 volume_spike_threshold: float = 2.0,
                 atr_period: int = 14,
                 cmc_enricher=None):
        self.momentum_threshold_1m = momentum_threshold_1m
        self.momentum_threshold_5m = momentum_threshold_5m
        self.volume_spike_threshold = volume_spike_threshold
        self.atr_period = atr_period
        self.cmc_enricher = cmc_enricher

    def compute(self, symbol: str,
                klines_1m: KlineData,
                klines_5m: KlineData,
                klines_15m: KlineData,
                ticker: TickerData,
                orderbook: OrderBookData,
                trades: TradesData) -> CoinFeatures:
        """
        计算币种的所有特征

        Args:
            symbol: 交易对
            klines_1m: 1分钟K线
            klines_5m: 5分钟K线
            klines_15m: 15分钟K线
            ticker: Ticker 数据
            orderbook: 订单簿
            trades: 成交数据

        Returns:
            CoinFeatures: 特征对象
        """
        f = CoinFeatures(symbol=symbol)

        # ── 价格数据 ────────────────────────────────────────────────
        if not klines_1m or len(klines_1m.closes) < 2:
            return f

        f.price = ticker.last if ticker else klines_1m.closes[-1]
        closes_1m = klines_1m.closes

        # ── 动量计算 ─────────────────────────────────────────────────
        f.momentum_1m = self._pct_change(closes_1m[-2], closes_1m[-1]) if len(closes_1m) >= 2 else 0.0

        if klines_5m and len(klines_5m.closes) >= 2:
            f.momentum_5m = self._pct_change(klines_5m.closes[-2], klines_5m.closes[-1])
        else:
            # 用1m数据模拟5m
            if len(closes_1m) >= 6:
                f.momentum_5m = self._pct_change(closes_1m[-6], closes_1m[-1])

        if klines_15m and len(klines_15m.closes) >= 2:
            f.momentum_15m = self._pct_change(klines_15m.closes[-2], klines_15m.closes[-1])
        else:
            # 用1m数据模拟15m
            if len(closes_1m) >= 16:
                f.momentum_15m = self._pct_change(closes_1m[-16], closes_1m[-1])

        # ── 成交量特征 ───────────────────────────────────────────────
        volumes_20 = klines_1m.volumes[-20:]
        f.avg_volume_20 = sum(volumes_20) / len(volumes_20) if volumes_20 else 1.0
        current_vol = klines_1m.volumes[-1] if klines_1m.volumes else 0.0
        f.volume_spike = current_vol / f.avg_volume_20 if f.avg_volume_20 > 0 else 0.0
        f.volume_24h = ticker.volume_24h if ticker else 0.0

        # ── 价格位置（突破比率） ─────────────────────────────────────
        highs_20 = klines_1m.highs[-20:]
        f.high_20 = max(highs_20) if highs_20 else f.price
        f.breakout_ratio = f.price / f.high_20 if f.high_20 > 0 else 1.0

        # ── ATR 波动率 ────────────────────────────────────────────────
        f.atr = self._calc_atr(klines_1m.highs, klines_1m.lows, klines_1m.closes, self.atr_period)
        f.atr_pct = f.atr / f.price if f.price > 0 else 0.0

        # ── 资金流 ───────────────────────────────────────────────────
        if trades:
            f.taker_buy_ratio = trades.taker_buy_ratio

        # ── 订单簿 ───────────────────────────────────────────────────
        if orderbook:
            f.orderbook_imbalance = orderbook.imbalance

        # ── 24h 数据（过滤用） ───────────────────────────────────────
        if ticker:
            f.change_24h = ticker.change_24h

        # ── CMC 增强特征（可选，非阻塞）───────────────────────────────
        if self.cmc_enricher and self.cmc_enricher.available:
            try:
                cmc_feat = self.cmc_enricher.get_coin_features(symbol)
                if cmc_feat:
                    f.cmc_rank = cmc_feat.rank
                    f.market_cap = cmc_feat.market_cap
                    f.mcap_ratio = cmc_feat.mcap_ratio
                    f.ath_distance_pct = cmc_feat.ath_distance_pct
                    f.cmc_volume_score = cmc_feat.volume_score
                    f.cmc_mcap_score = cmc_feat.mcap_score
                    f.risk_flags = cmc_feat.risk_flags
            except Exception:
                pass  # CMC 数据失败不影响主流程

        return f

    def _pct_change(self, past: float, current: float) -> float:
        """涨跌幅计算"""
        if past <= 0:
            return 0.0
        return (current - past) / past

    def _calc_atr(self, highs: list, lows: list, closes: list, period: int) -> float:
        """计算 ATR"""
        if len(highs) < period + 1 or len(lows) < period + 1:
            return 0.0
        trs = []
        for i in range(1, len(closes)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1])
            )
            trs.append(tr)
        atr = sum(trs[-period:]) / period if trs else 0.0
        return atr