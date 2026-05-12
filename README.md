# QuantBot - 自动量化交易系统

基于 Bitget API 的多因子策略自动交易系统，支持 WebSocket 实时数据、风控管理、FastAPI 接口、Scanner V2（早期爆发币扫描）。

---

## 📁 项目结构

```
quantbot/
├── config/
│   └── config.yaml          # 配置文件（策略/风控/Scanner/日志参数）
├── src/
│   ├── __init__.py
│   ├── data/                # 数据层
│   │   ├── indicators.py    # 技术指标（RSI/ADX/布林带/ATR/MA交叉）
│   │   ├── kline.py        # K线数据获取 + 缓存
│   │   ├── websocket.py    # WebSocket 实时价格
│   │   ├── market.py       # 市场数据（ticker/订单簿）
│   │   └── cmc_data.py     # CoinMarketCap 数据增强
│   ├── strategy/           # 策略层
│   │   ├── base.py         # 策略基类（BaseStrategy, SignalResult, Signal）
│   │   ├── market_regime.py # 市场状态检测（TREND/SIDEWAYS/HIGH_VOL）
│   │   ├── strategy_router.py # 多策略融合路由器
│   │   ├── multi_factor.py # 趋势策略（MA + 资金费率 + 成交量）
│   │   ├── rsi_bollinger.py # 均值回归策略（RSI + 布林带）
│   │   ├── volume_momentum.py # 成交量动量策略
│   │   └── funding_rate.py # 资金费率策略
│   ├── risk/               # 风控层
│   │   ├── risk_manager.py # 风控（双方向仓位/止损/止盈/日限/ATR动态止损）
│   │   ├── market_filter.py # 市场过滤器（ATR/布林带/财经日历）
│   │   └── audit_logger.py # 审计日志
│   ├── execution/          # 执行层
│   │   └── trader.py      # 交易执行（市价单 + 重试 + 滑点）
│   ├── account/            # 账户管理
│   │   └── account_manager.py
│   ├── backtest/           # 回测引擎
│   │   └── backtest.py
│   ├── scanner/            # 市场扫描（Scanner V2）
│   │   ├── scanner_service.py  # 主扫描服务（整合所有引擎）
│   │   ├── feature_engine.py   # 特征计算（动量/成交量/ATR/订单簿）
│   │   ├── signal_engine.py   # 信号生成（Early Breakout 检测）
│   │   ├── scoring_engine.py  # 加权评分系统
│   │   ├── filter_engine.py   # 过滤器（流动性/价差/波动率）
│   │   ├── ranking_engine.py  # 排名输出
│   │   ├── data_source.py     # 数据获取（Bitget API）
│   │   └── coin_scanner.py    # 旧版扫描器（兼容）
│   └── utils/
│       └── logger.py       # 日志系统（loguru）
├── logs/                   # 运行日志（自动创建）
├── templates/
│   └── dashboard.html     # Web Dashboard
├── main.py                 # 主程序 + FastAPI 入口
├── requirements.txt
├── .env.example
└── README.md
```

---

## ⚡ 快速启动

### 第一步：进入项目目录

```bash
cd /Users/meixintang/Documents/quantbot
```

### 第二步：创建虚拟环境并安装依赖

```bash
# 使用 uv（推荐，速度快）
uv venv .venv
source .venv/bin/activate        # macOS / Linux

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

编辑 `.env`，填入你的 Bitget API Key：

```env
BITGET_API_KEY=你的API_KEY
BITGET_SECRET_KEY=你的SECRET_KEY
BITGET_PASSWORD=你的API_PASSPHRASE
```

> ⚠️ 如果不填写 API Key，系统会自动进入**模拟交易模式**（dry run），不会真实下单，适合测试。

### 第四步：（可选）调整策略参数

编辑 `config/config.yaml`：

```yaml
strategy:
  interval: 60      # 策略执行间隔（秒）

  # 市场状态检测参数
  regime:
    adx_period: 14
    adx_trend_threshold: 25.0   # ADX > 25 = 趋势市场
    atr_low_threshold: 0.03      # ATR% < 3% = 低波动
    atr_high_threshold: 0.05     # ATR% > 5% = 高波动

  # 各策略参数
  ma:
    short_period: 5
    long_period: 20
  rsi:
    period: 14
    overbought: 70
    overbought: 30

  # 策略权重（按市场状态动态调整）
  regime_weights:
    TREND:    {ma: 0.6, funding: 0.2, rsi_bb: 0.05, volume: 0.15}
    SIDEWAYS: {ma: 0.1, funding: 0.1, rsi_bb: 0.5, volume: 0.3}
    HIGH_VOL: {ma: 0.25, funding: 0.1, rsi_bb: 0.25, volume: 0.4}

risk:
  max_position_size: 0.2   # 单笔最大仓位 20%
  stop_loss: 0.02           # 止损 2%
  take_profit: 0.05          # 止盈 5%
  max_daily_loss: 0.1       # 单日最大亏损 10%
  max_trades_per_day: 20    # 每日最大交易次数
  atr_stop_loss_enabled: true  # 启用 ATR 动态止损
  atr_multiplier: 2.0         # ATR 倍数（止损距离 = 2 * ATR）
  consecutive_loss_limit: 3    # 连续亏损次数限制
  consecutive_loss_reduction: 0.5  # 连续亏损后仓位缩减比例
