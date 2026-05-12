"""打分系统 - 综合评分计算"""

from dataclasses import dataclass

from src.scanner.feature_engine import CoinFeatures
from src.scanner.signal_engine import SignalResult
from src.utils.logger import logger


@dataclass
class ScoreResult:
    """评分结果"""
    score: float           # 综合评分 (0 ~ 1)
    stage: str            # early / mid / late / none
    confidence: float     # 置信度
    signal: float         # 信号值
    reasons: list         # 加分/扣分原因


class ScoringEngine:
    """
    打分系统：加权评分 + 调整项
    支持 CMC 增强特征
    """

    def __init__(
        self,
        w_momentum_5m: float = 0.30,
        w_momentum_1m: float = 0.20,
        w_volume_spike: float = 0.20,
        w_taker_buy: float = 0.15,
        w_orderbook: float = 0.15,
        # 过滤阈值
        max_change_24h: float = 80.0,
        min_volume_24h: float = 1_000_000.0,
        # CMC 增强权重
        w_cmc_mcap: float = 0.10,
        w_cmc_vol: float = 0.10,
        w_cmc_ath_dist: float = 0.05,
        # CMC 过滤参数
        cmc_filter_enabled: bool = False,
        max_cmc_rank: int = 0,       # 0=不过滤
        max_mcap_ratio: float = 1.0, # MCap/FDV 上限
    ):
        self.w_momentum_5m = w_momentum_5m
        self.w_momentum_1m = w_momentum_1m
        self.w_volume_spike = w_volume_spike
        self.w_taker_buy = w_taker_buy
        self.w_orderbook = w_orderbook
        self.max_change_24h = max_change_24h
        self.min_volume_24h = min_volume_24h
        self.w_cmc_mcap = w_cmc_mcap
        self.w_cmc_vol = w_cmc_vol
        self.w_cmc_ath_dist = w_cmc_ath_dist
        self.cmc_filter_enabled = cmc_filter_enabled
        self.max_cmc_rank = max_cmc_rank
        self.max_mcap_ratio = max_mcap_ratio

    def score(self, f: CoinFeatures, signal: SignalResult) -> ScoreResult:
        """
        计算综合评分

        公式:
        score = (
            0.3 * norm(momentum_5m) +
            0.2 * norm(momentum_1m) +
            0.2 * norm(volume_spike) +
            0.15 * taker_buy_ratio +
            0.15 * orderbook_imbalance
        )
        """
        reasons = []

        # ── 归一化动量 ───────────────────────────────────────────────
        # momentum_5m: 0~10% → 0~1
        mom_5m_score = min(f.momentum_5m / 0.10, 1.0) if f.momentum_5m > 0 else 0.0
        # momentum_1m: 0~5% → 0~1
        mom_1m_score = min(f.momentum_1m / 0.05, 1.0) if f.momentum_1m > 0 else 0.0
        # volume_spike: 0.5x~5x → 0~1（0.5x以下也有基础分）
        vol_score = min(max((f.volume_spike - 0.3) / 4.7, 0.0), 1.0) if f.volume_spike > 0 else 0.0

        # ── 加权求和 ─────────────────────────────────────────────────
        raw_score = (
            self.w_momentum_5m * mom_5m_score +
            self.w_momentum_1m * mom_1m_score +
            self.w_volume_spike * vol_score +
            self.w_taker_buy * f.taker_buy_ratio +
            self.w_orderbook * (f.orderbook_imbalance + 0.5) / 1.5  # imbalance ∈ [-1,1] → [0,1]
        )

        # ── 调整项 ───────────────────────────────────────────────────
        if f.change_24h > 30:
            penalty = 1.0 - min((f.change_24h - 30) / self.max_change_24h, 0.5)
            raw_score *= penalty
            reasons.append(f"24h涨幅过大: {f.change_24h:.1f}% → {penalty:.2f}")

        if f.volume_24h < self.min_volume_24h:
            raw_score *= 0.5
            reasons.append(f"24h成交量过低: ${f.volume_24h/1e6:.1f}M → 0.5x")

        if f.atr_pct > 0.05:
            raw_score *= 0.8
            reasons.append(f"高波动: ATR%={f.atr_pct*100:.1f}% → 0.8x")

        # ── CMC 增强评分（可选项）────────────────────────────────────────
        has_cmc = f.cmc_rank > 0 and f.market_cap > 0
        if has_cmc:
            # CMC 市值规模评分
            mcap_score_norm = min(f.cmc_mcap_score / 100.0, 1.0)
            raw_score += self.w_cmc_mcap * mcap_score_norm

            # CMC 流动性评分
            vol_score_norm = min(f.cmc_volume_score / 100.0, 1.0)
            raw_score += self.w_cmc_vol * vol_score_norm

            # ATH 超跌反弹信号（距 ATH 越远 = 可能超跌 = 额外加分）
            if f.ath_distance_pct > 0:
                ath_score = min(f.ath_distance_pct / 50.0, 1.0)  # 距 ATH 50% = 满分
                raw_score += self.w_cmc_ath_dist * ath_score
                if ath_score > 0.5:
                    reasons.append(f"ATH超跌: {f.ath_distance_pct:.0f}%")

            # CMC 风险过滤
            if self.cmc_filter_enabled:
                if 0 < self.max_cmc_rank < f.cmc_rank:
                    raw_score *= 0.3
                    reasons.append(f"CMC排名过低: #{f.cmc_rank} → 0.3x")
                if f.mcap_ratio < 0.2:
                    raw_score *= 0.5
                    reasons.append(f"HIGH_FDV: MCap/FDV={f.mcap_ratio:.2f} → 0.5x")
                if "HIGH_FDV_RISK" in f.risk_flags:
                    raw_score *= 0.7
                    reasons.append("CMC_FDV风险 → 0.7x")

        if raw_score > 0.05:
            logger.info(f"📊 {f.symbol} 评分: raw={raw_score:.3f} | 1m={f.momentum_1m*100:.2f}% 5m={f.momentum_5m*100:.2f}% vol={f.volume_spike:.1f}x taker={f.taker_buy_ratio:.2f}")

        # ── 阶段分类（优先级：signal.stage > 自动判断） ─────────────────
        stage = signal.stage
        if stage == "none" and raw_score > 0.4:
            if f.momentum_5m > 0.01 and f.volume_spike > 1.5:
                stage = "early"
            elif f.momentum_5m > 0.005:
                stage = "mid"

        score = max(0.0, min(raw_score, 1.0))

        if reasons:
            logger.debug(f"{f.symbol} 评分调整: {' | '.join(reasons)}")

        return ScoreResult(
            score=score,
            stage=stage,
            confidence=signal.confidence,
            signal=signal.signal,
            reasons=reasons,
        )