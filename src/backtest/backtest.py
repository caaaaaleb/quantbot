"""
回测模块
支持历史数据回测、多因子策略回测、绩效分析
"""

import pandas as pd
import numpy as np
from datetime import datetime
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field

from src.utils.logger import logger
from src.strategy.multi_factor import MultiFactorStrategy, Signal


@dataclass
class Trade:
    """单笔交易记录"""
    timestamp: str
    symbol: str
    side: str           # long / short
    entry_price: float
    exit_price: float
    quantity: float
    pnl: float
    pnl_pct: float
    holding_period: int  # 持仓K线数量


@dataclass
class BacktestResult:
    """回测结果"""
    initial_capital: float
    final_capital: float
    total_return: float
    total_return_pct: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    max_drawdown: float
    sharpe_ratio: float
    
    trades: List[Trade] = field(default_factory=list)
    equity_curve: List[Dict] = field(default_factory=list)
    daily_returns: List[float] = field(default_factory=list)


class Backtester:
    """回测引擎"""
    
    def __init__(
        self,
        initial_capital: float = 10000,
        commission: float = 0.001,
        slippage: float = 0.0005,
        stop_loss_pct: float = 0.02,
        take_profit_pct: float = 0.05
    ):
        """
        初始化回测引擎
        
        Args:
            initial_capital: 初始资金
            commission: 手续费率
            slippage: 滑点率
        """
        self.initial_capital = initial_capital
        self.commission = commission
        self.slippage = slippage
        
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct

        self.capital = initial_capital
        self.position: Optional[Dict] = None
        self.trades: List[Trade] = []
        self.equity_curve: List[Dict] = []
        
        logger.info(
            f"回测引擎初始化 | 初始资金={initial_capital} USDT | "
            f"手续费={commission*100}% | 滑点={slippage*100}%"
        )
    
    def reset(self):
        """重置回测状态"""
        self.capital = self.initial_capital
        self.position = None
        self.trades = []
        self.equity_curve = []
    
    def _apply_slippage(self, price: float, side: str) -> float:
        """应用滑点"""
        if side == "buy":
            return price * (1 + self.slippage)
        else:
            return price * (1 - self.slippage)
    
    def _apply_commission(self, amount: float) -> float:
        """计算手续费"""
        return amount * self.commission
    
    def run(
        self,
        df: pd.DataFrame,
        strategy: MultiFactorStrategy,
        symbol: str = "BTC/USDT"
    ) -> BacktestResult:
        """
        运行回测
        
        Args:
            df: K线数据 (需包含 timestamp, open, high, low, close, volume)
            strategy: 策略实例
            symbol: 交易对
            
        Returns:
            BacktestResult: 回测结果
        """
        self.reset()
        
        logger.info(f"开始回测 | 数据行数={len(df)} | 交易对={symbol}")
        
        # 逐K线回测
        for i in range(20, len(df)):  # 需要足够数据计算MA
            # 截取到当前时刻的数据
            current_df = df.iloc[:i+1].copy()
            current_price = current_df['close'].iloc[-1]
            current_time = current_df['datetime'].iloc[-1]
            
            # 生成信号
            signal_result = strategy.generate_signal(current_df)
            signal = signal_result.signal.value
            
            # 记录权益
            equity = self.capital
            if self.position:
                if self.position['side'] == 'long':
                    unrealized = (current_price - self.position['entry_price']) * self.position['quantity']
                else:
                    unrealized = (self.position['entry_price'] - current_price) * self.position['quantity']
                equity += unrealized
            
            self.equity_curve.append({
                'timestamp': current_time,
                'equity': equity,
                'price': current_price
            })
            
            # 开仓逻辑
            if self.position is None and signal in ['BUY', 'SELL']:
                # 仓位计算（固定20%）
                position_size = self.capital * 0.2 / current_price
                
                # 执行价格（含滑点）
                exec_price = self._apply_slippage(
                    current_price, 
                    'buy' if signal == 'BUY' else 'sell'
                )
                
                # 扣除手续费
                trade_cost = self._apply_commission(position_size * exec_price)
                self.capital -= trade_cost
                
                self.position = {
                    'symbol': symbol,
                    'side': 'long' if signal == 'BUY' else 'short',
                    'entry_price': exec_price,
                    'quantity': position_size,
                    'entry_time': current_time,
                    'entry_index': i
                }
                
                logger.debug(f"开仓: {self.position['side']} {symbol} @ {exec_price:.2f}")
            
            # 平仓逻辑
            elif self.position:
                should_close = False
                close_reason = ""
                
                # 1. 止盈/止损
                pnl_pct = 0.0
                if self.position['side'] == 'long':
                    pnl_pct = (current_price - self.position['entry_price']) / self.position['entry_price']
                else:
                    pnl_pct = (self.position['entry_price'] - current_price) / self.position['entry_price']
                
                if pnl_pct >= self.take_profit_pct:  # 止盈
                    should_close = True
                    close_reason = "TAKE_PROFIT"
                elif pnl_pct <= -self.stop_loss_pct:  # 止损
                    should_close = True
                    close_reason = "STOP_LOSS"
                
                # 2. 反向信号
                elif (self.position['side'] == 'long' and signal == 'SELL') or \
                     (self.position['side'] == 'short' and signal == 'BUY'):
                    should_close = True
                    close_reason = "REVERSE_SIGNAL"
                
                if should_close:
                    exec_price = self._apply_slippage(
                        current_price,
                        'sell' if self.position['side'] == 'long' else 'buy'
                    )
                    
                    # 计算盈亏
                    if self.position['side'] == 'long':
                        pnl = (exec_price - self.position['entry_price']) * self.position['quantity']
                    else:
                        pnl = (self.position['entry_price'] - exec_price) * self.position['quantity']
                    
                    # 扣除手续费
                    trade_cost = self._apply_commission(self.position['quantity'] * exec_price)
                    pnl -= trade_cost
                    
                    self.capital += self.position['quantity'] * exec_price - trade_cost
                    
                    # 记录交易
                    trade = Trade(
                        timestamp=self.position['entry_time'],
                        symbol=symbol,
                        side=self.position['side'],
                        entry_price=self.position['entry_price'],
                        exit_price=exec_price,
                        quantity=self.position['quantity'],
                        pnl=pnl,
                        pnl_pct=pnl_pct * 100,
                        holding_period=i - self.position['entry_index']
                    )
                    self.trades.append(trade)
                    
                    logger.debug(
                        f"平仓: {close_reason} | PnL={pnl:+.2f} ({pnl_pct*100:+.2f}%)"
                    )
                    
                    self.position = None
        
        # 计算最终结果
        final_capital = self.capital
        if self.position:
            # 按最后价格平掉剩余仓位
            final_price = df['close'].iloc[-1]
            final_pnl = (final_price - self.position['entry_price']) * self.position['quantity']
            final_capital += final_pnl
        
        return self._calculate_metrics(final_capital)
    
    def _calculate_metrics(self, final_capital: float) -> BacktestResult:
        """计算绩效指标"""
        total_return = final_capital - self.initial_capital
        total_return_pct = total_return / self.initial_capital * 100
        
        winning_trades = [t for t in self.trades if t.pnl > 0]
        losing_trades = [t for t in self.trades if t.pnl <= 0]
        
        win_rate = len(winning_trades) / len(self.trades) * 100 if self.trades else 0
        
        # 计算最大回撤
        equity_values = [e['equity'] for e in self.equity_curve]
        max_equity = np.maximum.accumulate(equity_values)
        drawdowns = (max_equity - equity_values) / max_equity
        max_drawdown = np.max(drawdowns) * 100 if drawdowns.any() else 0
        
        # 计算夏普比率（简化版）
        if len(self.equity_curve) > 1:
            returns = np.diff(equity_values) / equity_values[:-1]
            sharpe_ratio = returns.mean() / returns.std() * np.sqrt(252) if returns.std() > 0 else 0
        else:
            sharpe_ratio = 0
        
        result = BacktestResult(
            initial_capital=self.initial_capital,
            final_capital=final_capital,
            total_return=total_return,
            total_return_pct=total_return_pct,
            total_trades=len(self.trades),
            winning_trades=len(winning_trades),
            losing_trades=len(losing_trades),
            win_rate=win_rate,
            max_drawdown=max_drawdown,
            sharpe_ratio=sharpe_ratio,
            trades=self.trades,
            equity_curve=self.equity_curve
        )
        
        logger.info(
            f"回测完成 | 收益率={total_return_pct:+.2f}% | "
            f"交易次数={len(self.trades)} | 胜率={win_rate:.1f}% | "
            f"最大回撤={max_drawdown:.2f}%"
        )
        
        return result
    
    def generate_report(self, result: BacktestResult) -> Dict[str, Any]:
        """生成回测报告"""
        return {
            'summary': {
                'initial_capital': result.initial_capital,
                'final_capital': result.final_capital,
                'total_return': f"{result.total_return:+.2f} USDT",
                'total_return_pct': f"{result.total_return_pct:+.2f}%",
            },
            'trading_stats': {
                'total_trades': result.total_trades,
                'winning_trades': result.winning_trades,
                'losing_trades': result.losing_trades,
                'win_rate': f"{result.win_rate:.1f}%",
            },
            'risk_metrics': {
                'max_drawdown': f"{result.max_drawdown:.2f}%",
                'sharpe_ratio': f"{result.sharpe_ratio:.2f}",
            },
            'recent_trades': [
                {
                    'time': t.timestamp,
                    'side': t.side.upper(),
                    'entry': f"{t.entry_price:.2f}",
                    'exit': f"{t.exit_price:.2f}",
                    'pnl': f"{t.pnl:+.2f}",
                    'pnl_pct': f"{t.pnl_pct:+.2f}%"
                }
                for t in result.trades[-10:]
            ]
        }