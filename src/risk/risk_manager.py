"""风控模块"""

from typing import Dict, Any, Optional
from dataclasses import dataclass, field
from datetime import datetime

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
    
    def unrealized_pnl(self, current_price: float) -> float:
        """计算未实现盈亏"""
        if self.side == "long":
            return (current_price - self.entry_price) * self.quantity
        else:
            return (self.entry_price - current_price) * self.quantity
    
    def pnl_pct(self, current_price: float) -> float:
        """计算盈亏百分比"""
        if self.entry_price == 0:
            return 0.0
        return self.unrealized_pnl(current_price) / (self.entry_price * self.quantity)


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
        max_positions: int = 3
    ):
        """
        初始化风控
        
        Args:
            max_position_size: 单笔最大仓位比例 (默认20%)
            stop_loss: 止损比例 (默认2%)
            take_profit: 止盈比例 (默认5%)
            max_daily_loss: 单日最大亏损比例 (默认10%)
            max_trades_per_day: 每日最大交易次数
            max_positions: 最大持仓交易对数量
        """
        self.max_position_size = max_position_size
        self.stop_loss = stop_loss
        self.take_profit = take_profit
        self.max_daily_loss = max_daily_loss
        self.max_trades_per_day = max_trades_per_day
        self.max_positions = max_positions
        
        # 持仓管理
        self.positions: Dict[str, Position] = {}
        self.stats = RiskStats()
        
        logger.info(
            f"风控初始化 - 仓位上限={max_position_size*100}%, "
            f"止损={stop_loss*100}%, 止盈={take_profit*100}%, "
            f"日亏损上限={max_daily_loss*100}%, 日交易上限={max_trades_per_day}"
        )
    
    def _check_daily_reset(self):
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
        signal: str
    ) -> Dict[str, Any]:
        """
        检查是否允许交易
        
        Args:
            symbol: 交易对
            balance: 可用余额
            price: 当前价格
            signal: 交易信号 (BUY/SELL/HOLD)
            
        Returns:
            dict: {allowed: bool, reason: str, size: float}
        """
        self._check_daily_reset()
        
        # 信号检查
        if signal == "HOLD":
            return {'allowed': False, 'reason': '信号为HOLD，不交易', 'size': 0}
        
        # 每日交易次数检查
        if self.stats.daily_trades >= self.max_trades_per_day:
            logger.warning(f"每日交易次数已达上限: {self.stats.daily_trades}")
            return {'allowed': False, 'reason': f'日交易次数超限 ({self.stats.daily_trades}/{self.max_trades_per_day})', 'size': 0}
        
        # 每日亏损检查
        if self.stats.daily_loss <= -self.max_daily_loss * balance:
            logger.warning(f"每日亏损已达上限: {self.stats.daily_loss}")
            return {'allowed': False, 'reason': f'日亏损超限 ({self.stats.daily_loss:.2f})', 'size': 0}
        
        # 最大持仓数量检查
        if symbol not in self.positions and len(self.positions) >= self.max_positions:
            logger.warning(f"已达最大持仓数量: {len(self.positions)}/{self.max_positions}")
            return {'allowed': False, 'reason': f'已达最大持仓数量 ({len(self.positions)}/{self.max_positions})', 'size': 0}
        
        # 计算仓位大小
        max_amount = balance * self.max_position_size
        quantity = max_amount / price
        
        # 持仓方向检查
        existing = self.positions.get(symbol)
        if existing:
            if signal == "BUY" and existing.side == "long":
                return {'allowed': False, 'reason': '已有多头持仓，不再加仓', 'size': 0}
            if signal == "SELL" and existing.side == "short":
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
        current_price: float
    ) -> Optional[str]:
        """
        检查止损止盈
        
        Args:
            symbol: 交易对
            current_price: 当前价格
            
        Returns:
            Optional[str]: 'STOP_LOSS' / 'TAKE_PROFIT' / None
        """
        position = self.positions.get(symbol)
        if not position:
            return None
        
        pnl_pct = position.pnl_pct(current_price)
        
        # 止损检查
        if pnl_pct <= -self.stop_loss:
            logger.warning(
                f"🚨 止损触发! {symbol} | "
                f"入场={position.entry_price:.2f} | "
                f"当前={current_price:.2f} | "
                f"亏损={pnl_pct*100:.2f}%"
            )
            return "STOP_LOSS"
        
        # 止盈检查
        if pnl_pct >= self.take_profit:
            logger.info(
                f"🎯 止盈触发! {symbol} | "
                f"入场={position.entry_price:.2f} | "
                f"当前={current_price:.2f} | "
                f"盈利={pnl_pct*100:.2f}%"
            )
            return "TAKE_PROFIT"
        
        return None
    
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
        
        # 设置止损止盈
        if side == "long":
            position.stop_loss = entry_price * (1 - self.stop_loss)
            position.take_profit = entry_price * (1 + self.take_profit)
        else:
            position.stop_loss = entry_price * (1 + self.stop_loss)
            position.take_profit = entry_price * (1 - self.take_profit)
        
        self.positions[symbol] = position
        self.stats.total_trades += 1
        self.stats.daily_trades += 1
        
        logger.info(
            f"📈 开仓成功: {side.upper()} {symbol} | "
            f"价格={entry_price:.2f} | "
            f"数量={quantity:.6f} | "
            f"止损={position.stop_loss:.2f} | "
            f"止盈={position.take_profit:.2f}"
        )
        
        return position
    
    def close_position(
        self,
        symbol: str,
        close_price: float
    ) -> Optional[Dict[str, Any]]:
        """
        平仓
        
        Args:
            symbol: 交易对
            close_price: 平仓价格
            
        Returns:
            Optional[dict]: 平仓信息
        """
        position = self.positions.pop(symbol, None)
        if not position:
            logger.warning(f"无持仓可平: {symbol}")
            return None
        
        pnl = position.unrealized_pnl(close_price)
        pnl_pct = position.pnl_pct(close_price)
        
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
    
    def get_position(self, symbol: str) -> Optional[Position]:
        """获取持仓"""
        return self.positions.get(symbol)
    
    def get_all_positions(self) -> Dict[str, Position]:
        """获取所有持仓"""
        return self.positions.copy()
    
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
            'open_positions': len(self.positions)
        }