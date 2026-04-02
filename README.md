# QuantBot - 自动量化交易系统

基于 Binance API 的多因子策略自动交易系统，支持 WebSocket 实时数据、风控管理、FastAPI 接口。

---

## 📁 项目结构

```
quantbot/
├── config/
│   └── config.yaml          # 配置文件（策略/风控/日志参数）
├── src/
│   ├── __init__.py
│   ├── data/
│   │   ├── __init__.py
│   │   ├── kline.py         # K线数据获取 + 缓存
│   │   └── websocket.py     # WebSocket 实时价格
│   ├── strategy/
│   │   ├── __init__.py
│   │   └── multi_factor.py  # 多因子策略（MA + 资金费率 + 成交量）
│   ├── risk/
│   │   ├── __init__.py
│   │   └── risk_manager.py  # 风控（仓位/止损/止盈/日限）
│   ├── execution/
│   │   ├── __init__.py
│   │   └── trader.py        # 交易执行（市价单 + 重试）
│   └── utils/
│       ├── __init__.py
│       └── logger.py        # 日志系统（loguru）
├── logs/                    # 运行日志（自动创建）
├── main.py                  # 主程序 + FastAPI 入口
├── requirements.txt
├── .env.example
└── README.md
```

---

## ⚡ 快速启动

### 第一步：克隆 / 进入项目目录

```bash
cd quantbot
```

### 第二步：创建虚拟环境并安装依赖

```bash
# 使用 uv（推荐，速度快）
uv venv .venv
source .venv/bin/activate        # macOS / Linux
# .venv\Scripts\activate         # Windows

uv pip install -r requirements.txt

# 或使用标准 pip
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 第三步：配置环境变量

```bash
cp .env.example .env
```

编辑 `.env`，填入你的 Binance API Key：

```env
BINANCE_API_KEY=你的API_KEY
BINANCE_SECRET_KEY=你的SECRET_KEY
```

> ⚠️ 如果不填写 API Key，系统会自动进入**模拟交易模式**（dry run），不会真实下单，适合测试。

### 第四步：（可选）调整策略参数

编辑 `config/config.yaml`，按需修改：

```yaml
strategy:
  ma_short: 5       # 短期均线
  ma_long: 20       # 长期均线
  interval: 60      # 策略执行间隔（秒）

risk:
  max_position_size: 0.2   # 单笔最大仓位 20%
  stop_loss: 0.02          # 止损 2%
  take_profit: 0.05        # 止盈 5%
```

### 第五步：启动系统

```bash
python main.py
```

启动后输出示例：

```
2026-04-03 01:00:00 | INFO     | 🟢 QuantBot 启动中...
2026-04-03 01:00:00 | WARNING  | 🧪 未检测到真实 API Key，已自动开启模拟交易模式
2026-04-03 01:00:00 | INFO     | 📡 WebSocket 启动...
2026-04-03 01:00:00 | INFO     | 🚀 策略循环启动，执行间隔: 60s
2026-04-03 01:00:01 | INFO     | 当前价格: BTC/USDT = 83241.50 USDT
2026-04-03 01:00:01 | INFO     | 策略信号: BUY | 强度=0.412
```

---

## 🌐 REST API 接口

系统启动后访问 `http://localhost:8000`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 健康检查 |
| GET | `/price` | 获取最新价格 |
| GET | `/klines` | 获取K线数据 |
| GET | `/signal` | 获取当前策略信号 |
| GET | `/positions` | 查看当前持仓 |
| GET | `/balance` | 查看账户余额 |
| GET | `/stats` | 查看交易统计 |
| POST | `/run` | 手动触发一次策略 |

**Swagger 文档：** `http://localhost:8000/docs`

---

## 🧠 策略说明

### 多因子模型

| 因子 | 权重 | 说明 |
|------|------|------|
| MA5 vs MA20 | 50% | 金叉做多，死叉做空 |
| 资金费率 | 30% | 正费率偏空，负费率偏多 |
| 成交量异动 | 20% | 放量确认信号 |

### 信号阈值

- 综合得分 > **+0.3** → `BUY`
- 综合得分 < **-0.3** → `SELL`
- 其余 → `HOLD`

---

## 🛡️ 风控规则

| 规则 | 默认值 |
|------|--------|
| 单笔最大仓位 | 20% 可用余额 |
| 止损 | 入场价 -2% |
| 止盈 | 入场价 +5% |
| 单日最大亏损 | 总余额 10% |
| 每日最大交易次数 | 20 次 |

---

## 📋 日志

日志文件保存在 `logs/quantbot.log`，自动轮转（10MB），保留 7 天。

```bash
# 实时查看日志
tail -f logs/quantbot.log
```

---

## ⚠️ 免责声明

本项目仅供学习和研究使用，不构成任何投资建议。加密货币交易存在极高风险，请在充分了解风险的前提下谨慎使用。**作者不对任何交易损失负责。**
