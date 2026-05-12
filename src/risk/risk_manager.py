"""风控模块"""

from typing import Dict, Any, Optional
from dataclasses import dataclass, field
from datetime import datetime

from src.strategy.base import Signal
from src.utils.logger import logger


@dataclass
class Position:
    """持仓信息"""
    symbol: str
    side: str          # long / short
    entry_price: float
    quantity: float
    entry_time: str
    stop_loss: float = 0.0
    take_profit: float = 0.0
    atr_stop_loss: float = 0.0  # ATR dynamic stop loss price
    atr_take_profit: float = 0.0  # ATR dynamic take profit price
    trailing_stop: float = 0.0   # Trailing stop trigger price
    highest_price: float = 0.0   # Highest price since entry (long)
    lowest_price: float = 0.0    # Lowest price since entry (short)
    partial_tp1_done: bool = False  # First partial TP (2%) triggered
    partial_tp2_done: bool = False  # Second partial TP (5%) triggered
    regime: str = "unknown"      # Market regime at entry
    gross_long: float = 0.0     # Hedge Mode long qty
    gross_short: float = 0.0    # Hedge Mode short qty
    synced: bool = False         # Synced from exchange (SL/TP are calculated)

    def unrealized_pnl(self, current_price: float) -> float:
        """计算未实现盈亏"""
        if self.side == "long":
            return (current_price - self.entry_price) * self.quantity
        else:
            return (self.entry_price - current_price) * self.quantity

    def pnl_pct(self, current_price: float) -> float:
        """计算盈亏百分比"""
        if self.entry_price == 0 or current_price <= 0:
            return 0.0
        return self.unrealized_pnl(current_price) / (self.entry_price * self.quantity)


@dataclass
class StopLossResult:
    """止损止盈检查结果"""
    action: str = "HOLD"           # HOLD / STOP_LOSS / TAKE_PROFIT / PARTIAL_TP / TRAILING_STOP
    close_qty: float = 0.0        # Qty to close (0=no close)
    updated_trailing_stop: float = 0.0  # Updated trailing stop price


@dataclass
class RiskStats:
    """风控统计"""
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl: float = 0.0
    daily_loss: float = 0.0
    daily_trades: int = 0
    last_reset: str = field(default_factory=lambda: datetime.now().strftime('%Y-%m-%d'))


