"""
QuantBot ↔ AI-Trader 双向信号桥接适配器

QuantBot → AI-Trader: 将 SignalResult 转为 RealtimeSignalRequest 推送到 AI-Trader
AI-Trader → QuantBot: 拉取外部 agent 信号，加权融合到最终评分
"""

import json
import time
import threading
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

from src.utils.logger import logger


@dataclass
class RealtimeSignalRequest:
    """AI-Trader 实时信号请求模型"""
    market: str           # "crypto" | "stock"
    action: str           # "BUY" | "SELL"
    symbol: str
    price: float
    quantity: float = 0.0
    executed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    content: str = ""     # 信号描述 / JSON metadata


@dataclass
class ExternalSignal:
    """从 AI-Trader 拉取的外部信号"""
    agent_id: str
    agent_name: str
    market: str
    action: str           # BUY / SELL / HOLD
    symbol: str
    price: float
    score: float          # 信号强度 (0~1)
    confidence: float     # 置信度 (0~1)
    timestamp: str


class SignalBridge:
    """QuantBot ↔ AI-Trader 信号桥"""

    def __init__(
        self,
        ai_trader_url: str = "http://localhost:3000",
        api_key: str = "",
        agent_name: str = "QuantBot",
        agent_id: str = "quantbot-1",
        publish_signals: bool = True,
        consume_signals: bool = True,
        external_weight: float = 0.15,
    ):
        self.ai_trader_url = ai_trader_url.rstrip("/")
        self.api_key = api_key
        self.agent_name = agent_name
        self.agent_id = agent_id
        self.publish_signals = publish_signals
        self.consume_signals = consume_signals
        self.external_weight = external_weight
        self.enabled = True
        self.session = requests.Session()
        if api_key:
            self.session.headers["Authorization"] = f"Bearer {api_key}"
        self.session.headers["Content-Type"] = "application/json"

        # 本地信号存储（用于 GET /api/signals/feed）
        self._signal_store: List[Dict] = []
        self._signal_ttl = 300  # 5 分钟
        self._lock = threading.Lock()

        logger.info(
            f"SignalBridge: url={ai_trader_url}, publish={publish_signals}, "
            f"consume={consume_signals}, ext_weight={external_weight}"
        )

    # ═══════════════════════════════════════════════════════════════
    # QuantBot → AI-Trader (信号发布)
    # ═══════════════════════════════════════════════════════════════

    def publish_signal(
        self,
        symbol: str,
        signal: str,
        score: float,
        price: float,
        confidence: float = 0.0,
        metadata: Optional[Dict] = None,
        quantity: float = 0.0,
    ) -> bool:
        """
        将 QuantBot 信号推送到 AI-Trader

        Returns:
            bool: 是否推送成功
        """
        if not self.publish_signals or not self.enabled:
            return False

        req = RealtimeSignalRequest(
            market="crypto",
            action=signal,
            symbol=symbol,
            price=price,
            quantity=quantity,
            content=json.dumps({
                "agent": self.agent_name,
                "agent_id": self.agent_id,
                "score": score,
                "confidence": confidence,
                "metadata": metadata or {},
            }, ensure_ascii=False),
        )

        # 同时存入本地信号存储
        self._store_signal({
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "market": req.market,
            "action": req.action,
            "symbol": req.symbol,
            "price": req.price,
            "score": score,
            "confidence": confidence,
            "timestamp": req.executed_at,
        })

        # 自评模式（localhost）：本地已存储，跳过 HTTP 调用避免死锁
        if "localhost" in self.ai_trader_url or "127.0.0.1" in self.ai_trader_url:
            return True

        try:
            resp = self.session.post(
                f"{self.ai_trader_url}/api/signals/realtime",
                json={
                    "market": req.market,
                    "action": req.action,
                    "symbol": req.symbol,
                    "price": req.price,
                    "quantity": req.quantity,
                    "executed_at": req.executed_at,
                    "content": req.content,
                },
                timeout=5,
            )
            if resp.status_code < 400:
                logger.debug(f"SignalBridge: 已推送 {symbol} {signal} → AI-Trader")
                return True
            else:
                logger.warning(f"SignalBridge: 推送失败 {resp.status_code}")
                return False
        except requests.ConnectionError:
            logger.debug(f"SignalBridge: AI-Trader 未连接")
            return False
        except Exception as e:
            logger.warning(f"SignalBridge: 推送异常 {e}")
            return False

    # ═══════════════════════════════════════════════════════════════
    # AI-Trader → QuantBot (信号消费)
    # ═══════════════════════════════════════════════════════════════

    def fetch_external_signals(self, symbol: Optional[str] = None) -> List[ExternalSignal]:
        """
        从 AI-Trader 拉取外部 agent 信号

        Args:
            symbol: 可选，按币种过滤

        Returns:
            List[ExternalSignal]: 外部信号列表
        """
        if not self.consume_signals or not self.enabled:
            return []

        # 自评模式（localhost）：直接读本地 store，跳过 HTTP
        if "localhost" in self.ai_trader_url or "127.0.0.1" in self.ai_trader_url:
            feed = self.get_feed(symbol=symbol)
            return [
                ExternalSignal(
                    agent_id=s.get("agent_id", ""),
                    agent_name=s.get("agent_name", ""),
                    market=s.get("market", "crypto"),
                    action=s.get("action", "HOLD"),
                    symbol=s.get("symbol", ""),
                    price=s.get("price", 0),
                    score=s.get("score", 0),
                    confidence=s.get("confidence", 0),
                    timestamp=s.get("timestamp", ""),
                )
                for s in feed
                if s.get("agent_id") != self.agent_id
            ]

        try:
            resp = self.session.get(
                f"{self.ai_trader_url}/api/signals/feed",
                params={"symbol": symbol} if symbol else {},
                timeout=5,
            )
            if resp.status_code != 200:
                return []

            data = resp.json()
            signals_raw = data if isinstance(data, list) else data.get("signals", [])

            return [
                ExternalSignal(
                    agent_id=s.get("agent_id", ""),
                    agent_name=s.get("agent_name", ""),
                    market=s.get("market", "crypto"),
                    action=s.get("action", "HOLD"),
                    symbol=s.get("symbol", ""),
                    price=s.get("price", 0),
                    score=s.get("score", 0),
                    confidence=s.get("confidence", 0),
                    timestamp=s.get("timestamp", ""),
                )
                for s in signals_raw
                if s.get("agent_id") != self.agent_id  # 排除自己的信号
            ]
        except requests.ConnectionError:
            return []
        except Exception as e:
            logger.debug(f"SignalBridge: 拉取外部信号失败 {e}")
            return []

    def merge_external_signals(
        self,
        quantbot_score: float,
        external_signals: List[ExternalSignal],
    ) -> tuple[float, Dict]:
        """
        融合外部信号到 QuantBot 评分

        Args:
            quantbot_score: QuantBot 内部信号分 (-1 ~ 1)
            external_signals: 外部信号列表

        Returns:
            (merged_score, summary_detail)
        """
        if not external_signals:
            return quantbot_score, {"external_count": 0, "external_score": 0.0}

        # 外部信号分：取加权平均 (weight by confidence)
        total_w = sum(s.confidence for s in external_signals) or 1.0
        external_score = sum(
            (1.0 if s.action == "BUY" else -1.0 if s.action == "SELL" else 0.0)
            * s.confidence
            for s in external_signals
        ) / total_w

        # 归一化外部分数
        external_score = max(-1.0, min(1.0, external_score))

        # 加权融合
        w = self.external_weight
        merged = quantbot_score * (1 - w) + external_score * w

        detail = {
            "external_count": len(external_signals),
            "external_score": round(external_score, 4),
            "external_weight": w,
            "merged_score": round(merged, 4),
            "sources": [
                {"agent": s.agent_name, "action": s.action, "score": s.score, "confidence": s.confidence}
                for s in external_signals
            ],
        }

        logger.info(
            f"SignalBridge: 融合外部信号 | "
            f"内部={quantbot_score:.3f} 外部={external_score:.3f}(×{w}) → {merged:.3f} | "
            f"来源={len(external_signals)}个agent"
        )

        return merged, detail

    # ═══════════════════════════════════════════════════════════════
    # 本地信号存储（供 AI-Trader API 接口使用）
    # ═══════════════════════════════════════════════════════════════

    def _store_signal(self, signal: Dict):
        """存入本地信号存储（线程安全）"""
        with self._lock:
            self._signal_store.append(signal)
            # 清理过期信号
            now = time.time()
            self._signal_store = [
                s for s in self._signal_store
                if now - self._parse_timestamp(s.get("timestamp", "")) < self._signal_ttl
            ]
            # 限制最大 500 条
            if len(self._signal_store) > 500:
                self._signal_store = self._signal_store[-500:]

    def _parse_timestamp(self, ts: str) -> float:
        """解析 ISO 时间戳"""
        try:
            return datetime.fromisoformat(ts).timestamp()
        except Exception:
            return time.time()

    def get_feed(self, limit: int = 50, symbol: Optional[str] = None) -> List[Dict]:
        """获取本地信号 feed"""
        with self._lock:
            feed = self._signal_store[:]
        if symbol:
            feed = [s for s in feed if s.get("symbol") == symbol]
        return feed[-limit:]

    def store_external_signal(self, signal_data: Dict):
        """存储外部接收到的信号（用于 API 接收）"""
        entry = {
            "agent_id": signal_data.get("agent_id", "external"),
            "agent_name": signal_data.get("agent_name", "External"),
            "market": signal_data.get("market", "crypto"),
            "action": signal_data.get("action", "HOLD"),
            "symbol": signal_data.get("symbol", ""),
            "price": signal_data.get("price", 0),
            "score": signal_data.get("score", 0),
            "confidence": signal_data.get("confidence", 0),
            "timestamp": signal_data.get("executed_at", datetime.now(timezone.utc).isoformat()),
        }
        self._store_signal(entry)
