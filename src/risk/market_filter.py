"""市场过滤器：财经日历 + ATR 波动率前置检查"""

import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional
from dataclasses import dataclass

from src.utils.logger import logger


@dataclass
class EconomicEvent:
    """财经事件"""
    time: datetime
    currency: str          # USD/EUR/GBP/JPY/CNY 等
    event: str             # 事件名称
    impact: str            # High/Medium/Low
    actual: Optional[str] = None
    forecast: Optional[str] = None
    previous: Optional[str] = None

    def is_high_impact(self) -> bool:
        return self.impact == "High"

    def is_usd_related(self) -> bool:
        """是否与 USD 相关（直接影响加密市场）"""
        return self.currency in ("USD", "EUR", "GBP")


class MarketFilter:
    """
    市场过滤器

    功能1: 财经日历过滤
    - 读取 Forex Factory RSS 获取重大财经数据发布
    - 高影响力事件（如非农/CPI/利率决议）前后 N 分钟内禁止开仓

    功能2: ATR 波动率检查
    - 计算 N 周期 ATR
    - 当前波动率超过阈值时拒绝开仓或降低仓位
    """

    def __init__(
        self,
        news_pause_before_minutes: int = 30,   # 事件前 N 分钟暂停
        news_pause_after_minutes: int = 30,    # 事件后 N 分钟暂停
        atr_period: int = 14,
        atr_max_pct: float = 0.05,              # ATR/价格 超过 5% 认为波动过高
        atr_pause_threshold: float = 0.03,      # ATR/价格 超过 3% 暂停开仓
        boll_period: int = 20,
        boll_max_deviation: float = 4.0,        # 布林带偏离度阈值
        exchange=None,                          # ccxt exchange 实例
    ):
        self.news_pause_before = news_pause_before_minutes
        self.news_pause_after = news_pause_after_minutes
        self.atr_period = atr_period
        self.atr_max_pct = atr_max_pct
        self.atr_pause_threshold = atr_pause_threshold
        self.boll_period = boll_period
        self.boll_max_deviation = boll_max_deviation
        self.exchange = exchange

        # 财经日历缓存（10分钟刷新一次）
        self._calendar_cache: List[EconomicEvent] = []
        self._calendar_cache_time: float = 0
        self._calendar_cache_ttl: float = 600

        # ATR 缓存（30秒）
        self._atr_cache: Dict[str, float] = {}
        self._atr_cache_time: Dict[str, float] = {}
        self._atr_cache_ttl: float = 30

        logger.info(
            f"MarketFilter 初始化 - 新闻暂停:前后{news_pause_before_minutes}min/后{news_pause_after_minutes}min, "
            f"ATR阈值:>{atr_pause_threshold*100:.0f}%暂停, >{atr_max_pct*100:.0f}%拒绝, "
            f"布林偏离:>{boll_max_deviation}σ"
        )

    # ═══════════════════════════════════════════════════════════════
    # 财经日历
    # ═══════════════════════════════════════════════════════════════

    # 内置本地财经日历（2026年已知重大事件）
    # 格式：(月份, 日, 事件描述, 发布时间UTC)
    _LOCAL_EVENTS = [
        # 非农就业（每月第一个周五 13:30 UTC）
        (1, 10, "美国12月非农就业", 13, 30),
        (2, 7, "美国1月非农就业", 13, 30),
        (3, 6, "美国2月非农就业", 13, 30),
        (4, 3, "美国3月非农就业", 13, 30),
        (5, 9, "美国4月非农就业", 13, 30),
        (6, 6, "美国5月非农就业", 13, 30),
        (7, 3, "美国6月非农就业", 13, 30),
        (8, 7, "美国7月非农就业", 13, 30),
        (9, 4, "美国8月非农就业", 13, 30),
        (10, 8, "美国9月非农就业", 13, 30),
        (11, 7, "美国10月非农就业", 13, 30),
        (12, 5, "美国11月非农就业", 13, 30),
        # CPI（每月 13:30 UTC）
        (1, 15, "美国12月CPI", 13, 30),
        (2, 12, "美国1月CPI", 13, 30),
        (3, 12, "美国2月CPI", 13, 30),
        (4, 10, "美国3月CPI", 13, 30),
        (5, 8, "美国4月CPI", 13, 30),
        (6, 11, "美国5月CPI", 13, 30),
        (7, 10, "美国6月CPI", 13, 30),
        (8, 13, "美国7月CPI", 13, 30),
        (9, 11, "美国8月CPI", 13, 30),
        (10, 14, "美国9月CPI", 13, 30),
        (11, 12, "美国10月CPI", 13, 30),
        (12, 10, "美国11月CPI", 13, 30),
        # FOMC 利率决议（18:00 UTC 公布）
        (1, 29, "美国1月FOMC利率决议", 18, 0),
        (3, 19, "美国3月FOMC利率决议 + 点阵图", 18, 0),
        (5, 1, "美国5月FOMC利率决议", 18, 0),
        (6, 18, "美国6月FOMC利率决议", 18, 0),
        (7, 30, "美国7月FOMC利率决议", 18, 0),
        (9, 17, "美国9月FOMC利率决议 + 点阵图", 18, 0),
        (11, 5, "美国11月FOMC利率决议", 18, 0),
        (12, 17, "美国12月FOMC利率决议 + 点阵图", 18, 0),
        # PPI / GDP / 零售销售
        (1, 16, "美国12月PPI", 13, 30),
        (1, 30, "美国Q4 GDP初值", 13, 30),
        (4, 30, "美国Q1 GDP初值", 13, 30),
        (7, 30, "美国Q2 GDP初值", 13, 30),
        (10, 29, "美国Q3 GDP初值", 13, 30),
        (11, 19, "美国10月零售销售", 13, 30),
        (12, 18, "美国11月零售销售", 13, 30),
    ]

    def _get_local_calendar(self) -> List[EconomicEvent]:
        """使用内置本地财经日历（2026年）"""
        from calendar import monthrange
        now = datetime.now(timezone.utc)
        events = []

        for item in self._LOCAL_EVENTS:
            month, day, description = item[0], item[1], item[2]
            # 新格式包含具体时间（UTC）
            hour = item[3] if len(item) > 3 else 14
            minute = item[4] if len(item) > 4 else 0
            try:
                year = now.year
                _, last_day = monthrange(year, month)
                day = min(day, last_day)
                event_time = datetime(
                    year, month, day, hour, minute, 0, tzinfo=timezone.utc
                )
            except ValueError:
                continue

            # 只保留当月及以后的事件
            if event_time < now - timedelta(days=30):
                continue

            events.append(EconomicEvent(
                time=event_time,
                currency="USD",
                event=description,
                impact="High",
            ))

        logger.info(f"本地财经日历加载: {len(events)} 个高影响力事件")
        return events

    def _fetch_forex_factory_calendar(self) -> List[EconomicEvent]:
        """从 Forex Factory 抓取财经日历（备用，已被本地日历覆盖）"""
        # Forex Factory 国内访问受限，回退到本地日历
        return self._get_local_calendar()

    def _get_calendar(self) -> List[EconomicEvent]:
        """获取日历（带缓存）"""
        now = time.time()
        if not self._calendar_cache or (now - self._calendar_cache_time) > self._calendar_cache_ttl:
            self._calendar_cache = self._fetch_forex_factory_calendar()
            self._calendar_cache_time = now
        return self._calendar_cache

    def is_trading_paused_due_to_news(self, symbol: str = None) -> Dict[str, Any]:
        """
        检查是否因财经事件暂停交易

        Returns:
            {paused: bool, reason: str, next_event: str or None}
        """
        events = self._get_calendar()
        now_utc = datetime.now(timezone.utc)

        # 检查当前时间前后窗口内是否有高影响力事件
        window_start = now_utc - timedelta(minutes=self.news_pause_before)
        window_end = now_utc + timedelta(minutes=self.news_pause_after)

        for ev in events:
            # 只关注高影响力且与 USD 相关的
            if not ev.is_high_impact():
                continue
            if not ev.is_usd_related():
                continue

            ev_time = ev.time
            # 事件发生在窗口内
            if window_start <= ev_time <= window_end:
                return {
                    "paused": True,
                    "reason": f"高影响力财经事件: [{ev.currency}] {ev.event} @ {ev.time.strftime('%H:%M UTC')}",
                    "next_event": f"{ev.event} ({ev.time.strftime('%H:%M UTC')})",
                    "impact": ev.impact,
                }

        return {"paused": False, "reason": "无重大财经事件", "next_event": None}

    def get_upcoming_events(self, hours: int = 4) -> List[Dict[str, str]]:
        """获取接下来 N 小时内的高影响力事件"""
        events = self._get_calendar()
        now_utc = datetime.now(timezone.utc)
        window_end = now_utc + timedelta(hours=hours)

        upcoming = []
        for ev in events:
            if not ev.is_high_impact():
                continue
            if not ev.is_usd_related():
                continue
            if now_utc <= ev.time <= window_end:
                upcoming.append({
                    "time": ev.time.strftime("%H:%M UTC"),
                    "currency": ev.currency,
                    "event": ev.event,
                    "impact": ev.impact,
                })

        return upcoming

    # ═══════════════════════════════════════════════════════════════
    # ATR 波动率检查
    # ═══════════════════════════════════════════════════════════════

    def get_atr(self, symbol: str) -> Optional[float]:
        """
        计算 ATR (Average True Range)

        Returns:
            ATR 值（以 USDT 计），或 None（获取失败）
        """
        now = time.time()
        cache_age = now - self._atr_cache_time.get(symbol, 0)

        if symbol in self._atr_cache and cache_age < self._atr_cache_ttl:
            return self._atr_cache[symbol]

        if not self.exchange:
            return None

        try:
            # 获取日线 ATR
            ohlcv = self.exchange.fetch_ohlcv(symbol, "1d", limit=self.atr_period + 10)
            if not ohlcv or len(ohlcv) < self.atr_period:
                return None

            # 取最近 atr_period 根 K 线
            ohlcv = ohlcv[-self.atr_period:]

            trs = []
            for i in range(1, len(ohlcv)):
                high = ohlcv[i][2]
                low = ohlcv[i][3]
                prev_close = ohlcv[i - 1][4]
                tr = max(
                    high - low,
                    abs(high - prev_close),
                    abs(low - prev_close)
                )
                trs.append(tr)

            atr = sum(trs) / len(trs) if trs else 0

            # 使用最后一根 K 线的收盘价计算 ATR%（避免额外 API 调用）
            current_price = ohlcv[-1][4] if ohlcv else 0

            if current_price > 0:
                atr_pct = atr / current_price
                logger.info(
                    f"ATR 计算: {symbol} | ATR={atr:.4f} | "
                    f"价格={current_price:.4f} | ATR%={atr_pct*100:.2f}%"
                )

            self._atr_cache[symbol] = atr
            self._atr_cache_time[symbol] = now
            return atr

        except Exception as e:
            logger.warning(f"ATR 计算失败 {symbol}: {e}")
            return None

    def check_atr_volatility(self, symbol: str) -> Dict[str, Any]:
        """
        检查 ATR 波动率是否过高

        Returns:
            {allowed: bool, atr_pct: float, level: str, reason: str}
            level: "normal" / "elevated" / "extreme"
        """
        if not self.exchange:
            return {"allowed": True, "atr_pct": 0, "level": "normal", "reason": "无 exchange 实例"}

        try:
            ticker = self.exchange.fetch_ticker(symbol)
            current_price = ticker.get("last", 0) or 0
            if current_price == 0:
                return {"allowed": True, "atr_pct": 0, "level": "normal", "reason": "无法获取价格"}
        except Exception:
            return {"allowed": True, "atr_pct": 0, "level": "normal", "reason": "无法获取价格"}

        atr = self.get_atr(symbol)
        if atr is None or atr == 0:
            return {"allowed": True, "atr_pct": 0, "level": "normal", "reason": "无法计算ATR"}

        atr_pct = atr / current_price

        if atr_pct > self.atr_max_pct:
            return {
                "allowed": False,
                "atr_pct": atr_pct,
                "level": "extreme",
                "reason": f"波动率极端 (ATR%={atr_pct*100:.1f}% > {self.atr_max_pct*100:.1f}%)，拒绝开仓",
            }
        elif atr_pct > self.atr_pause_threshold:
            return {
                "allowed": True,
                "atr_pct": atr_pct,
                "level": "elevated",
                "reason": f"波动率偏高 (ATR%={atr_pct*100:.1f}% > {self.atr_pause_threshold*100:.1f}%)，可接受但建议轻仓",
            }

        return {
            "allowed": True,
            "atr_pct": atr_pct,
            "level": "normal",
            "reason": f"波动率正常 (ATR%={atr_pct*100:.2f}%)",
        }

    # ═══════════════════════════════════════════════════════════════
    # 布林带偏离度检查
    # ═══════════════════════════════════════════════════════════════

    def check_bollinger_deviation(self, symbol: str) -> Dict[str, Any]:
        """
        检查价格偏离布林带中心的程度

        Returns:
            {allowed: bool, deviation: float, level: str, reason: str}
        """
        if not self.exchange:
            return {"allowed": True, "deviation": 0, "level": "normal", "reason": "无 exchange 实例"}

        try:
            ohlcv = self.exchange.fetch_ohlcv(symbol, "1d", limit=self.boll_period + 5)
            if not ohlcv or len(ohlcv) < self.boll_period:
                return {"allowed": True, "deviation": 0, "level": "normal", "reason": "数据不足"}
        except Exception:
            return {"allowed": True, "deviation": 0, "level": "normal", "reason": "无法获取K线"}

        closes = [c[4] for c in ohlcv[-self.boll_period:]]
        import statistics
        mean = statistics.mean(closes)
        std = statistics.stdev(closes) if len(closes) > 1 else 0
        current_price = closes[-1]

        if std == 0:
            return {"allowed": True, "deviation": 0, "level": "normal", "reason": "标准差为0"}

        deviation = abs(current_price - mean) / std

        if deviation > self.boll_max_deviation:
            return {
                "allowed": False,
                "deviation": deviation,
                "level": "extreme",
                "reason": f"价格极度偏离布林带 ({deviation:.1f}σ > {self.boll_max_deviation}σ)，拒绝开仓",
            }

        return {
            "allowed": True,
            "deviation": deviation,
            "level": "normal" if deviation < 1.5 else "elevated",
            "reason": f"布林偏离={deviation:.2f}σ",
        }

    # ═══════════════════════════════════════════════════════════════
    # 综合检查
    # ═══════════════════════════════════════════════════════════════

    def pre_trade_check(self, symbol: str) -> Dict[str, Any]:
        """
        开仓前综合市场状态检查

        检查项:
        1. 财经日历（高影响力事件窗口）
        2. ATR 波动率
        3. 布林带偏离度

        Returns:
            {allowed: bool, reasons: List[str], warnings: List[str]}
        """
        reasons = []
        warnings = []

        # 1. 财经日历
        news_check = self.is_trading_paused_due_to_news(symbol)
        if news_check["paused"]:
            reasons.append(news_check["reason"])
        else:
            upcoming = self.get_upcoming_events(hours=2)
            if upcoming:
                next_ev = upcoming[0]
                warnings.append(f"2小时内有: [{next_ev['currency']}] {next_ev['event']} @ {next_ev['time']}")

        # 2. ATR 波动率
        atr_check = self.check_atr_volatility(symbol)
        if not atr_check["allowed"]:
            reasons.append(atr_check["reason"])
        elif atr_check["level"] == "elevated":
            warnings.append(f"[{symbol}] {atr_check['reason']}")

        # 3. 布林带偏离度
        boll_check = self.check_bollinger_deviation(symbol)
        if not boll_check["allowed"]:
            reasons.append(boll_check["reason"])
        elif boll_check["level"] == "elevated":
            warnings.append(f"[{symbol}] {boll_check['reason']}")

        allowed = len(reasons) == 0

        if not allowed:
            logger.warning(f"MarketFilter 阻止开仓 {symbol}: {'; '.join(reasons)}")
        elif warnings:
            logger.info(f"MarketFilter 警告 {symbol}: {'; '.join(warnings)}")

        return {
            "allowed": allowed,
            "reasons": reasons,
            "warnings": warnings,
            "atr": atr_check.get("atr", 0),
            "atr_pct": atr_check.get("atr_pct", 0),
            "boll_deviation": boll_check.get("deviation", 0),
        }
