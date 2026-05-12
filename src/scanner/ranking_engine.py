"""排名系统 - 综合评分排名"""

from typing import List, Optional
from dataclasses import dataclass

from src.utils.logger import logger


@dataclass
class RankedCoin:
    """排名结果"""
    rank: int
    symbol: str
    score: float
    stage: str
    confidence: float
    signal: float
    momentum_1m: float
    momentum_5m: float
    volume_spike: float
    change_24h: float
    reasons: list
    # CMC 增强字段
    cmc_rank: int = 0
    market_cap: float = 0.0
    risk_flags: Optional[list] = None

    def __post_init__(self):
        if self.risk_flags is None:
            self.risk_flags = []


class RankingEngine:
    """排名系统"""

    def __init__(self, top_n: int = 10):
        self.top_n = top_n

    def rank(self, scored_coins: List) -> List[RankedCoin]:
        """
        对币种评分排序，返回 Top N

        Args:
            scored_coins: [(symbol, score_result, features), ...]

        Returns:
            List[RankedCoin]: 排序结果
        """
        # 按 score 降序
        scored_coins.sort(key=lambda x: x[1].score, reverse=True)

        results = []
        for i, (symbol, score_result, features) in enumerate(scored_coins):
            ranked = RankedCoin(
                rank=i + 1,
                symbol=symbol,
                score=score_result.score,
                stage=score_result.stage,
                confidence=score_result.confidence,
                signal=score_result.signal,
                momentum_1m=features.momentum_1m,
                momentum_5m=features.momentum_5m,
                volume_spike=features.volume_spike,
                change_24h=features.change_24h,
                reasons=score_result.reasons,
                cmc_rank=features.cmc_rank,
                market_cap=features.market_cap,
                risk_flags=features.risk_flags,
            )
            results.append(ranked)

        return results[:self.top_n]

    def format_output(self, ranked: List[RankedCoin]) -> str:
        """格式化输出"""
        lines = []
        for c in ranked:
            emoji = {"early": "🚀", "mid": "🔥", "late": "⚠️"}.get(c.stage, "❓")
            stage_label = {"early": "启动", "mid": "中段", "late": "尾声", "none": "无信号"}.get(c.stage, c.stage)
            lines.append(
                f"{c.rank:2d}. {c.symbol:<15} {emoji} score={c.score:.2f} | "
                f"{stage_label} | 1m={c.momentum_1m*100:+.2f}% 5m={c.momentum_5m*100:+.2f}% | "
                f"量={c.volume_spike:.1f}x | 24h={c.change_24h:+.1f}% | 置信={c.confidence:.0%}"
            )
        return "\n".join(lines)