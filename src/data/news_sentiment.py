"""新闻情绪模块 — 抓取币种相关新闻 + 简单情绪评分"""

import asyncio
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

import httpx

from src.utils.logger import logger


# 正面/负面关键词（中英文，覆盖 meme 币常见词汇）
_POSITIVE_KEYWORDS = [
    "surge", "moon", "bullish", "rally", "breakthrough", "soar", "skyrocket",
    "explode", "boom", "gain", "up", "green", "pump", "breakout",
    "大涨", "暴涨", "突破", "拉升", "牛市", "起飞", "看涨",
]

_NEGATIVE_KEYWORDS = [
    "crash", "dump", "bearish", "plunge", "slump", "tumble", "collapse",
    "selloff", "sell-off", "decline", "drop", "red", "fud", "panic",
    "暴跌", "崩盘", "跳水", "砸盘", "熊市", "跌", "恐慌", "利空",
]

_ATTENTION_KEYWORDS = [
    "listing", "announce", "partnership", "launch", "upgrade", "burn",
    "buyback", "integration", "grant", "airdrop", "meme", "viral",
    "上线", "公告", "合作", "发布", "空投", "销毁",
]


@dataclass
class NewsItem:
    """单条新闻"""
    title: str
    source: str
    url: str
    date: str


@dataclass
class SentimentResult:
    """单个币种的情绪分析结果"""
    symbol: str
    coin_name: str
    score: float           # -1.0 ~ 1.0
    headline_count: int    # 相关新闻数
    positive_count: int
    negative_count: int
    attention_count: int   # 重要事件提及数
    headlines: List[str] = field(default_factory=list)
    recent_positive: List[str] = field(default_factory=list)
    recent_negative: List[str] = field(default_factory=list)


