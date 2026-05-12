"""潜力币自动扫描模块"""

import ccxt
import time
from typing import Dict, Any, List, Optional
from dataclasses import dataclass
from datetime import datetime

from src.utils.logger import logger


@dataclass
class CoinScore:
    """币种评分"""
    symbol: str = ""
    score: float = 0.0
    price: float = 0.0
    change_24h: float = 0.0
    change_1h: float = 0.0
    volume_24h: float = 0.0
    volume_ratio: float = 0.0
    momentum_score: float = 0.0
    liquidity_score: float = 0.0
    reason: str = ""


class CoinScanner:
    """
    潜力币扫描器

    筛选标准:
    - 24h 成交量放大超过历史均量 X 倍
    - 价格动量处于上升趋势但不过度
    - 流动性充足（避免土狗币）
    - 排除稳定币和已有持仓的币
    """

    def __init__(
        self,
        exchange: ccxt.Exchange,
        min_volume_24h: float = 1_000_000,      # 最低24h成交量 ($1M)
        min_volume_ratio: float = 3.0,            # 当日/均量 最小比值
        max_change_pct: float = 50.0,             # 最大涨幅（过滤Meme币）
        lookback_days: int = 7,                  # 历史均量回溯天数
        top_n: int = 20,                         # 返回候选币数量
        price_change_interval: str = "1h",        # 涨跌幅参考周期（1h/4h/24h）
    ):
        self.exchange = exchange
        self.min_volume_24h = min_volume_24h
        self.min_volume_ratio = min_volume_ratio
        self.max_change_pct = max_change_pct
        self.lookback_days = lookback_days
        self.top_n = top_n
        self.price_change_interval = price_change_interval

        # 缓存历史成交量（避免重复请求）
        self._volume_cache: Dict[str, List[float]] = {}
        # 缓存全市场 ticker（避免重复请求）
        self._tickers_cache: Dict[str, Any] = {}
        self._tickers_cache_time: float = 0
        self._tickers_cache_ttl: float = 30  # 30秒缓存

        logger.info(
            f"CoinScanner 初始化 - 最低成交量=${min_volume_24h/1e6:.0f}M, "
            f"最小量比={min_volume_ratio}x, 回溯={lookback_days}天"
        )

    def _get_tickers(self) -> Dict[str, Any]:
        """获取全市场tickers（带30s缓存）"""
        now = time.time()
        if self._tickers_cache and (now - self._tickers_cache_time) < self._tickers_cache_ttl:
            return self._tickers_cache
        tickers = self.exchange.fetch_tickers()
        self._tickers_cache = tickers
        self._tickers_cache_time = now
        return tickers

    def scan(self, exclude_symbols: List[str] = None) -> List[CoinScore]:
        """
        扫描全市场，返回潜力币候选列表

        Args:
            exclude_symbols: 排除的交易对列表（如已持仓的币）

        Returns:
            list[CoinScore]: 按评分排序的候选币列表
        """
        exclude_symbols = exclude_symbols or []
        logger.info("开始扫描潜力币...")

        try:
            # 1. 获取所有 USDT 交易对的 24h 数据
            tickers = self._get_tickers()
            usdt_pairs = {
                sym: data for sym, data in tickers.items()
                if "/USDT" in sym
                and sym not in exclude_symbols
                and sym not in ["USDC/USDT", "USDP/USDT", "BUSD/USDT", "USDt/USDT"]
            }

            # 2. 基础过滤
            candidates = []
            for symbol, data in usdt_pairs.items():
                price = data.get("last", 0) or 0
                change_24h = data.get("percentage", 0) or 0
                volume_24h = data.get("quoteVolume", 0) or 0  # USDT 成交额

                # 过滤价格太低（土狗最小交易量问题）或太高
                if price <= 0:
                    continue

                # 过滤涨幅过大（可能是Meme币）
                if abs(change_24h) > self.max_change_pct:
                    continue

                # 过滤成交量不足
                if volume_24h < self.min_volume_24h:
                    continue

                # 计算短期涨跌幅
                change_short = self._get_price_change(symbol, self.price_change_interval)

                candidates.append({
                    "symbol": symbol,
                    "price": price,
                    "change_24h": change_24h,
                    "change_short": change_short,
                    "volume_24h": volume_24h,
                })

            logger.info(f"基础过滤后剩余 {len(candidates)} 个候选")

            # 3. 计算历史均量并评分
            scored = []
            for c in candidates:
                avg_volume = self._get_avg_volume(c["symbol"], c["price"])
                volume_ratio = c["volume_24h"] / avg_volume if avg_volume > 0 else 0

                # 综合评分
                score = self._calculate_score(
                    change_24h=c["change_24h"],
                    change_short=c["change_short"],
                    volume_ratio=volume_ratio,
                    volume_24h=c["volume_24h"],
                    price=c["price"],
                )

                reason = self._get_reason(c, volume_ratio, score)

                scored.append(CoinScore(
                    symbol=c["symbol"],
                    score=score,
                    price=c["price"],
                    change_24h=c["change_24h"],
                    change_1h=c["change_short"],
                    volume_24h=c["volume_24h"],
                    volume_ratio=volume_ratio,
                    momentum_score=self._momentum_score(c["change_short"]),
                    liquidity_score=self._liquidity_score(c["volume_24h"]),
                    reason=reason,
                ))

            # 4. 按评分排序，取 top N
            scored.sort(key=lambda x: x.score, reverse=True)
            top = scored[:self.top_n]

            logger.info(
                f"扫描完成 - {len(top)} 个潜力币 | "
                f"冠军: {top[0].symbol if top else 'N/A'} (评分={top[0].score:.1f})"
            )
            return top

        except Exception as e:
            logger.warning(f"扫描被rate limit，跳过本次: {e}")
            return []

    def _get_avg_volume(self, symbol: str, current_price: float) -> float:
        """
        获取该币过去 N 天的平均日成交量
        优先用缓存，否则请求 K 线计算
        """
        if symbol in self._volume_cache:
            volumes = self._volume_cache[symbol]
            return sum(volumes) / len(volumes) if volumes else 0

        try:
            # 用日K线计算每日成交量
            ohlcv = self.exchange.fetch_ohlcv(symbol, "1d", limit=self.lookback_days)
            volumes = [c[5] * c[4] for c in ohlcv]  # volume * close = quote volume

            # 缓存
            self._volume_cache[symbol] = volumes
            return sum(volumes) / len(volumes) if volumes else 0

        except Exception:
            return 0

    def _get_price_change(self, symbol: str, interval: str = "1h") -> float:
        """
        计算指定周期的价格涨跌幅

        Args:
            symbol: 交易对
            interval: K线周期 (1m/5m/1h/4h/1d)

        Returns:
            float: 涨跌幅 (%)
        """
        try:
            ohlcv = self.exchange.fetch_ohlcv(symbol, interval, limit=2)
            if not ohlcv or len(ohlcv) < 2:
                return 0.0
            past_price = ohlcv[0][4]  # 早期收盘价
            current_price = ohlcv[-1][4]  # 当前收盘价
            if past_price <= 0:
                return 0.0
            return (current_price - past_price) / past_price * 100
        except Exception:
            return 0.0

    def _calculate_score(
        self,
        change_24h: float,
        change_short: float,
        volume_ratio: float,
        volume_24h: float,
        price: float,
    ) -> float:
        """
        综合评分 (0-100)

        组成:
        - 动量分 40%: 短期涨幅适中给分，过高/负给低分
        - 成交量分 40%: 量比越高说明越活跃
        - 流动性分 20%: 绝对成交量越高分越高
        """
        momentum = self._momentum_score(change_short)
        volume_score = self._volume_score(volume_ratio)
        liquidity = self._liquidity_score(volume_24h)

        return momentum * 0.4 + volume_score * 0.4 + liquidity * 0.2

    def _momentum_score(self, change_short: float) -> float:
        """
        动量分: 短期涨幅在 2%-15% 区间得分最高
        涨幅太小(<2%): 无明显方向
        涨幅过大(>25%): 可能是Meme，风险高
        """
        change = abs(change_short)
        if change < 1:
            return 20                    # 几乎不动
        elif change < 3:
            return 50 + (change - 1) * 10  # 1%~3%: 逐渐加分
        elif change <= 15:
            return 80                    # 3%~15%: 最佳区间
        elif change <= 25:
            return 80 - (change - 15) * 4  # 15%~25%: 开始减分
        else:
            return 40                    # >25%: 可能是Meme

    def _volume_score(self, volume_ratio: float) -> float:
        """
        成交量分: 量比越高说明越活跃
        """
        if volume_ratio < 1:
            return 20          # 低于均量
        elif volume_ratio < 2:
            return 40 + (volume_ratio - 1) * 20  # 1x~2x
        elif volume_ratio < 3:
            return 60 + (volume_ratio - 2) * 20  # 2x~3x
        elif volume_ratio < 5:
            return 80 + (volume_ratio - 3) * 5   # 3x~5x: 放量明显
        else:
            return 90 + min((volume_ratio - 5) * 2, 10)  # 5x+: 极度放量

    def _liquidity_score(self, volume_24h: float) -> float:
        """
        流动性分: 防止土狗币（成交量太小）
        """
        if volume_24h < 1_000_000:
            return 20
        elif volume_24h < 10_000_000:
            return 50 + (volume_24h - 1e6) / 9e5  # 1M~10M: 50~60
        elif volume_24h < 100_000_000:
            return 60 + (volume_24h - 1e7) / 9e6 * 20  # 10M~100M: 60~80
        else:
            return 80 + min((volume_24h - 1e8) / 9e8 * 20, 20)  # 100M+: 80~100

    def _get_reason(self, c: Dict, volume_ratio: float, score: float) -> str:
        """生成入选原因描述"""
        parts = []
        if volume_ratio >= 3:
            parts.append(f"成交量放大 {volume_ratio:.1f}x")
        if abs(c["change_short"]) >= 2:
            direction = "上涨" if c["change_short"] > 0 else "下跌"
            parts.append(f"1h{direction} {c['change_short']:.1f}%")
        if abs(c["change_24h"]) >= 5:
            parts.append(f"24h{c['change_24h']:.1f}%")
        if c["volume_24h"] >= 10_000_000:
            parts.append(f"成交额 ${c['volume_24h']/1e6:.0f}M")
        return " | ".join(parts) if parts else f"综合评分 {score:.1f}"

    def get_short_candidates(self, exclude_symbols: List[str] = None) -> List[CoinScore]:
        """
        获取适合做空的候选币
        筛选条件: 24h 跌幅明显 + 成交量放大（可能是反弹卖出机会）
        或者资金费率由负转正（空头被挤压信号）
        """
        exclude_symbols = exclude_symbols or []

        try:
            tickers = self._get_tickers()
            usdt_pairs = {
                sym: data for sym, data in tickers.items()
                if "/USDT" in sym
                and sym not in exclude_symbols
                and sym not in ["USDC/USDT", "USDP/USDT", "BUSD/USDT", "USDt/USDT"]
            }

            candidates = []
            for symbol, data in usdt_pairs.items():
                price = data.get("last", 0) or 0
                change_24h = data.get("percentage", 0) or 0
                volume_24h = data.get("quoteVolume", 0) or 0

                if price <= 0 or volume_24h < self.min_volume_24h:
                    continue

                avg_vol = self._get_avg_volume(symbol, price)
                ratio = volume_24h / avg_vol if avg_vol > 0 else 0

                # 做空候选: 跌幅适中（5%~30%），成交量放大
                if -30 <= change_24h <= -5:
                    if ratio >= 2.0:
                        score = self._calculate_score(
                            change_24h=change_24h,
                            change_short=change_24h,
                            volume_ratio=ratio,
                            volume_24h=volume_24h,
                            price=price,
                        )
                        candidates.append(CoinScore(
                            symbol=symbol,
                            score=score,
                            price=price,
                            change_24h=change_24h,
                            change_1h=change_24h,
                            volume_24h=volume_24h,
                            volume_ratio=ratio,
                            momentum_score=self._momentum_score(change_24h),
                            liquidity_score=self._liquidity_score(volume_24h),
                            reason=f"做空候选: 下跌 {abs(change_24h):.1f}% + 放量 {ratio:.1f}x",
                        ))

            candidates.sort(key=lambda x: x.score, reverse=True)
            return candidates[:self.top_n]

        except Exception as e:
            logger.warning(f"做空候选扫描被rate limit，跳过本次: {e}")
            return []

    def get_long_candidates(self, top_n: int = 20) -> List[CoinScore]:
        """获取适合做多的候选币（调用 scan）"""
        return self.scan(exclude_symbols=[])[:top_n]

    def get_ranked_coins(self, top_n: int = 50) -> List[CoinScore]:
        """获取全量评分排名（评分最高的所有币）"""
        return self.scan(exclude_symbols=[])[:top_n]
