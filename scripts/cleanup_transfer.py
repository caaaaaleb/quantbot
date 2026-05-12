"""清理现货持仓并归集资金到合约账户"""
import os
import ccxt
from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv("BITGET_API_KEY")
secret_key = os.getenv("BITGET_SECRET_KEY")
password = os.getenv("BITGET_PASSWORD")

exchange = ccxt.bitget({
    "apiKey": api_key,
    "secret": secret_key,
    "password": password,
    "enableRateLimit": True,
    "options": {"defaultType": "spot"},
})

# 1. 查看现货余额
balance = exchange.fetch_balance()
usdt_free = balance.get("free", {}).get("USDT", 0)
usdt_total = balance.get("total", {}).get("USDT", 0)
print(f"现货 USDT: free={usdt_free}, total={usdt_total}")

# 2. 查看 SOL 持仓
sol_free = balance.get("free", {}).get("SOL", 0)
sol_total = balance.get("total", {}).get("SOL", 0)
print(f"现货 SOL: free={sol_free}, total={sol_total}")

# 3. 如果有 SOL，卖出
if sol_total > 0:
    try:
        ticker = exchange.fetch_ticker("SOL/USDT")
        price = ticker["last"]
        print(f"SOL/USDT 当前价格: {price}")

        # minNotional = 5 USDT, 需要至少 5/价格 个 SOL
        import math
        min_notional = 5.0
        step = 0.001  # 精度
        need_amount = math.ceil(min_notional / price / step) * step
        sell_amount = max(sol_free, need_amount)
        sell_amount = float(f"{sell_amount:.3f}")  # 3位小数精度
        print(f"卖出 {sell_amount} SOL @ ~{price} (最少需要={need_amount:.3f})")

        order = exchange.create_market_sell_order("SOL/USDT", sell_amount)
        print(f"卖出成功: {order}")
    except Exception as e:
        print(f"卖出失败: {e}")
else:
    print("无 SOL 持仓，无需卖出")

# 4. 转 USDT 到合约
exchange_futures = ccxt.bitget({
    "apiKey": api_key,
    "secret": secret_key,
    "password": password,
    "enableRateLimit": True,
    "options": {"defaultType": "swap"},
})

# 重新获取余额（卖出后）
balance2 = exchange.fetch_balance()
usdt_to_transfer = balance2.get("free", {}).get("USDT", 0)
print(f"可转 USDT: {usdt_to_transfer}")

if usdt_to_transfer > 0.5:
    try:
        result = exchange.transfer(
            code="USDT",
            amount=float(f"{usdt_to_transfer:.2f}"),
            fromAccount="spot",
            toAccount="swap",
        )
        print(f"划转成功: {result}")
    except Exception as e:
        print(f"划转失败: {e}")
else:
    print("USDT 余额过少，无需划转")

# 5. 确认最终余额
final_spot = exchange.fetch_balance()
final_futures = exchange_futures.fetch_balance()
print(f"\n最终 现货 USDT: {final_spot.get('total', {}).get('USDT', 0)}")
print(f"最终 合约 USDT: {final_futures.get('total', {}).get('USDT', 0)}")