```

### 第五步：启动系统

```bash
PYTHONPATH=. .venv/bin/python main.py
```

启动后输出示例：

```
2026-05-02 10:00:00 | INFO | 🚀 QuantBot 启动中...
2026-05-02 10:00:00 | WARNING | 🧪 未检测到真实 API Key，已自动开启模拟交易模式
2026-05-02 10:00:00 | INFO | 📡 WebSocket 启动...
2026-05-02 10:00:00 | INFO | 🔍 Scanner V2 启用 | auto_add=True
2026-05-02 10:00:00 | INFO | 🚀 策略循环启动，执行间隔: 60s | 交易对: ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']
2026-05-02 10:00:01 | INFO | BTC/USDT 信号: SELL | 强度=-0.461 | 价格=77561.15 | regime=sideways
```

---

## 🌐 REST API 接口

系统启动后访问 `http://localhost:8000`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 系统状态信息 |
| GET | `/dashboard` | Web Dashboard（HTML） |
| GET | `/api/prices` | 所有交易对实时价格 |
| GET | `/api/price/{symbol}` | 指定交易对价格 |
| GET | `/api/signal/{symbol}` | 指定交易对策略信号 |
| GET | `/api/signals` | 所有交易对融合信号 |
| GET | `/api/positions` | 当前持仓（多空分离） |
| GET | `/api/balance` | 账户余额总览 |
| GET | `/api/stats` | 交易统计 |
| GET | `/api/backtest` | 快速回测 |
| POST | `/api/run` | 手动触发一次策略 |
| POST | `/api/scanner/scan` | 立即触发 Scanner 扫描 |
| GET | `/api/scanner/candidates` | Scanner 候选币列表 |
| POST | `/api/scanner/trigger` | 后台触发 Scanner |
| POST | `/api/scanner/apply` | 手动设置交易币种 |
| GET | `/scanner/long-candidates` | 做多候选币（缓存） |
| GET | `/scanner/short-candidates` | 做空候选币（预留） |
| GET | `/scanner/alerts` | 早期爆发信号（early stage + score>0.5） |
| GET | `/scanner/detail/{symbol}` | 单币分析详情 |
| GET | `/scanner/top` | Top N 排名 |
| GET | `/scanner/raw` | 原始评分列表 |
| GET | `/signals` | 所有信号（alias） |
| GET | `/account/summary` | 账户总览 |
| GET | `/account/trades` | 成交记录 |
| POST | `/account/transfer` | 资金划转（现货 ↔ 合约） |
| GET | `/market/klines` | K线数据 |
| GET | `/market/overview` | 市场总览（stub） |
| GET | `/market/tickers` | 市场行情（stub） |

**Swagger 文档：** `http://localhost:8000/docs`

---

## 🔍 Scanner V2（早期爆发币扫描系统）

### 架构

```
scanner_service.py     # 主扫描服务（整合所有引擎）
├── feature_engine.py  # 特征计算
│   ├── momentum_1m/5m/15m    # 多时间动量
│   ├── volume_spike         # 成交量爆发倍数
│   ├── breakout_ratio       # 突破比率（价格/20期高点）
│   ├── atr                  # 波动率
│   ├── taker_buy_ratio     # 主动买入比例
│   └── orderbook_imbalance # 订单簿多空失衡度
├── signal_engine.py    # 信号生成
│   ├── Early Breakout 检测  # momentum_1m>0.5% + momentum_5m>2% + vol>2x
│   ├── 资金推动检测         # taker_buy_ratio>0.6
│   └── 突破确认检测         # breakout_ratio>1.01
├── scoring_engine.py   # 加权评分
│   ├── 0.3 × momentum_5m
│   ├── 0.2 × momentum_1m
│   ├── 0.2 × volume_spike
│   ├── 0.15 × taker_buy_ratio
│   └── 0.15 × orderbook_imbalance
├── filter_engine.py    # 过滤器
│   ├── min_volume_24h > $1M
│   ├── max_spread < 0.5%
│   └── max_change_24h < 80%
└── ranking_engine.py   # 排名输出
    ├── stage 分类（early/mid/late）
    └── Top N 输出
```

### Scanner API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/scanner/long-candidates` | 做多候选币（自动更新缓存） |
| GET | `/scanner/alerts` | 早期爆发信号（score>0.5，stage=early） |
| GET | `/scanner/detail/{symbol}` | 单币详细分析 |
| GET | `/scanner/top` | Top N 综合排名 |
| GET | `/scanner/raw` | 原始评分数据 |
| POST | `/api/scanner/trigger` | 手动触发扫描 |
| POST | `/api/scanner/apply` | 应用候选币到交易列表 |

### Scanner 配置（config.yaml）

