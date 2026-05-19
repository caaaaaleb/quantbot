"""
三层仓位池管理器
基于 Nerve-Knife 策略核心设计：

  趋势单池 (Trend Pool):  顺大趋势递增加仓，金字塔 1→2→4→8→16
  锁定单池 (Locked Pool): 大趋势反转时锁定反向仓，降低盈利门槛(1%)追踪止盈
  保留单池 (Reserved Pool): 价差 > 3% 的仓位暂时休眠，等价差缩小后释放

核心理念：顺的时候加仓，不顺的时候减仓，永远不爆仓
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Any

import numpy as np

from src.strategy.multi_timeframe import TrendResult, TrendDirection
from src.utils.logger import logger


@dataclass
class PoolPosition:
    """池中持仓"""
    symbol: str
    side: str              # 'long' / 'short'
    entry_price: float
    quantity: float
    layer: int             # 0-4 金字塔层, 5 海豹突击队
    entry_time: str
    pool: str              # 'trend' / 'locked' / 'reserved'
    trail_high: float      # 追踪止盈最高价 (long)
    trail_low: float       # 追踪止盈最低价 (short)
    trail_active: bool     # 追踪止盈是否激活
    lock_price: float = 0.0  # 锁定时价格
    order_id: str = ""     # 交易所订单 ID

    def pnl_pct(self, current_price: float) -> float:
        if self.entry_price <= 0:
            return 0.0
        if self.side == 'long':
            return (current_price - self.entry_price) / self.entry_price
        else:
            return (self.entry_price - current_price) / self.entry_price

    def unrealized_pnl(self, current_price: float) -> float:
        if self.side == 'long':
            return (current_price - self.entry_price) * self.quantity
        else:
            return (self.entry_price - current_price) * self.quantity


@dataclass
class PoolConfig:
    """仓位池配置"""
    base_size_usd: float = 50.0          # 基础开仓金额 (USD)
    max_trend_layers: int = 5            # 最大金字塔层数 (0-4, 5=海豹)
    spacing_pct: float = 0.55            # 加仓最小间距 (%)
    locked_profit_trigger: float = 1.0   # 锁定单启动追踪的盈利 (%)
    locked_trail_pullback: float = 0.3   # 锁定单追踪回撤平仓 (%)
    reserved_threshold: float = 3.0      # 保留单价差阈值 (%)
    seal_team_trigger_pct: float = 3.0   # 海豹突击队触发 (管壁突破 %)
    enable_seal_team: bool = True        # 是否启用海豹突击队
    enable_pyramid: bool = True          # 是否启用金字塔加仓


class PositionPools:
    """
    三层仓位池管理器

    用法:
        pools = PositionPools(config, risk_manager, trader)
        pools.update_trend(symbol, trend_result)        # 每 tick 更新趋势
        pools.evaluate(symbol, current_price, balance)   # 评估池状态并生成操作
    """

    def __init__(self, config: PoolConfig, risk_manager=None, trader=None):
        self.cfg = config
        self.risk_manager = risk_manager
        self.trader = trader

        # 三层仓位池: symbol -> [PoolPosition]
        self.trend: Dict[str, List[PoolPosition]] = {}
        self.locked: Dict[str, List[PoolPosition]] = {}
        self.reserved: Dict[str, List[PoolPosition]] = {}

        # 当前大趋势方向
        self.direction: Dict[str, TrendDirection] = {}

        # 已平仓统计
        self.closed_pnl: float = 0.0
        self.total_closed: int = 0

        logger.info(
            f"PositionPools 初始化: base=${config.base_size_usd:.0f}, "
            f"layers={config.max_trend_layers}, "
            f"spacing={config.spacing_pct}%, "
            f"locked_tp={config.locked_profit_trigger}%/pullback={config.locked_trail_pullback}%, "
            f"reserved={config.reserved_threshold}%, "
            f"seal_team={config.enable_seal_team}"
        )

    # ── 趋势更新 ──────────────────────────────────────────────────

    def update_trend(self, symbol: str, trend: TrendResult):
        """
        每 tick 调用：更新大趋势方向，触发锁仓逻辑
        """
        old_dir = self.direction.get(symbol)
        new_dir = trend.big_trend

        if new_dir == TrendDirection.WEAK:
            return

        # 大趋势确认反转 → 锁定反向仓位
        if old_dir is not None and old_dir != new_dir and trend.big_stable:
            logger.info(
                f"🔄 {symbol} 大趋势反转: {old_dir.value} → {new_dir.value} | "
                f"HAS={trend.big_has_color} | 锁定反向仓位"
            )
            self._lock_opposite_positions(symbol, new_dir)

        self.direction[symbol] = new_dir

    def _lock_opposite_positions(self, symbol: str, new_direction: TrendDirection, current_price: float = 0):
        """
        大趋势反转：将趋势池中与 new_direction 反向的仓位移入锁定池/保留池
        """
        if symbol not in self.trend:
            return

        opposite_side = 'short' if new_direction == TrendDirection.UP else 'long'
        to_remove = []

        for pos in self.trend[symbol]:
            if pos.side == opposite_side:
                to_remove.append(pos)

        for pos in to_remove:
            self.trend[symbol].remove(pos)
            pos.lock_price = current_price if current_price > 0 else pos.entry_price

            # 价差 > reserved_threshold → 保留池
            if current_price > 0:
                gap = abs(current_price - pos.entry_price) / pos.entry_price
                if gap > self.cfg.reserved_threshold / 100.0:
                    pos.pool = 'reserved'
                    self.reserved.setdefault(symbol, []).append(pos)
                    logger.info(f"  📦 {symbol} L{pos.layer} {pos.side} → 保留池 (价差={gap*100:.1f}%)")
                    continue

            pos.pool = 'locked'
            pos.trail_high = current_price if pos.side == 'long' and current_price > 0 else pos.entry_price
            pos.trail_low = current_price if pos.side == 'short' and current_price > 0 else pos.entry_price
            pos.trail_active = False
            self.locked.setdefault(symbol, []).append(pos)
            logger.info(f"  🔒 {symbol} L{pos.layer} {pos.side} → 锁定池")

        # 清理空列表
        if symbol in self.trend and not self.trend[symbol]:
            del self.trend[symbol]

    # ── 趋势池操作 ────────────────────────────────────────────────

    def can_add_trend_layer(self, symbol: str, side: str, current_price: float) -> tuple[bool, str]:
        """
        检查是否可以在趋势池中新加一层

        Returns:
            (allowed, reason)
        """
        if not self.cfg.enable_pyramid:
            return False, "pyramid_disabled"

        same_side = self._get_same_side_trend(symbol, side)

        if len(same_side) >= self.cfg.max_trend_layers:
            return False, f"max_layers({len(same_side)}/{self.cfg.max_trend_layers})"

        # 检查间距：新层必须与前一层有一定距离
        if same_side:
            nearest = min(same_side, key=lambda p: abs(p.entry_price - current_price))
            gap_pct = abs(current_price - nearest.entry_price) / nearest.entry_price * 100
            if gap_pct < self.cfg.spacing_pct:
                return False, f"too_close({gap_pct:.2f}% < {self.cfg.spacing_pct}%)"

        return True, "ok"

    def get_next_layer_size(self, symbol: str, side: str, current_price: float, balance: float) -> tuple[int, float]:
        """
        计算下一层的金字塔仓位

        Returns:
            (layer_index, quantity)
        """
        same_side = self._get_same_side_trend(symbol, side)
        next_layer = len(same_side)  # 0-indexed

        if next_layer >= self.cfg.max_trend_layers:
            return next_layer, 0.0

        # 金字塔: 2^layer * base
        scale = 2 ** next_layer
        amount_usd = min(self.cfg.base_size_usd * scale, balance * 0.5)
        qty = amount_usd / current_price if current_price > 0 else 0

        return next_layer, qty

    def add_trend_position(self, symbol: str, side: str, price: float, quantity: float) -> Optional[PoolPosition]:
        """向趋势池添加新仓位"""
        same_side = self._get_same_side_trend(symbol, side)
        layer = len(same_side)

        pos = PoolPosition(
            symbol=symbol,
            side=side,
            entry_price=price,
            quantity=quantity,
            layer=layer,
            entry_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            pool='trend',
            trail_high=price,
            trail_low=price,
            trail_active=False,
        )

        self.trend.setdefault(symbol, []).append(pos)
        logger.info(
            f"📈 趋势池加仓: {symbol} {side.upper()} L{layer} | "
            f"价格={price:.8g} | 量={quantity:.6f} | "
            f"同向层数={layer + 1}/{self.cfg.max_trend_layers}"
        )
        return pos

    def _get_same_side_trend(self, symbol: str, side: str) -> List[PoolPosition]:
        """获取趋势池中同方向的仓位"""
        return [p for p in self.trend.get(symbol, []) if p.side == side]

    def _get_same_side_locked(self, symbol: str, side: str) -> List[PoolPosition]:
        """获取锁定池中同方向的仓位"""
        return [p for p in self.locked.get(symbol, []) if p.side == side]

    # ── 锁定池操作 ────────────────────────────────────────────────

    def check_locked_exits(self, symbol: str, current_price: float) -> List[dict]:
        """
        检查锁定池中是否有仓位满足退出条件

        逻辑：任一单独仓位盈利 ≥ 1% → 启动追踪 → 回撤 0.3% → 平仓

        Returns:
            [{'action': 'CLOSE_LOCKED', 'position': PoolPosition, 'reason': str}, ...]
        """
        actions = []
        if symbol not in self.locked:
            return actions

        trigger = self.cfg.locked_profit_trigger / 100.0
        pullback = self.cfg.locked_trail_pullback / 100.0

        for pos in list(self.locked[symbol]):
            pnl = pos.pnl_pct(current_price)

            # 更新追踪最高/最低价
            if pos.side == 'long' and current_price > pos.trail_high:
                pos.trail_high = current_price
            if pos.side == 'short' and current_price < pos.trail_low:
                pos.trail_low = current_price

            # 激活追踪止盈（盈利超过触发线）
            if pnl >= trigger and not pos.trail_active:
                pos.trail_active = True
                logger.info(f"  🎯 {symbol} L{pos.layer} {pos.side} 锁定仓追踪激活 (盈利={pnl*100:.2f}%)")

            # 追踪止盈退出
            if pos.trail_active:
                if pos.side == 'long':
                    pullback_pct = (pos.trail_high - current_price) / pos.trail_high
                    if pullback_pct >= pullback:
                        actions.append({
                            'action': 'CLOSE_LOCKED',
                            'position': pos,
                            'reason': f'追踪止盈(最高={pos.trail_high:.4f}, 回撤={pullback_pct*100:.2f}%)'
                        })
                else:
                    pullback_pct = (current_price - pos.trail_low) / pos.trail_low if pos.trail_low > 0 else 0
                    if pullback_pct >= pullback:
                        actions.append({
                            'action': 'CLOSE_LOCKED',
                            'position': pos,
                            'reason': f'追踪止盈(最低={pos.trail_low:.4f}, 回撤={pullback_pct*100:.2f}%)'
                        })

        return actions

    def remove_locked_position(self, symbol: str, pos: PoolPosition):
        """从锁定池移除已平仓的仓位"""
        if symbol in self.locked and pos in self.locked[symbol]:
            self.locked[symbol].remove(pos)
            if not self.locked[symbol]:
                del self.locked[symbol]

    # ── 保留池操作 ────────────────────────────────────────────────

    def check_reserved_release(self, symbol: str, current_price: float) -> List[PoolPosition]:
        """
        检查保留池中是否有仓位可以释放

        条件：价差 ≤ reserved_threshold (3%)
        释放方向：与当前大趋势同向 → 趋势池；反向 → 锁定池
        """
        released = []
        if symbol not in self.reserved:
            return released

        threshold = self.cfg.reserved_threshold / 100.0
        cur_dir = self.direction.get(symbol)

        for pos in list(self.reserved[symbol]):
            gap = abs(current_price - pos.entry_price) / pos.entry_price
            if gap <= threshold:
                self.reserved[symbol].remove(pos)

                pos_side_dir = TrendDirection.UP if pos.side == 'long' else TrendDirection.DOWN
                if cur_dir and pos_side_dir == cur_dir:
                    pos.pool = 'trend'
                    self.trend.setdefault(symbol, []).append(pos)
                    logger.info(f"  📤 {symbol} L{pos.layer} {pos.side} 保留→趋势 (价差={gap*100:.1f}%)")
                else:
                    pos.pool = 'locked'
                    pos.trail_active = False
                    pos.trail_high = current_price
                    pos.trail_low = current_price
                    self.locked.setdefault(symbol, []).append(pos)
                    logger.info(f"  📤 {symbol} L{pos.layer} {pos.side} 保留→锁定 (价差={gap*100:.1f}%)")
                released.append(pos)

        if symbol in self.reserved and not self.reserved[symbol]:
            del self.reserved[symbol]

        return released

    # ── 海豹突击队 ────────────────────────────────────────────────

    def calculate_seal_team(
        self, symbol: str, side: str, current_price: float, balance: float
    ) -> tuple[bool, float]:
        """
        计算海豹突击队开仓量

        条件（严格）：
        - 趋势池已有 ≥ 2 层浮亏
        - M1 管壁反向突破 3%

        目标：使全部趋势仓在 +1% 价格变动时整体盈利平仓

        Returns:
            (should_trigger, quantity)
        """
        if not self.cfg.enable_seal_team:
            return False, 0.0

        same_side = self._get_same_side_trend(symbol, side)
        if len(same_side) < 2:
            return False, 0.0

        # 计算整体浮亏
        total_qty = sum(p.quantity for p in same_side)
        total_cost = sum(p.quantity * p.entry_price for p in same_side)
        avg_entry = total_cost / total_qty if total_qty > 0 else 0
        if avg_entry <= 0:
            return False, 0.0

        # 当前是否在浮亏
        if side == 'long' and current_price >= avg_entry:
            return False, 0.0
        if side == 'short' and current_price <= avg_entry:
            return False, 0.0

        # 海豹：目标在 avg_entry + 1% 处全部解套
        target_pct = 1.01
        if side == 'long':
            target_price = avg_entry * target_pct
            current_loss = total_qty * (avg_entry - current_price)
            profit_per_unit = target_price - current_price
        else:
            target_price = avg_entry / target_pct
            current_loss = total_qty * (current_price - avg_entry)
            profit_per_unit = current_price - target_price

        if profit_per_unit <= 0:
            return False, 0.0

        seal_qty = current_loss / profit_per_unit
        max_qty = (balance * 0.8) / current_price
        seal_qty = min(seal_qty, max_qty)

        if seal_qty <= 0:
            return False, 0.0

        logger.warning(
            f"🦭 海豹突击队 {symbol} {side} | "
            f"浮亏=${current_loss:.2f} | 均价={avg_entry:.4f} | "
            f"目标价={target_price:.4f} | 量={seal_qty:.4f}"
        )

        return True, seal_qty

    def add_seal_position(self, symbol: str, side: str, price: float, quantity: float) -> PoolPosition:
        """记录海豹突击队仓位"""
        pos = PoolPosition(
            symbol=symbol,
            side=side,
            entry_price=price,
            quantity=quantity,
            layer=5,  # 海豹标记
            entry_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            pool='trend',  # 海豹也算趋势池（特攻）
            trail_high=price,
            trail_low=price,
            trail_active=False,
        )
        self.trend.setdefault(symbol, []).append(pos)
        return pos

    # ── 综合评估 ──────────────────────────────────────────────────

    def evaluate(
        self, symbol: str, trend: TrendResult, current_price: float, balance: float
    ) -> List[dict]:
        """
        综合评估：根据多时间框架趋势 + 三层池状态，生成操作列表

        Returns:
            [{'action': 'OPEN_TREND'|'CLOSE_LOCKED'|'CLOSE_ALL'|'HOLD',
              'side': 'long'|'short',
              'quantity': float,
              'layer': int,
              'reason': str}, ...]
        """
        actions = []

        # 1. 更新趋势方向
        self.update_trend(symbol, trend)

        # 2. 检查保留池释放
        self.check_reserved_release(symbol, current_price)

        # 3. 检查锁定池退出
        locked_exits = self.check_locked_exits(symbol, current_price)
        actions.extend(locked_exits)

        # 4. 趋势池操作
        cur_dir = self.direction.get(symbol)
        if cur_dir and cur_dir != TrendDirection.WEAK:
            side = 'long' if cur_dir == TrendDirection.UP else 'short'

            # 4a. 趋势加仓检查
            if trend.should_add_to_trend():
                can_add, reason = self.can_add_trend_layer(symbol, side, current_price)
                if can_add:
                    layer, qty = self.get_next_layer_size(symbol, side, current_price, balance)
                    if qty > 0:
                        actions.append({
                            'action': 'OPEN_TREND',
                            'side': side,
                            'quantity': qty,
                            'layer': layer,
                            'reason': f'金字塔L{layer} | 大={trend.big_trend.value} 小={trend.small_trend.value}'
                        })

            # 4b. 趋势减仓（小趋势震荡）
            if trend.should_reduce_trend():
                # 每次只平盈利最多的一单，不平全部（避免震荡期集体平仓）
                same_side = self._get_same_side_trend(symbol, side)
                profitable = [p for p in same_side if p.pnl_pct(current_price) > 0]
                if profitable:
                    best = max(profitable, key=lambda p: p.pnl_pct(current_price))
                    actions.append({
                        'action': 'CLOSE_TREND',
                        'side': side,
                        'quantity': best.quantity,
                        'layer': best.layer,
                        'position': best,
                        'reason': f'小趋势震荡减仓 (盈利={best.pnl_pct(current_price)*100:.2f}%)'
                    })

            # 4c. 海豹突击队检查
            has_loss = any(
                p.pnl_pct(current_price) < 0
                for p in self._get_same_side_trend(symbol, side)
            )
            if has_loss and trend.tube_breakout:
                trigger, seal_qty = self.calculate_seal_team(symbol, side, current_price, balance)
                if trigger:
                    actions.append({
                        'action': 'SEAL_TEAM',
                        'side': side,
                        'quantity': seal_qty,
                        'layer': 5,
                        'reason': f'管壁突破 {trend.tube_breakout_direction.value if trend.tube_breakout_direction else "?"}'
                    })

        return actions

    # ── 仓位查询 ──────────────────────────────────────────────────

    def get_all_positions(self) -> Dict[str, List[PoolPosition]]:
        """获取所有池中的仓位（按 symbol 分组）"""
        result: Dict[str, List[PoolPosition]] = {}
        for pool in [self.trend, self.locked, self.reserved]:
            for symbol, positions in pool.items():
                result.setdefault(symbol, []).extend(positions)
        return result

    def get_total_exposure(self, symbol: str, current_price: float) -> dict:
        """计算某 symbol 的总敞口"""
        longs = 0.0
        shorts = 0.0
        for pool in [self.trend, self.locked, self.reserved]:
            for pos in pool.get(symbol, []):
                if pos.side == 'long':
                    longs += pos.quantity * current_price
                else:
                    shorts += pos.quantity * current_price
        return {
            'long_notional': longs,
            'short_notional': shorts,
            'net': longs - shorts,
            'trend_layers': len(self._get_same_side_trend(symbol, 'long')) + len(self._get_same_side_trend(symbol, 'short')),
            'locked_count': len(self.locked.get(symbol, [])),
            'reserved_count': len(self.reserved.get(symbol, [])),
        }

    def get_pool_summary(self) -> dict:
        """获取三层池概况"""
        trend_count = sum(len(v) for v in self.trend.values())
        locked_count = sum(len(v) for v in self.locked.values())
        reserved_count = sum(len(v) for v in self.reserved.values())
        return {
            'trend': trend_count,
            'locked': locked_count,
            'reserved': reserved_count,
            'total': trend_count + locked_count + reserved_count,
            'symbols': len(self.direction),
            'closed_pnl': round(self.closed_pnl, 2),
        }
