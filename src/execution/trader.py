"""交易执行模块"""

import time
from typing import Dict, Any, Optional

import ccxt

from src.utils.logger import logger


class Trader:
    """交易执行器"""
    
    def __init__(
        self,
        exchange: ccxt.Exchange,
        max_retries: int = 3,
        retry_delay: float = 1.0
    ):
        """
        初始化交易执行器
        
        Args:
            exchange: ccxt 交易所实例
            max_retries: 最大重试次数
            retry_delay: 重试延迟（秒）
        """
        self.exchange = exchange
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.dry_run = False  # 模拟模式标志
        
        logger.info(f"交易执行器初始化 - 最大重试={max_retries}, 延迟={retry_delay}s")
    
    def set_dry_run(self, enabled: bool):
        """设置模拟交易模式"""
        self.dry_run = enabled
        logger.info(f"模拟模式: {'开启' if enabled else '关闭'}")
    
    def get_balance(self, currency: str = "USDT") -> Dict[str, Any]:
        """
        获取账户余额
        
        Args:
            currency: 币种
            
        Returns:
            dict: 余额信息
        """
        try:
            balance = self.exchange.fetch_balance()
            free = balance.get('free', {}).get(currency, 0)
            total = balance.get('total', {}).get(currency, 0)
            used = balance.get('used', {}).get(currency, 0)
            
            return {
                'currency': currency,
                'free': free,
                'used': used,
                'total': total
            }
        except Exception as e:
            logger.error(f"获取余额失败: {e}")
            raise
    
    def _execute_with_retry(self, func, *args, **kwargs) -> Dict[str, Any]:
        """
        带重试机制的执行函数
        
        Args:
            func: 执行函数
            *args, **kwargs: 参数
            
        Returns:
            dict: 执行结果
            
        Raises:
            Exception: 所有重试都失败
        """
        last_error = None
        
        for attempt in range(1, self.max_retries + 1):
            try:
                result = func(*args, **kwargs)
                
                if attempt > 1:
                    logger.info(f"重试第 {attempt} 次成功!")
                
                return result
                
            except ccxt.InsufficientFunds as e:
                logger.error(f"余额不足: {e}")
                raise
                
            except ccxt.InvalidOrder as e:
                logger.error(f"无效订单: {e}")
                raise
                
            except ccxt.NetworkError as e:
                last_error = e
                logger.warning(
                    f"网络错误 (尝试 {attempt}/{self.max_retries}): {e}"
                )
                time.sleep(self.retry_delay)
                
            except ccxt.ExchangeError as e:
                last_error = e
                logger.warning(
                    f"交易所错误 (尝试 {attempt}/{self.max_retries}): {e}"
                )
                time.sleep(self.retry_delay)
                
            except Exception as e:
                last_error = e
                logger.error(f"未知错误 (尝试 {attempt}/{self.max_retries}): {e}")
                time.sleep(self.retry_delay)
        
        logger.error(f"所有 {self.max_retries} 次重试均失败!")
        raise Exception(f"重试耗尽 | 最后错误: {last_error}")
    
    def market_buy(
        self,
        symbol: str,
        quantity: float,
        price: float = None
    ) -> Dict[str, Any]:
        """
        市价买入
        
        Args:
            symbol: 交易对
            quantity: 买入数量
            price: 参考价格（日志用）
            
        Returns:
            dict: 订单结果
        """
        if self.dry_run:
            logger.info(
                f"🧪 [模拟] 市价买入: {symbol} x {quantity:.6f} | "
                f"参考价={price:.2f if price else 'N/A'}"
            )
            return {
                'id': 'dry_run_order',
                'symbol': symbol,
                'side': 'buy',
                'amount': quantity,
                'price': price,
                'status': 'closed',
                'dry_run': True
            }
        
        logger.info(f"💰 市价买入: {symbol} x {quantity:.6f}")
        
        try:
            order = self._execute_with_retry(
                self.exchange.create_market_buy_order,
                symbol=symbol,
                amount=quantity
            )
            
            avg_price = order.get('average', order.get('price', price))
            
            logger.info(
                f"✅ 买入成功 | 订单ID={order['id']} | "
                f"成交数量={order.get('filled', quantity):.6f} | "
                f"成交均价={avg_price:.2f}"
            )
            
            return {
                'id': order['id'],
                'symbol': symbol,
                'side': 'buy',
                'amount': order.get('filled', quantity),
                'price': avg_price,
                'status': order.get('status', 'closed'),
                'fee': order.get('fee', {}).get('cost', 0)
            }
            
        except Exception as e:
            logger.error(f"❌ 买入失败: {e}")
            return {
                'id': None,
                'symbol': symbol,
                'side': 'buy',
                'amount': quantity,
                'price': price,
                'status': 'failed',
                'error': str(e)
            }
    
    def market_sell(
        self,
        symbol: str,
        quantity: float,
        price: float = None
    ) -> Dict[str, Any]:
        """
        市价卖出
        
        Args:
            symbol: 交易对
            quantity: 卖出数量
            price: 参考价格（日志用）
            
        Returns:
            dict: 订单结果
        """
        if self.dry_run:
            logger.info(
                f"🧪 [模拟] 市价卖出: {symbol} x {quantity:.6f} | "
                f"参考价={price:.2f if price else 'N/A'}"
            )
            return {
                'id': 'dry_run_order',
                'symbol': symbol,
                'side': 'sell',
                'amount': quantity,
                'price': price,
                'status': 'closed',
                'dry_run': True
            }
        
        logger.info(f"💸 市价卖出: {symbol} x {quantity:.6f}")
        
        try:
            order = self._execute_with_retry(
                self.exchange.create_market_sell_order,
                symbol=symbol,
                amount=quantity
            )
            
            avg_price = order.get('average', order.get('price', price))
            
            logger.info(
                f"✅ 卖出成功 | 订单ID={order['id']} | "
                f"成交数量={order.get('filled', quantity):.6f} | "
                f"成交均价={avg_price:.2f}"
            )
            
            return {
                'id': order['id'],
                'symbol': symbol,
                'side': 'sell',
                'amount': order.get('filled', quantity),
                'price': avg_price,
                'status': order.get('status', 'closed'),
                'fee': order.get('fee', {}).get('cost', 0)
            }
            
        except Exception as e:
            logger.error(f"❌ 卖出失败: {e}")
            return {
                'id': None,
                'symbol': symbol,
                'side': 'sell',
                'amount': quantity,
                'price': price,
                'status': 'failed',
                'error': str(e)
            }
    
    def get_open_orders(self, symbol: str) -> list:
        """获取未完成订单"""
        try:
            orders = self.exchange.fetch_open_orders(symbol)
            logger.debug(f"未完成订单数: {len(orders)}")
            return orders
        except Exception as e:
            logger.error(f"获取订单失败: {e}")
            return []