```yaml
scanner:
  enabled: true
  scan_interval: 10          # 每 10 秒扫描一次
  auto_add: true            # 自动将 Top 币加入交易列表
  top_n: 20

  # 信号引擎阈值
  momentum_1m: 0.005         # 1分钟动量阈值
  momentum_5m: 0.02          # 5分钟动量阈值
  volume_spike: 2.0          # 成交量爆发倍数阈值
  taker_buy_ratio: 0.6       # 主动买入比例阈值
  breakout_threshold: 1.01   # 突破阈值

  # 打分权重
  w_momentum_5m: 0.30
  w_momentum_1m: 0.20
  w_volume_spike: 0.20
  w_taker_buy: 0.15
  w_orderbook: 0.15

  # 过滤器
  min_volume: 1000000        # 最低 24h 成交量
  max_spread: 0.005         # 买卖价差上限
  max_change_24h: 80.0       # 24h 涨跌幅上限
```

### 阶段分类（stage）

| 阶段 | 说明 | 策略建议 |
|------|------|---------|
| **early** | 刚刚启动，潜在回报最高 | 重点关注，early breakout 信号 |
| **mid** | 已启动一段时间 | 稳健持有，注意回撤 |
| **late** | 接近尾声 | 谨慎，追高风险大 |

---

## 🧠 策略说明

### 市场状态检测（Regime Detection）

系统根据 ADX + ATR% 动态判断市场状态：

| 状态 | 判断条件 | 策略重点 |
|------|---------|---------|
| **TREND**（趋势） | ADX > 25 | MA 交叉 + 资金费率 |
| **SIDEWAYS**（震荡） | ADX < 25 且 ATR% < 3% | RSI + 布林带均值回归 |
| **HIGH_VOL**（高波动） | ADX < 25 且 ATR% > 5% | 成交量动量，轻仓观望 |

### 多策略融合（Strategy Router）

`/api/signals` 接口返回每个交易对的融合信号及子策略详情：

```json
{
  "BTC/USDT": {
    "signal": "SELL",
    "score": -0.448,
    "confidence": 0.792,
    "regime": "sideways",
    "strategies": {
      "ma": {"signal": "SELL", "score": -0.508, "metadata": {...}},
      "rsi_bb": {"signal": "SELL", "score": -0.600, "metadata": {...}},
      "volume": {"signal": "SELL", "score": -0.345, "metadata": {...}},
      "funding": {"signal": "HOLD", "score": 0.059, "metadata": {...}}
    },
    "weights": {"ma": 0.1, "rsi_bb": 0.5, "volume": 0.3, "funding": 0.1}
  }
}
```

### 子策略说明

| 策略 | 文件 | 适用市场 | 逻辑 |
|------|------|---------|------|
| **MA交叉** | `multi_factor.py` | TREND | MA5/MA20 金叉做多，死叉做空 |
| **RSI+布林带** | `rsi_bollinger.py` | SIDEWAYS | RSI<30+价格触布林带下轨→买入；RSI>70+价格触上轨→卖出 |
| **成交量动量** | `volume_momentum.py` | ALL | 放量+涨→多头；放量+跌→空头；缩量→谨慎 |
| **资金费率** | `funding_rate.py` | TREND | 负费率→做多有利；正费率→做空有利 |

### 信号阈值

- 综合得分 > **+0.3** → `BUY`
- 综合得分 < **-0.3** → `SELL`
- 其余 → `HOLD`

### 双方向持仓交易逻辑（Bitget Hedge Mode）

| 信号 | 持仓状态 | 动作 |
|------|---------|------|
| BUY | 无 | 开多仓（LONG） |
| BUY | 有空仓 | 平空仓（SHORT）后再开多 |
| SELL | 有长仓 | 平多仓（LONG） |
| SELL | 无 | 开空仓（SHORT） |
| SL/TP | 长仓 | 平多仓（LONG） |
| SL/TP | 空仓 | 平空仓（SHORT） |

---

## 🛡️ 风控规则

| 规则 | 默认值 |
|------|--------|
| 杠杆倍数 | 20x |
| 单笔最大仓位 | 100% 可用余额 |
| 固定止损 | 入场价 ±40% |
| 固定止盈 | 入场价 ±1.5% |
| ATR 动态止损 | 止损距离 = 1.5 × ATR |
| ATR 动态止盈 | 止盈距离 = 3 × ATR |
| 单日最大亏损 | 总余额 10% |
| 每日最大交易次数 | 20 次 |
| 最大持仓交易对数 | 5 个 |
| 连续亏损保护 | 3 次后仓位减半 |
| 分批止盈 | 2%时平30%，5%时平50%，启用追踪止盈 |

### 动态仓位调整

| 市场状态 | 仓位比例 |
|---------|---------|
| TREND | 100% |
| SIDEWAYS | 75% |
| HIGH_VOL | 50% |
| 连续亏损 ≥3 次 | 50%（叠加） |

### 市场过滤器（MarketFilter）

- **ATR 波动率过滤**：ATR% > 12% 警告，> 25% 拒绝开仓
- **布林带偏离过滤**：价格偏离 > 2.5σ 警告
- **财经日历过滤**：高影响力事件前后 30min 暂停交易

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
