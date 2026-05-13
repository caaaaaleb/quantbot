"""交易执行模块"""

import math
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
        self.min_notional = 5.1  # 最低名义价值(USD)，Bitget 合约最低 $5，留 $0.1 缓冲

        logger.info(f"交易执行器初始化 - 最大重试={max_retries}, 延迟={retry_delay}s")

    def set_dry_run(self, enabled: bool) -> None:
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
            if self.dry_run:
                return {
                    'currency': currency,
                    'free': 1000.0,
                    'used': 0.0,
                    'total': 1000.0
                }
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

    def _round_amount(self, symbol: str, amount: float) -> float:
        """
        按交易所精度要求取精订单数量
        """
        try:
            info = self.exchange.market(symbol)
            step_size = info.get('precision', {}).get('amount', 0)
            min_amount = info.get('limits', {}).get('amount', {}).get('min', 0) or step_size
            if step_size and step_size > 0:
                # 使用数学方法计算小数位数
                decimals = max(0, round(-math.log10(step_size))) if step_size < 1 else 0
                # 避免浮点数精度问题（如 0.1+0.2=0.30000000000000004）
                factor = 10 ** decimals
                amount = math.floor(amount * factor) / factor
            elif step_size == 0:
                amount = int(amount)
            # 确保不低于最小开仓量
            if min_amount and amount < min_amount:
                amount = min_amount
        except Exception:
            pass
        return amount

    def _order_params(
        self,
        position_side: str,
        reduce_only: bool,
        params: Optional[dict] = None
    ) -> dict:
        hold_side = position_side.lower()
        order_params = {
            'hedged': True,
            'holdSide': hold_side,
            'tradeSide': 'close' if reduce_only else 'open',
        }
        if reduce_only:
            order_params['reduceOnly'] = True
        if params:
            order_params.update(params)
        return order_params

    def market_buy(
        self,
        symbol: str,
        quantity: float,
        price: float = None,
        position_side: str = "LONG",
        reduce_only: bool = False,
        params: dict = None
    ) -> Dict[str, Any]:
        """
        市价买入

        Args:
            symbol: 交易对
            quantity: 买入数量
            price: 参考价格（日志用）
            position_side: 持仓方向 ('LONG' 或 'SHORT')，平仓时传入对应方向
            reduce_only: True=平仓, False=开仓（Bitget单向模式需要 tradeSide）
            params: 额外参数
        """
        if self.dry_run:
            price_str = f"{price:.8g}" if price else "N/A"
            logger.info(
                f"🧪 [模拟] 市价买入: {symbol} x {quantity:.6f} | positionSide={position_side} | "
                f"参考价={price_str}"
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

        # 名义价值检查
        if not reduce_only and price and quantity * price < self.min_notional:
            logger.warning(f"名义价值 ${quantity * price:.2f} 低于最低要求 ${self.min_notional}，跳过买入")
            return {'id': None, 'symbol': symbol, 'side': 'buy', 'amount': 0, 'price': price, 'status': 'failed'}

        logger.info(f"💰 市价买入: {symbol} x {quantity:.6f} | holdSide={position_side} | reduce_only={reduce_only}")

        try:
            amount = self._round_amount(symbol, quantity)
            order_params = self._order_params(position_side, reduce_only, params)

            if reduce_only:
                order = self._execute_with_retry(
                    self.exchange.create_market_buy_order,
                    symbol=symbol,
                    amount=amount,
                    params=order_params
                )
            else:
                # 开仓：ccxt 默认逻辑正确
                order = self._execute_with_retry(
                    self.exchange.create_market_buy_order,
                    symbol=symbol,
                    amount=amount,
                    params=order_params
                )

            avg_price = order.get('average', order.get('price', price))
            filled = order.get('filled', order.get('amount', quantity)) or quantity

            logger.info(
                f"✅ 买入成功 | 订单ID={order.get('id')} | "
                f"成交数量={filled:.6f} | "
                f"成交均价={avg_price}"
            )

            return {
                'id': order.get('id'),
                'symbol': symbol,
                'side': 'buy',
                'amount': filled,
                'price': avg_price,
                'status': order.get('status', 'closed'),
                'fee': (order.get('fee') or {}).get('cost', 0)
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
        price: float = None,
        position_side: str = "SHORT",
        reduce_only: bool = False,
        params: dict = None
    ) -> Dict[str, Any]:
        """
        市价卖出

        Args:
            symbol: 交易对
            quantity: 卖出数量
            price: 参考价格（日志用）
            position_side: 持仓方向 ('LONG' 或 'SHORT')，平仓时传入对应方向
            reduce_only: True=平仓, False=开仓（Bitget单向模式需要 tradeSide）
            params: 额外参数
        """
        if self.dry_run:
            price_str = f"{price:.8g}" if price else "N/A"
            logger.info(
                f"🧪 [模拟] 市价卖出: {symbol} x {quantity:.6f} | positionSide={position_side} | "
                f"参考价={price_str}"
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

        # 名义价值检查
        if not reduce_only and price and quantity * price < self.min_notional:
            logger.warning(f"名义价值 ${quantity * price:.2f} 低于最低要求 ${self.min_notional}，跳过卖出")
            return {'id': None, 'symbol': symbol, 'side': 'sell', 'amount': 0, 'price': price, 'status': 'failed'}

        logger.info(f"💸 市价卖出: {symbol} x {quantity:.6f} | holdSide={position_side} | reduce_only={reduce_only}")

        try:
            amount = self._round_amount(symbol, quantity)
            order_params = self._order_params(position_side, reduce_only, params)

            if reduce_only:
                order = self._execute_with_retry(
                    self.exchange.create_market_sell_order,
                    symbol=symbol,
                    amount=amount,
                    params=order_params
                )
            else:
                # 开仓：ccxt 默认逻辑正确
                order = self._execute_with_retry(
                    self.exchange.create_market_sell_order,
                    symbol=symbol,
                    amount=amount,
                    params=order_params
                )

            avg_price = order.get('average', order.get('price', price))
            filled = order.get('filled', order.get('amount', quantity)) or quantity

            logger.info(
                f"✅ 卖出成功 | 订单ID={order.get('id')} | "
                f"成交数量={filled:.6f} | "
                f"成交均价={avg_price}"
            )

            return {
                'id': order.get('id'),
                'symbol': symbol,
                'side': 'sell',
                'amount': filled,
                'price': avg_price,
                'status': order.get('status', 'closed'),
                'fee': (order.get('fee') or {}).get('cost', 0)
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