class RiskManager:
    """风控管理器"""

    def __init__(
        self,
        max_position_size: float = 0.2,
        stop_loss: float = 0.02,
        take_profit: float = 0.05,
        max_daily_loss: float = 0.1,
        max_trades_per_day: int = 20,
        max_positions: int = 3,
        atr_stop_loss_enabled: bool = True,
        atr_multiplier: float = 1.5,
        atr_tp_multiplier: float = 3.0,
        atr_tsl_multiplier: float = 1.5,
        vol_adj_enabled: bool = True,
        consecutive_loss_limit: int = 3,
        consecutive_loss_reduction: float = 0.5,
        tp1_pct: float = 0.02,
        tp1_portion: float = 0.30,
        tp2_pct: float = 0.05,
        tp2_portion: float = 0.50,
        enable_trailing: bool = True,
        time_exit_enabled: bool = True,
        time_exit_minutes: int = 20,
        time_exit_min_profit: float = 0.015,
        leverage: int = 1,
        atr_period: int = 14,
        tp_min_profit: float = 0.0,
        exchange=None,
        vol_threshold: float = 0.03,
        atr_pause_threshold: float = 0.12,
        atr_max_pct: float = 0.25,
    ):
        self.max_position_size = max_position_size
        self.stop_loss = stop_loss
        self.take_profit = take_profit
        self.max_daily_loss = max_daily_loss
        self.max_trades_per_day = max_trades_per_day
        self.max_positions = max_positions
        self.atr_stop_loss_enabled = atr_stop_loss_enabled
        self.atr_multiplier = atr_multiplier
        self.atr_tp_multiplier = atr_tp_multiplier
        self.atr_tsl_multiplier = atr_tsl_multiplier
        self.vol_adj_enabled = vol_adj_enabled
        self.consecutive_loss_limit = consecutive_loss_limit
        self.consecutive_loss_reduction = consecutive_loss_reduction
        self.tp1_pct = tp1_pct
        self.tp1_portion = tp1_portion
        self.tp2_pct = tp2_pct
        self.tp2_portion = tp2_portion
        self.enable_trailing = enable_trailing
        self.time_exit_enabled = time_exit_enabled
        self.time_exit_minutes = time_exit_minutes
        self.time_exit_min_profit = time_exit_min_profit
        self.leverage = leverage
        self.atr_period = atr_period
        self.vol_threshold = vol_threshold
        self.atr_pause_threshold = atr_pause_threshold
        self.atr_max_pct = atr_max_pct
        self.tp_min_profit = tp_min_profit
        self.exchange = exchange
        self._consecutive_losses = 0
        self._daily_initial_balance: Optional[float] = None
        # positions: symbol -> {side: Position}（支持同一币多空双向持仓）
        self.positions: Dict[str, Dict[str, Position]] = {}
        self.stats = RiskStats()
        logger.info(
            f"RiskManager: pos_size={max_position_size*100}%, "
            f"SL={stop_loss*100}%, TP={take_profit*100}%, "
            f"ATR_SL={atr_multiplier}x, ATR_TP={atr_tp_multiplier}x, ATR_TSL={atr_tsl_multiplier}x, "
            f"TP1={tp1_pct*100:.0f}%x{tp1_portion*100:.0f}%, TP2={tp2_pct*100:.0f}%x{tp2_portion*100:.0f}%, "
            f"trailing={enable_trailing}, leverage={leverage}, "
            f"time_exit={time_exit_enabled}(>{time_exit_minutes}min & >{time_exit_min_profit*100:.0f}%), "
            f"min_profit=\${tp_min_profit:.0f}"
        )
    
    def _check_daily_reset(self) -> None:
        """检查是否需要重置每日统计"""
        today = datetime.now().strftime('%Y-%m-%d')
        if self.stats.last_reset != today:
            logger.info(f"每日统计重置: {self.stats.last_reset} -> {today}")
            self.stats.daily_loss = 0.0
            self.stats.daily_trades = 0
            self.stats.last_reset = today
    
    def check_trade_allowed(
        self,
        symbol: str,
        balance: float,
        price: float,
        signal: str,
        position: Any = None,
        regime: str = "unknown",
        atr: float = 0.0,
        atr_pct: float = 0.0,
    ) -> Dict[str, Any]:
        """
        检查是否允许交易

        Args:
            symbol: 交易对
            balance: 可用余额
            price: 当前价格
            signal: 交易信号 (BUY/SELL/HOLD)
            position: 当前持仓（可选）
            regime: 市场状态
            atr: ATR 值
            atr_pct: ATR/价格 百分比

        Returns:
            dict: {allowed: bool, reason: str, size: float}
        """
        self._check_daily_reset()

        # 信号检查
        if signal == Signal.HOLD:
            return {'allowed': False, 'reason': '信号为HOLD，不交易', 'size': 0}

        # 每日交易次数检查
        if self.stats.daily_trades >= self.max_trades_per_day:
            logger.warning(f"每日交易次数已达上限: {self.stats.daily_trades}")
            return {'allowed': False, 'reason': f'日交易次数超限 ({self.stats.daily_trades}/{self.max_trades_per_day})', 'size': 0}

        # 每日亏损检查
        if self.stats.daily_loss <= -self.max_daily_loss * balance:
            logger.warning(f"每日亏损已达上限: {self.stats.daily_loss}")
            return {'allowed': False, 'reason': f'日亏损超限 ({self.stats.daily_loss:.2f})', 'size': 0}

        # 最大持仓数量检查（对冲模式：同币可有多个方向，分别计数）
        existing_sides = len(self.positions.get(symbol, {}))
        total_positions = self.get_total_position_count()
        if existing_sides == 0 and total_positions >= self.max_positions:
            logger.warning(f"已达最大持仓数量: {total_positions}/{self.max_positions}")
            return {'allowed': False, 'reason': f'已达最大持仓数量 ({total_positions}/{self.max_positions})', 'size': 0}

        # 计算仓位大小
        if price <= 0:
            return {'allowed': False, 'reason': f'无法获取 {symbol} 价格', 'size': 0}
        max_amount = balance * self.max_position_size * self.leverage
        quantity = max_amount / price

        # ATR 波动率仓位调整（meme币天然高波动，阈值可配置）
        if self.vol_adj_enabled and atr_pct > 0:
            if atr_pct > self.vol_threshold:
                adj_factor = max(0.1, 1.0 - min(0.9, (atr_pct - self.vol_threshold) / self.vol_threshold))
                adj_amount = max_amount * adj_factor
                quantity = adj_amount / price
                logger.info(
                    f"波动率降仓: {symbol} | ATR%={atr_pct*100:.2f}% | "
                    f"降权={adj_factor:.2f} | 金额={adj_amount:.2f}"
                )

        # 连续亏损仓位缩减
        if self._consecutive_losses >= self.consecutive_loss_limit:
            reduction = self.consecutive_loss_reduction
            reduced_qty = quantity * reduction
            logger.warning(
                f"连续亏损缩减: {symbol} | "
                f"连亏={self._consecutive_losses}次 | 缩减={reduction*100:.0f}% | "
                f"数量={reduced_qty:.6f}"
            )
            quantity = reduced_qty

        # 持仓方向检查（对冲模式：同币可同时有多空）
        pos_dict = self.positions.get(symbol, {})
        if signal == Signal.BUY and "long" in pos_dict:
            return {'allowed': False, 'reason': '已有多头持仓，不再加仓', 'size': 0}
        if signal == Signal.SELL and "short" in pos_dict:
            return {'allowed': False, 'reason': '已有空头持仓，不再加仓', 'size': 0}

        logger.info(
            f"交易允许: {signal} {symbol} | "
            f"数量={quantity:.6f} | 金额={max_amount:.2f}"
        )

        return {
            'allowed': True,
            'reason': 'OK',
            'size': quantity,
            'amount': max_amount
        }
    
    def check_stop_loss_take_profit(
        self,
        symbol: str,
        current_price: float,
        atr: float = 0.0
    ) -> list:
        """
        检查止损止盈（对冲模式：同币可能有多空两个持仓）
        含 ATR 动态止损 / 追踪止盈 / 分批止盈

        Returns:
            list of triggered items:
            [{"action": "STOP_LOSS"|"TAKE_PROFIT"|"PARTIAL_TP"|"TRAILING_STOP", "side": "long"|"short", "close_qty": float, "position": Position}, ...]
        """
        pos_dict = self.positions.get(symbol, {})
        if not pos_dict:
            return []

        triggered = []

        # ── 双向仓位解锁：满仓时释放同币双向持仓中较差的一边 ──
        if len(pos_dict) == 2 and self.get_total_position_count() >= self.max_positions:
            long_pos = pos_dict.get("long")
            short_pos = pos_dict.get("short")
            if long_pos and short_pos:
                long_pnl = long_pos.pnl_pct(current_price)
                short_pnl = short_pos.pnl_pct(current_price)
                if long_pnl >= short_pnl:
                    logger.warning(
                        f"🔓 双向解锁 {symbol}: 平空头(盈亏={short_pnl*100:.2f}%) 保留多头(盈亏={long_pnl*100:.2f}%) | "
                        f"总仓位={self.get_total_position_count()}/{self.max_positions}"
                    )
                    triggered.append({"action": "TIME_EXIT", "side": "short", "position": short_pos, "close_qty": short_pos.quantity})
                else:
                    logger.warning(
                        f"🔓 双向解锁 {symbol}: 平多头(盈亏={long_pnl*100:.2f}%) 保留空头(盈亏={short_pnl*100:.2f}%) | "
                        f"总仓位={self.get_total_position_count()}/{self.max_positions}"
                    )
                    triggered.append({"action": "TIME_EXIT", "side": "long", "position": long_pos, "close_qty": long_pos.quantity})

        for side, position in list(pos_dict.items()):
            pnl_pct = position.pnl_pct(current_price)
            usd_pnl = position.unrealized_pnl(current_price)

            # ── ATR 动态止损 ──
            if self.atr_stop_loss_enabled and atr > 0 and position.atr_stop_loss > 0:
                if side == "long" and current_price <= position.atr_stop_loss:
                    logger.warning(f"🚨 ATR止损 {symbol} {side} | {current_price:.4f} <= ATR_SL={position.atr_stop_loss:.4f}")
                    triggered.append({"action": "STOP_LOSS", "side": side, "position": position, "close_qty": position.quantity})
                    continue
                if side == "short" and current_price >= position.atr_stop_loss:
                    logger.warning(f"🚨 ATR止损 {symbol} {side} | {current_price:.4f} >= ATR_SL={position.atr_stop_loss:.4f}")
                    triggered.append({"action": "STOP_LOSS", "side": side, "position": position, "close_qty": position.quantity})
                    continue

            # ── 固定止损 ──
            if side == "long" and current_price <= position.stop_loss:
                logger.warning(f"🚨 固定止损 {symbol} {side} | {current_price:.4f} <= SL={position.stop_loss:.4f}")
                triggered.append({"action": "STOP_LOSS", "side": side, "position": position, "close_qty": position.quantity})
                continue
            if side == "short" and current_price >= position.stop_loss:
                logger.warning(f"🚨 固定止损 {symbol} {side} | {current_price:.4f} >= SL={position.stop_loss:.4f}")
                triggered.append({"action": "STOP_LOSS", "side": side, "position": position, "close_qty": position.quantity})
                continue

            # ── 追踪止盈 ──
            if self.enable_trailing and atr > 0:
                if side == "long":
                    if current_price > position.highest_price:
                        position.highest_price = current_price
                        new_tsl = current_price - atr * self.atr_tsl_multiplier
                        if new_tsl > position.trailing_stop:
                            position.trailing_stop = new_tsl
                    if position.trailing_stop > 0 and current_price <= position.trailing_stop and usd_pnl >= self.tp_min_profit:
                        logger.info(f"🎯 追踪止盈 {symbol} {side} | {current_price:.4f} <= TSL={position.trailing_stop:.4f}")
                        triggered.append({"action": "TRAILING_STOP", "side": side, "position": position, "close_qty": position.quantity})
                        continue
                else:
                    if current_price < position.lowest_price:
                        position.lowest_price = current_price
                        new_tsl = current_price + atr * self.atr_tsl_multiplier
                        if new_tsl < position.trailing_stop or position.trailing_stop == 0:
                            position.trailing_stop = new_tsl
                    if position.trailing_stop > 0 and current_price >= position.trailing_stop and usd_pnl >= self.tp_min_profit:
                        logger.info(f"🎯 追踪止盈 {symbol} {side} | {current_price:.4f} >= TSL={position.trailing_stop:.4f}")
                        triggered.append({"action": "TRAILING_STOP", "side": side, "position": position, "close_qty": position.quantity})
                        continue

            # ── 分批止盈 ──
            if self.tp1_portion > 0 and not position.partial_tp1_done and pnl_pct >= self.tp1_pct and usd_pnl >= self.tp_min_profit:
                close_qty = position.quantity * self.tp1_portion
                position.partial_tp1_done = True
                logger.info(f"🎯 TP1分批止盈 {symbol} {side} | 平{int(self.tp1_portion*100)}% | 数量={close_qty:.6f}")
                triggered.append({"action": "PARTIAL_TP", "side": side, "position": position, "close_qty": close_qty})
                continue
            if self.tp2_portion > 0 and not position.partial_tp2_done and pnl_pct >= self.tp2_pct and usd_pnl >= self.tp_min_profit:
                close_qty = position.quantity * self.tp2_portion
                position.partial_tp2_done = True
                logger.info(f"🎯 TP2分批止盈 {symbol} {side} | 平{int(self.tp2_portion*100)}% | 数量={close_qty:.6f}")
                triggered.append({"action": "PARTIAL_TP", "side": side, "position": position, "close_qty": close_qty})
                continue

            # ── 时间退出（持仓超时+有利润 → 平仓释放资金）──
            if self.time_exit_enabled and pnl_pct >= self.time_exit_min_profit and usd_pnl >= self.tp_min_profit:
                try:
                    entry_dt = datetime.strptime(position.entry_time, "%Y-%m-%d %H:%M:%S")
                    held_minutes = (datetime.now() - entry_dt).total_seconds() / 60
                    if held_minutes >= self.time_exit_minutes:
                        logger.info(
                            f"⏰ 时间退出 {symbol} {side} | "
                            f"持仓={held_minutes:.0f}min | 盈利={pnl_pct*100:.2f}%"
                        )
                        triggered.append({"action": "TIME_EXIT", "side": side, "position": position, "close_qty": position.quantity})
                        continue
                except (ValueError, TypeError):
                    pass

            # ── ATR 止盈 ──
            if self.atr_stop_loss_enabled and atr > 0:
                atr_tp_price_long = position.entry_price + atr * self.atr_tp_multiplier
                atr_tp_price_short = position.entry_price - atr * self.atr_tp_multiplier
                if side == "long" and current_price >= atr_tp_price_long and atr_tp_price_long > position.take_profit and usd_pnl >= self.tp_min_profit:
                    logger.info(f"🎯 ATR止盈 {symbol} {side} | {current_price:.4f} >= ATR_TP={atr_tp_price_long:.4f}")
                    triggered.append({"action": "TAKE_PROFIT", "side": side, "position": position, "close_qty": position.quantity})
                    continue
                if side == "short" and current_price <= atr_tp_price_short and atr_tp_price_short < position.take_profit and usd_pnl >= self.tp_min_profit:
                    logger.info(f"🎯 ATR止盈 {symbol} {side} | {current_price:.4f} <= ATR_TP={atr_tp_price_short:.4f}")
                    triggered.append({"action": "TAKE_PROFIT", "side": side, "position": position, "close_qty": position.quantity})
                    continue

            # ── 固定止盈（取 max(1%价格变动, $最小利润)）──
            if side == "long":
                pct_tp_price = position.entry_price * (1 + self.take_profit)
                min_tp_price = position.entry_price + self.tp_min_profit / position.quantity if position.quantity > 0 and self.tp_min_profit > 0 else pct_tp_price
                effective_tp_price = max(pct_tp_price, min_tp_price)
                if current_price >= effective_tp_price and usd_pnl >= self.tp_min_profit:
                    logger.info(f"🎯 固定止盈 {symbol} {side} | 盈利={pnl_pct*100:.2f}% (${usd_pnl:+.2f}) | TP价={effective_tp_price:.4f}")
                    triggered.append({"action": "TAKE_PROFIT", "side": side, "position": position, "close_qty": position.quantity})
                    continue
            else:
                pct_tp_price = position.entry_price * (1 - self.take_profit)
                min_tp_price = position.entry_price - self.tp_min_profit / position.quantity if position.quantity > 0 and self.tp_min_profit > 0 else pct_tp_price
                effective_tp_price = min(pct_tp_price, min_tp_price)
                if current_price <= effective_tp_price and usd_pnl >= self.tp_min_profit:
                    logger.info(f"🎯 固定止盈 {symbol} {side} | 盈利={pnl_pct*100:.2f}% (${usd_pnl:+.2f}) | TP价={effective_tp_price:.4f}")
                    triggered.append({"action": "TAKE_PROFIT", "side": side, "position": position, "close_qty": position.quantity})
                    continue
                continue

        return triggered
    
    def open_position(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        quantity: float
    ) -> Position:
        """
        开仓
        
        Args:
            symbol: 交易对
            side: 方向 (long/short)
            entry_price: 入场价格
            quantity: 数量
            
        Returns:
            Position: 持仓对象
        """
        position = Position(
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            quantity=quantity,
            entry_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        )
        
        # 设置止损止盈（固定）
        if side == "long":
            position.stop_loss = entry_price * (1 - self.stop_loss)
            position.take_profit = entry_price * (1 + self.take_profit)
            position.highest_price = entry_price
        else:
            position.stop_loss = entry_price * (1 + self.stop_loss)
            position.take_profit = entry_price * (1 - self.take_profit)
            position.lowest_price = entry_price

        # ATR 动态止损/止盈（在 exchange 可用时计算）
        if self.exchange and self.atr_stop_loss_enabled:
            try:
                ohlcv = self.exchange.fetch_ohlcv(symbol, "1d", limit=self.atr_period + 5)
                if ohlcv and len(ohlcv) >= self.atr_period:
                    trs = []
                    for i in range(1, len(ohlcv)):
                        tr = max(
                            ohlcv[i][2] - ohlcv[i][3],
                            abs(ohlcv[i][2] - ohlcv[i - 1][4]),
                            abs(ohlcv[i][3] - ohlcv[i - 1][4])
                        )
                        trs.append(tr)
                    atr = sum(trs[-self.atr_period:]) / self.atr_period if trs else 0
                    if atr > 0:
                        if side == "long":
                            position.atr_stop_loss = entry_price - atr * self.atr_multiplier
                            position.atr_take_profit = entry_price + atr * self.atr_tp_multiplier
                            position.trailing_stop = 0  # 延迟激活，等价格脱离成本区后再开始追踪
                        else:
                            position.atr_stop_loss = entry_price + atr * self.atr_multiplier
                            position.atr_take_profit = entry_price - atr * self.atr_tp_multiplier
                            position.trailing_stop = 0
                        logger.info(
                            f"ATR 动态 SL/TP: {symbol} | "
                            f"ATR={atr:.4f} | SL={position.atr_stop_loss:.4f} | "
                            f"TP={position.atr_take_profit:.4f}"
                        )
            except Exception as e:
                logger.warning(f"ATR 计算失败 {symbol}: {e}")
        
        if symbol not in self.positions:
            self.positions[symbol] = {}
        self.positions[symbol][side] = position
        self.stats.total_trades += 1
        self.stats.daily_trades += 1
        
        logger.info(
            f"📈 开仓成功: {side.upper()} {symbol} | "
            f"价格={entry_price:.8g} | "
            f"数量={quantity:.6f} | "
            f"止损={position.stop_loss:.8g} | "
            f"止盈={position.take_profit:.8g}"
        )
        
        return position
    
    def close_position(
        self,
        symbol: str,
        side: str,
        close_price: float
    ) -> Optional[Dict[str, Any]]:
        """
        平仓（对冲模式需指定 side）

        注意：调用方负责在交易所执行实际平仓交易，
        此方法仅更新内部持仓记录（在交易确认后调用）。
        """
        if symbol not in self.positions or side not in self.positions[symbol]:
            logger.warning(f"无持仓可平: {symbol} {side}")
            return None

        position = self.positions[symbol].pop(side)
        if not self.positions[symbol]:
            del self.positions[symbol]

        pnl = position.unrealized_pnl(close_price)
        pnl_pct = position.pnl_pct(close_price)

        # 更新连续亏损计数器
        if pnl > 0:
            self._consecutive_losses = 0
        else:
            self._consecutive_losses += 1
            logger.info(f"连续亏损计数: {self._consecutive_losses}/{self.consecutive_loss_limit}")

        # 更新统计
        self.stats.total_pnl += pnl
        self.stats.daily_loss += pnl
        
        if pnl > 0:
            self.stats.winning_trades += 1
        else:
            self.stats.losing_trades += 1
        
        result = {
            'symbol': symbol,
            'side': position.side,
            'entry_price': position.entry_price,
            'close_price': close_price,
            'quantity': position.quantity,
            'pnl': pnl,
            'pnl_pct': pnl_pct,
            'duration': position.entry_time
        }
        
        logger.info(
            f"📉 平仓完成: {position.side.upper()} {symbol} | "
            f"入场={position.entry_price:.2f} | "
            f"平仓={close_price:.2f} | "
            f"盈亏={pnl:+.4f} ({pnl_pct*100:+.2f}%)"
        )
        
        return result
    
    def sync_from_exchange(self, exchange, symbols: list):
        """
        从交易所同步现有持仓（启动时调用）

        Args:
            exchange: ccxt exchange 实例
            symbols: 交易对列表
        """
        if not exchange:
            logger.warning("sync_from_exchange: 无 exchange 实例")
            return

        # 先从交易所拉所有活跃持仓的 symbol 集合
        active_exchange_symbols = set()
        try:
            all_pos = exchange.fetch_positions()
            for p in all_pos:
                if float(p.get('contracts', 0) or 0) != 0:
                    sym = p.get('symbol', '')
                    active_exchange_symbols.add(sym)
        except Exception as e:
            logger.warning(f"获取交易所全部持仓失败: {e}")

        # 清理这些 symbol 的旧仓位（如果交易所已经没有这个方向的持仓）
        for sym in symbols:
            if sym in self.positions and sym not in active_exchange_symbols:
                del self.positions[sym]

        for symbol in symbols:
            try:
                # 获取合约信息
                position_info = exchange.fetch_positions([symbol])
                for pos in position_info:
                    if float(pos.get('contracts', 0) or 0) == 0:
                        continue
                    # 对冲模式：Bitget 返回的 side 字段直接标识多空
                    raw_side = pos.get('side', '').upper()
                    side = "long" if raw_side == "LONG" else "short"
                    entry_price = float(pos.get('entryPrice', 0) or 0)
                    quantity = float(pos.get('contracts', 0) or 0)
                    if entry_price <= 0 or quantity <= 0:
                        continue

                    # 计算 ATR
                    atr = 0.0
                    if self.atr_stop_loss_enabled:
                        try:
                            ohlcv = exchange.fetch_ohlcv(symbol, "1d", limit=self.atr_period + 5)
                            if ohlcv and len(ohlcv) >= self.atr_period:
                                trs = []
                                for i in range(1, len(ohlcv)):
                                    tr = max(
                                        ohlcv[i][2] - ohlcv[i][3],
                                        abs(ohlcv[i][2] - ohlcv[i - 1][4]),
                                        abs(ohlcv[i][3] - ohlcv[i - 1][4])
                                    )
                                    trs.append(tr)
                                atr = sum(trs[-self.atr_period:]) / self.atr_period
                        except Exception as e:
                            logger.warning(f"ATR 计算失败 {symbol}: {e}")

                    position = Position(
                        symbol=symbol,
                        side=side,
                        entry_price=entry_price,
                        quantity=quantity,
                        entry_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        synced=True,
                    )

                    # 固定 SL/TP
                    if side == "long":
                        position.stop_loss = entry_price * (1 - self.stop_loss)
                        position.take_profit = entry_price * (1 + self.take_profit)
                        position.highest_price = entry_price
                    else:
                        position.stop_loss = entry_price * (1 + self.stop_loss)
                        position.take_profit = entry_price * (1 - self.take_profit)
                        position.lowest_price = entry_price

                    # ATR 动态 SL/TP
                    if self.atr_stop_loss_enabled and atr > 0:
                        if side == "long":
                            position.atr_stop_loss = entry_price - atr * self.atr_multiplier
                            position.atr_take_profit = entry_price + atr * self.atr_tp_multiplier
                            position.trailing_stop = 0
                        else:
                            position.atr_stop_loss = entry_price + atr * self.atr_multiplier
                            position.atr_take_profit = entry_price - atr * self.atr_tp_multiplier
                            position.trailing_stop = 0

                    if symbol not in self.positions:
                        self.positions[symbol] = {}
                    self.positions[symbol][side] = position
                    logger.info(
                        f"📡 同步持仓: {side.upper()} {symbol} | "
                        f"价格={entry_price:.4f} | 数量={quantity:.6f} | "
                        f"ATR={atr:.4f} | SL={position.stop_loss:.4f} | TP={position.take_profit:.4f}"
                    )
            except Exception as e:
                logger.warning(f"同步持仓失败 {symbol}: {e}")

    def get_position(self, symbol: str) -> Optional[Dict[str, Position]]:
        """获取指定交易对的所有持仓（对冲模式返回 dict {side: Position}）"""
        return self.positions.get(symbol)

    def get_all_positions(self) -> Dict[str, Dict[str, Position]]:
        """获取所有持仓"""
        return self.positions.copy()

    def get_total_position_count(self) -> int:
        """获取总持仓数（每个方向算一个）"""
        return sum(len(sides) for sides in self.positions.values())
    
    def get_stats(self) -> Dict[str, Any]:
        """获取风控统计"""
        win_rate = (
            self.stats.winning_trades / self.stats.total_trades * 100
            if self.stats.total_trades > 0
            else 0
        )
        
        return {
            'total_trades': self.stats.total_trades,
            'winning_trades': self.stats.winning_trades,
            'losing_trades': self.stats.losing_trades,
            'win_rate': f"{win_rate:.1f}%",
            'total_pnl': self.stats.total_pnl,
            'daily_loss': self.stats.daily_loss,
            'daily_trades': self.stats.daily_trades,
            'open_positions': self.get_total_position_count()
        }