class NewsSentiment:
    """
    新闻情绪分析器

    数据源:
    1. Google News RSS（免费，无需 API Key）
    2. CoinGecko 公开 API（免费，无需 API Key）
    """

    # 币种符号 → CoinGecko ID 映射
    COINGECKO_IDS = {
        "PEPE": "pepe",
        "NEIRO": "neiro",
        "MEME": "meme",
        "BTC": "bitcoin",
        "ETH": "ethereum",
        "SOL": "solana",
        "DOGE": "dogecoin",
        "SHIB": "shiba-inu",
        "FLOKI": "floki",
        "BONK": "bonk",
    }

    # 币种符号 → 搜索关键词
    SEARCH_KEYWORDS = {
        "PEPE": "Pepe coin crypto",
        "NEIRO": "Neiro crypto",
        "MEME": "Meme coin crypto",
        "1000PEPE": "Pepe coin crypto",
        "1000NEIRO": "Neiro crypto",
        "1000BONK": "Bonk crypto",
        "BTC": "Bitcoin",
        "ETH": "Ethereum",
    }

    def __init__(
        self,
        cache_ttl: int = 300,  # 5分钟缓存
        max_news_per_symbol: int = 10,
    ):
        self.cache_ttl = cache_ttl
        self.max_news_per_symbol = max_news_per_symbol
        self._cache: Dict[str, Tuple[float, SentimentResult]] = {}
        self._client = httpx.AsyncClient(
            timeout=10.0,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
        )

    async def close(self):
        await self._client.aclose()

    def _get_search_keyword(self, symbol: str) -> str:
        """获取搜索关键词"""
        base = symbol.replace("/USDT", "").replace("1000", "")
        return self.SEARCH_KEYWORDS.get(symbol) or self.SEARCH_KEYWORDS.get(base, f"{base} crypto")

    def _get_coingecko_id(self, symbol: str) -> Optional[str]:
        """获取 CoinGecko ID"""
        base = symbol.replace("/USDT", "").replace("1000", "")
        return self.COINGECKO_IDS.get(base) or self.COINGECKO_IDS.get(symbol)

    async def fetch_news_rss(self, keyword: str) -> List[NewsItem]:
        """
        通过 Google News RSS 获取最新新闻

        Args:
            keyword: 搜索关键词

        Returns:
            list[NewsItem]: 新闻列表
        """
        url = f"https://news.google.com/rss/search?q={keyword}&hl=en-US&gl=US&ceid=US:en"
        try:
            response = await self._client.get(url)
            response.raise_for_status()
            root = ET.fromstring(response.text)
            items = []
            for item in root.findall(".//item"):
                title = item.findtext("title", "")
                source = item.findtext("source", "")
                link = item.findtext("link", "")
                pub_date = item.findtext("pubDate", "")
                # 过滤非相关结果
                if title and keyword.split()[0].lower() in title.lower():
                    items.append(NewsItem(
                        title=title.strip(),
                        source=source.strip() if source else "Google News",
                        url=link.strip() if link else "",
                        date=pub_date.strip() if pub_date else "",
                    ))
                if len(items) >= self.max_news_per_symbol:
                    break
            return items
        except Exception as e:
            logger.debug(f"Google News RSS 抓取失败 ({keyword}): {e}")
            return []

    async def fetch_coingecko_trending(self) -> List[str]:
        """
        获取 CoinGecko  trending 列表
        用于辅助判断哪些币当前热度高
        """
        try:
            response = await self._client.get(
                "https://api.coingecko.com/api/v3/search/trending",
            )
            if response.status_code == 429:
                logger.debug("CoinGecko API rate limited")
                return []
            data = response.json()
            coins = data.get("coins", [])
            return [
                c["item"]["symbol"].upper()
                for c in coins[:15]
                if c.get("item", {}).get("symbol")
            ]
        except Exception as e:
            logger.debug(f"CoinGecko trending 获取失败: {e}")
            return []

    def _analyze_headline(self, title: str) -> Tuple[int, int, int]:
        """
        分析单条标题的情绪

        Returns:
            (positive_count, negative_count, attention_count)
        """
        title_lower = title.lower()
        pos = sum(1 for kw in _POSITIVE_KEYWORDS if kw in title_lower)
        neg = sum(1 for kw in _NEGATIVE_KEYWORDS if kw in title_lower)
        att = sum(1 for kw in _ATTENTION_KEYWORDS if kw in title_lower)
        return pos, neg, att

    async def analyze_symbol(self, symbol: str) -> SentimentResult:
        """
        分析单个币种的情绪

        Args:
            symbol: 交易对符号，如 "1000PEPE/USDT" 或 "PEPE"

        Returns:
            SentimentResult: 情绪分析结果
        """
        # 检查缓存
        now = datetime.now(timezone.utc).timestamp()
        if symbol in self._cache:
            ts, result = self._cache[symbol]
            if now - ts < self.cache_ttl:
                return result

        coin_name = symbol.replace("/USDT", "").replace("1000", "")
        keyword = self._get_search_keyword(symbol)

        # 抓取新闻
        news_items = await self.fetch_news_rss(keyword)

        # 情绪分析
        total_pos = 0
        total_neg = 0
        total_att = 0
        pos_titles: List[str] = []
        neg_titles: List[str] = []
        all_titles: List[str] = []

        for item in news_items:
            p, n, a = self._analyze_headline(item.title)
            total_pos += p
            total_neg += n
            total_att += a
            all_titles.append(item.title)
            if p > n:
                pos_titles.append(item.title)
            elif n > p:
                neg_titles.append(item.title)

        # 计算情绪得分 (-1 ~ 1)
        headline_count = len(news_items)
        if headline_count > 0:
            net = (total_pos - total_neg) / max(headline_count, 1)
            # 压缩到 -1 ~ 1 范围
            score = max(-1.0, min(1.0, net))
        else:
            score = 0.0

        result = SentimentResult(
            symbol=symbol,
            coin_name=coin_name,
            score=round(score, 3),
            headline_count=headline_count,
            positive_count=total_pos,
            negative_count=total_neg,
            attention_count=total_att,
            headlines=all_titles[:5],
            recent_positive=pos_titles[:3],
            recent_negative=neg_titles[:3],
        )

        # 写缓存
        self._cache[symbol] = (now, result)

        if headline_count > 0:
            logger.info(
                f"情绪分析 {symbol}: 得分={result.score:+.3f} | "
                f"新闻={headline_count}条 | "
                f"正面={total_pos} 负面={total_neg} 关注={total_att}"
            )

        return result

    async def get_sentiment_signal(
        self,
        symbols: List[str],
    ) -> Dict[str, float]:
        """
        获取交易信号用的情绪得分

        返回符号→得分的映射，可直接用于策略
        得分范围 -1 ~ 1，正数=偏多，负数=偏空
        """
        results = {}
        tasks = [self.analyze_symbol(s) for s in symbols]
        sentiments = await asyncio.gather(*tasks, return_exceptions=True)

        for i, symbol in enumerate(symbols):
            if isinstance(sentiments[i], Exception):
                logger.warning(f"{symbol} 情绪分析失败: {sentiments[i]}")
                results[symbol] = 0.0
            else:
                results[symbol] = sentiments[i].score

        # 如果有多个交易对共享同一币种（如 PEPE 和 1000PEPE），去重用同一分数
        return results

    async def get_trending_coins(self) -> List[str]:
        """获取当前热门币种列表"""
        return await self.fetch_coingecko_trending()
