"""账户管理模块 - 统一账户视图 + 资金划转"""

import ccxt
from typing import Dict, Any, List, Optional
from datetime import datetime

from src.utils.logger import logger


class AccountManager:
    """
    统一账户管理器

    支持:
    - 现货账户余额查询
    - 合约账户余额查询
    - U本位合约 / 币本位合约
    - 资金划转（现货 ↔ 合约 ↔ 理财）
    """

    def __init__(self, exchange: ccxt.Exchange, dry_run: bool = False):
        self.exchange = exchange
        self.dry_run = dry_run
        logger.info(f"AccountManager 初始化成功 (dry_run={dry_run})")

    # ═══════════════════════════════════════════════════════════════
    # 现货账户
    # ═══════════════════════════════════════════════════════════════

    def get_spot_balance(self) -> Dict[str, Any]:
        """
        获取现货账户余额

        Returns:
            dict: 现货账户余额详情
        """
        if self.dry_run:
            return {
                "account_type": "spot",
                "total_usdt": 1000.0,
                "assets": [{"asset": "USDT", "free": 1000.0, "locked": 0, "total": 1000.0, "usdt_value": 1000.0}],
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "dry_run": True,
            }
        try:
            balance = self.exchange.fetch_balance({"type": "spot"})
            assets = []

            for currency, data in balance.get("free", {}).items():
                total = balance.get("total", {}).get(currency, 0)
                used = balance.get("used", {}).get(currency, 0)
                free = balance.get("free", {}).get(currency, 0)

                # 只返回有余额的资产
                if total and total > 0.00000001:
                    # 获取该资产对 USDT 的价格（如果有）
                    try:
                        if currency == "USDT":
                            price = 1.0
                        else:
                            ticker = self.exchange.fetch_ticker(f"{currency}/USDT")
                            price = ticker.get("last", 0) or 0
                    except:
                        price = 0

                    assets.append({
                        "asset": currency,
                        "free": free,
                        "locked": used,
                        "total": total,
                        "usdt_value": total * price,
                    })

            # 按 USDT 价值排序
            assets.sort(key=lambda x: x["usdt_value"], reverse=True)

            total_usdt = sum(a["usdt_value"] for a in assets)

            return {
                "account_type": "spot",
                "total_usdt": total_usdt,
                "assets": assets,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        except Exception as e:
            logger.error(f"获取现货余额失败: {e}")
            return {"error": str(e), "account_type": "spot"}

    # ═══════════════════════════════════════════════════════════════
    # USDT-M 合约账户
    # ═══════════════════════════════════════════════════════════════

    def get_usdt_futures_balance(self) -> Dict[str, Any]:
        """
        获取 USDT-M 合约账户余额

        Returns:
            dict: USDT-M 合约余额
        """
        if self.dry_run:
            return {
                "account_type": "usdt_futures",
                "total_usdt": 0.0,
                "assets": [],
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "dry_run": True,
            }
        try:
            # USDT-M 合约
            balance = self.exchange.fetch_balance({"type": "swap", "settle": "usdt"})
            assets = []

            for currency, data in balance.get("free", {}).items():
                total = balance.get("total", {}).get(currency, 0)
                used = balance.get("used", {}).get(currency, 0)
                free = balance.get("free", {}).get(currency, 0)

                if total and total > 0.00000001:
                    assets.append({
                        "asset": currency,
                        "free": free,
                        "locked": used,
                        "total": total,
                    })

            total_usdt = sum(a["total"] for a in assets if a["asset"] == "USDT")
            total_usdt = total_usdt or 0

            return {
                "account_type": "usdt_futures",
                "total_usdt": total_usdt,
                "assets": assets,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        except Exception as e:
            logger.warning(f"获取 USDT-M 合约余额失败（用户未开通 USDT-M 合约或无权限）")
            return {"error": "usdt_futures_not_available", "account_type": "usdt_futures"}

    # ═══════════════════════════════════════════════════════════════
    # 币本位合约账户
    # ═══════════════════════════════════════════════════════════════

    def get_coin_futures_balance(self) -> Dict[str, Any]:
        """
        获取币本位合约（反向合约）账户余额

        Returns:
            dict: 币本位合约余额
        """
        try:
            balance = self.exchange.fetch_balance({"type": "delivery", "settle": "btc"})
            assets = []

            for currency, data in balance.get("free", {}).items():
                total = balance.get("total", {}).get(currency, 0)
                used = balance.get("used", {}).get(currency, 0)
                free = balance.get("free", {}).get(currency, 0)

                if total and total > 0.00000001:
                    assets.append({
                        "asset": currency,
                        "free": free,
                        "locked": used,
                        "total": total,
                    })

            return {
                "account_type": "coin_futures",
                "assets": assets,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        except Exception as e:
            logger.warning(f"获取币本位合约余额失败（用户未开通币本位合约或无权限）")
            return {"error": "coin_futures_not_available", "account_type": "coin_futures"}

    # ═══════════════════════════════════════════════════════════════
    # 统一账户概览
    # ═══════════════════════════════════════════════════════════════

    def get_account_summary(self) -> Dict[str, Any]:
        """
        获取统一账户概览

        Returns:
            dict: 所有账户的汇总信息
        """
        spot = self.get_spot_balance()
        futures = self.get_usdt_futures_balance()

        # 计算总资产（USDT）
        spot_total = spot.get("total_usdt", 0) if "error" not in spot else 0
        futures_total = futures.get("total_usdt", 0) if "error" not in futures else 0

        return {
            "spot": spot,
            "usdt_futures": futures,
            "summary": {
                "spot_usdt": spot_total,
                "futures_usdt": futures_total,
                "total_usdt": spot_total + futures_total,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
        }

    # ═══════════════════════════════════════════════════════════════
    # 资金划转
    # ═══════════════════════════════════════════════════════════════

    def transfer(
        self,
        asset: str,
        amount: float,
        from_account: str,
        to_account: str
    ) -> Dict[str, Any]:
        """
        账户间资金划转

        Args:
            asset: 资产名称，如 "USDT", "BTC"
            amount: 划转数量
            from_account: 来源账户
                - "spot" (现货)
                - "usdt futures" / "swap" (USDT-M合约)
                - "coin futures" / "delivery" (币本位合约)
                - "funding" (理财/矿池)
            to_account: 目标账户（同上）

        Returns:
            dict: 划转结果
        """
        # 映射账户类型到 Bitget API 的 type 参数（ccxt 格式）
        account_type_map = {
            "spot": "spot",
            "usdt_futures": "swap",
            "swap": "swap",
            "futures": "swap",
            "coin_futures": "coin_futures",
            "delivery": "coin_futures",
            "funding": "funding",
            "mining": "mining",
        }

        from_type = account_type_map.get(from_account, from_account)
        to_type = account_type_map.get(to_account, to_account)

        try:
            logger.info(
                f"划转: {amount} {asset} | {from_type} → {to_type}"
            )

            result = self.exchange.transfer(
                code=asset,
                amount=amount,
                fromAccount=from_type,
                toAccount=to_type,
            )

            logger.info(f"划转成功: {result}")
            return {
                "success": True,
                "asset": asset,
                "amount": amount,
                "from": from_type,
                "to": to_type,
                "txn_id": result.get("id", ""),
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }

        except ccxt.InsufficientFunds as e:
            logger.error(f"划转失败: 余额不足 - {e}")
            return {
                "success": False,
                "error": "余额不足",
                "detail": str(e),
            }
        except ccxt.AccountNotEnabled as e:
            logger.error(f"划转失败: 账户未开通 - {e}")
            return {
                "success": False,
                "error": "账户未开通对应服务",
                "detail": str(e),
            }
        except ccxt.ExchangeError as e:
            logger.error(f"划转失败: 交易所错误 - {e}")
            return {
                "success": False,
                "error": "交易所错误",
                "detail": str(e),
            }
        except Exception as e:
            logger.error(f"划转失败: {e}")
            return {
                "success": False,
                "error": str(e),
            }

    def spot_to_futures(self, asset: str, amount: float) -> Dict[str, Any]:
        """
        现货 → 合约 划转

        Args:
            asset: 资产名称
            amount: 数量

        Returns:
            dict: 划转结果
        """
        return self.transfer(asset, amount, "spot", "usdt_futures")

    def futures_to_spot(self, asset: str, amount: float) -> Dict[str, Any]:
        """
        合约 → 现货 划转

        Args:
            asset: 资产名称
            amount: 数量

        Returns:
            dict: 划转结果
        """
        return self.transfer(asset, amount, "usdt_futures", "spot")

    # ═══════════════════════════════════════════════════════════════
    # 快捷操作
    # ═══════════════════════════════════════════════════════════════

    def transfer_all_spot_to_futures(self, asset: str = "USDT") -> Dict[str, Any]:
        """
        把现货账户中指定资产全部转到合约

        Args:
            asset: 资产名称，默认 USDT

        Returns:
            dict: 划转结果
        """
        spot = self.get_spot_balance()
        if "error" in spot:
            return {"success": False, "error": spot["error"]}

        # 找到该资产的余额
        target_asset = None
        for a in spot.get("assets", []):
            if a["asset"] == asset:
                target_asset = a
                break

        if not target_asset:
            return {
                "success": False,
                "error": f"现货账户中没有 {asset} 余额",
            }

        free_amount = target_asset["free"]
        if free_amount <= 0:
            return {
                "success": False,
                "error": f"{asset} 可用余额为 0",
            }

        return self.spot_to_futures(asset, free_amount)

    def get_deposit_address(self, coin: str) -> Dict[str, Any]:
        """
        获取充值地址

        Args:
            coin: 币种名称

        Returns:
            dict: 充值地址信息
        """
        try:
            address = self.exchange.fetch_deposit_address(coin)
            return {
                "coin": coin,
                "address": address.get("address", ""),
                "tag": address.get("tag", ""),
                "network": address.get("network", ""),
            }
        except Exception as e:
            logger.error(f"获取 {coin} 充值地址失败: {e}")
            return {"error": str(e)}

    def get_withdraw_history(self, limit: int = 10) -> List[Dict[str, Any]]:
        """
        获取提现历史

        Args:
            limit: 返回数量

        Returns:
            list: 提现历史
        """
        try:
            withdrawals = self.exchange.fetch_withdrawals(limit=limit)
            result = []
            for w in withdrawals:
                result.append({
                    "id": w.get("id", ""),
                    "txid": w.get("txid", ""),
                    "coin": w.get("currency", ""),
                    "amount": w.get("amount", 0),
                    "address": w.get("address", ""),
                    "status": w.get("status", ""),
                    "timestamp": w.get("timestamp", 0),
                    "datetime": w.get("datetime", ""),
                })
            return result
        except Exception as e:
            logger.error(f"获取提现历史失败: {e}")
            return []

    def get_deposit_history(self, limit: int = 10) -> List[Dict[str, Any]]:
        """
        获取充值历史

        Args:
            limit: 返回数量

        Returns:
            list: 充值历史
        """
        try:
            deposits = self.exchange.fetch_deposits(limit=limit)
            result = []
            for d in deposits:
                result.append({
                    "id": d.get("id", ""),
                    "txid": d.get("txid", ""),
                    "coin": d.get("currency", ""),
                    "amount": d.get("amount", 0),
                    "address": d.get("address", ""),
                    "status": d.get("status", ""),
                    "timestamp": d.get("timestamp", 0),
                    "datetime": d.get("datetime", ""),
                })
            return result
        except Exception as e:
            logger.error(f"获取充值历史失败: {e}")
            return []

    # ═══════════════════════════════════════════════════════════════
    # 交易记录 & 持仓
    # ═══════════════════════════════════════════════════════════════

    def get_my_trades(self, symbol: str = None, limit: int = 50) -> List[Dict[str, Any]]:
        """
        获取我的成交历史

        Args:
            symbol: 交易对，如 "BTC/USDT"，None 则返回全部
            limit: 返回数量

        Returns:
            list: 成交记录
        """
        try:
            if symbol:
                trades = self.exchange.fetch_my_trades(symbol=symbol, limit=limit)
            else:
                # 从 main 模块获取当前交易对列表（避免硬编码）
                try:
                    import main as _main
                    symbols = getattr(_main, 'SYMBOLS', None) or []
                except ImportError:
                    symbols = []
                if not symbols:
                    # 兜底：尝试用配置文件中定义的交易对
                    try:
                        import yaml
                        with open("config/config.yaml", encoding="utf-8") as f:
                            cfg = yaml.safe_load(f)
                        symbols = cfg.get("trading", {}).get("symbols", [])
                    except Exception:
                        symbols = []
                trades = []
                for sym in symbols:
                    try:
                        trades.extend(self.exchange.fetch_my_trades(symbol=sym, limit=limit))
                    except Exception:
                        pass
            result = []
            for t in trades:
                result.append({
                    "id": t.get("id", ""),
                    "symbol": t.get("symbol", ""),
                    "side": t.get("side", "").upper(),
                    "quantity": t.get("amount", 0),
                    "price": t.get("price", 0),
                    "cost": t.get("cost", 0),
                    "fee": t.get("fee", {}),
                    "time": t.get("datetime", ""),
                    "timestamp": t.get("timestamp", 0),
                    "order_id": t.get("order", ""),
                })
            # 按时间倒序
            result.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
            return result[:limit]
        except Exception as e:
            logger.error(f"获取成交记录失败: {e}")
            return []

    def get_futures_positions(self) -> List[Dict[str, Any]]:
        """
        获取合约持仓

        Returns:
            list: 持仓列表
        """
        if self.dry_run:
            return []
        try:
            positions = self.exchange.fetch_positions()
            result = []
            for p in positions:
                size = p.get("contracts", 0) or p.get("size", 0)
                if size and size != 0:
                    entry_price = p.get("entryPrice", 0) or 0
                    mark_price = p.get("markPrice", 0) or 0
                    unrealized_pnl = p.get("unrealizedPnl", 0) or 0
                    notional = entry_price * size if entry_price else 0
                    pnl_pct = (unrealized_pnl / notional) if notional else 0
                    result.append({
                        "symbol": p.get("symbol", ""),
                        "side": p.get("side", "").upper(),
                        "size": size,
                        "entry_price": entry_price,
                        "mark_price": mark_price,
                        "unrealized_pnl": unrealized_pnl,
                        "pnl_pct": round(pnl_pct, 4),
                        "leverage": p.get("leverage", 1),
                        "margin": p.get("margin", 0),
                        "liquidation_price": p.get("liquidationPrice", 0),
                    })
            return result
        except Exception as e:
            logger.error(f"获取合约持仓失败: {e}")
            return []

    def get_account_stats(self) -> Dict[str, Any]:
        """
        获取账户统计数据（供 Dashboard 使用）

        Returns:
            dict: 统计信息
        """
        try:
            positions = self.get_futures_positions()

            # 从当前持仓获取未实现盈亏
            total_unrealized_pnl = sum(p.get("unrealized_pnl", 0) for p in positions)

            # 尝试从成交历史估算已实现盈亏（仅统计 SELL 交易的名义价值变化）
            # 注意：此估算无法区分开仓/平仓，真实 PnL 需参考交易所持仓报告
            total_pnl = total_unrealized_pnl  # 当前以未实现盈亏为主

            # 统计成交笔数
            trades = self.get_my_trades(limit=200)
            total_trades = len(trades)
            # 注意：已实现盈亏需参考交易所持仓报告，从成交历史无法准确估算
            # 此处仅返回未实现盈亏和成交笔数
            realized_pnl = 0.0
            winning_trades = 0
            losing_trades = 0

            win_rate = winning_trades / total_trades if total_trades > 0 else 0

            return {
                "total_trades": total_trades,
                "win_rate": win_rate,
                "total_pnl": total_pnl + realized_pnl,
                "winning_trades": winning_trades,
                "losing_trades": losing_trades,
                "positions": positions,
            }
        except Exception as e:
            logger.error(f"获取账户统计失败: {e}")
            return {
                "total_trades": 0,
                "win_rate": 0,
                "total_pnl": 0,
                "winning_trades": 0,
                "losing_trades": 0,
                "positions": [],
            }
