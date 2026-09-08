# CoinGlass 风格 成交量 / OI 异动监控

监控币安上有的币种，观察两类异动并按规定档位通过 Telegram 提醒：

1. **单日成交量涨幅**：超 70% 提醒一次，100% 一次，200% 一次，依次增加。
2. **OI 一小时涨跌幅**：10% 一次，20% 一次，30% 一次，40%，50%……依次增加（看涨/看跌分开记忆）。

## 功能

- 阶梯告警：每档每天只提醒一次，爬升到更高的新档位再提醒；跨天自动重置。
- 状态持久化到 `monitor/alert_state.json`，重启不会重复提醒。
- 仅监控 Binance USDT 永续合约（读取 `binance_usdt_perps.txt`，并定期从 Binance 实时刷新最新交易对）。
- 首次观测某币时只发一条“当前最高档”，避免启动时已暴涨币种的逐档刷屏。
- Telegram Bot 推送；`DRY_RUN=1` 时只打日志、不发送。

## 数据源

- **binance**（默认，免 key）：币安合约数据。恰好只覆盖“币安上有的币种”，开箱即用。
- **coinglass**（聚合数据，需 key）：在 `.env` 填 `COINGLASS_API_KEY`，并设 `DATA_SOURCE=coinglass`。CoinGlass 开放接口对无 key 请求返回 500，所以未填 key 时自动降级为 binance。

## 配置（.env）

```env
# Telegram
TELEGRAM_BOT_TOKEN=你的Bot Token
TELEGRAM_CHAT_ID=你的群Chat ID
TELEGRAM_SEND_INTERVAL_SECONDS=3.5
TELEGRAM_MAX_RETRIES=4

# 数据源（留空自动选择：有 key 用 coinglass，否则 binance）
DATA_SOURCE=
COINGLASS_API_KEY=

# 档位（升序，逗号分隔）
VOLUME_THRESHOLDS=70,100,200,300,400,500,600,700,800,900,1000,1500,2000,3000
OI_THRESHOLDS=10,20,30,40,50,60,70,80,90,100,150,200,300

# 只监控 24h 成交额 >= 该值 的币
MIN_VOLUME_USDT=5000000

# 扫描间隔（秒）；binace 源每个周期会遍历所有币的 kline+OI 历史，建议 >=120
CHECK_INTERVAL_SECONDS=120
SYMBOL_REFRESH_SECONDS=43200
FETCH_WORKERS=8
FETCH_MIN_INTERVAL_SECONDS=0.08

# 状态文件 / 安全测试
STATE_FILE=monitor/alert_state.json
ALERT_STATE_RESET_DAILY=true
DRY_RUN=false

# 端口（Render 健康检查）
PORT=8080

# 代理（留空则直连；本例走系统代理）
HTTP_PROXY=
HTTPS_PROXY=
TELEGRAM_PROXY=http://127.0.0.1:7897
```

## 运行

```bash
pip install -r requirements.txt
python main.py
```

设置 `DRY_RUN=1` 先看日志确认逻辑，无误后再去掉该开关。

## 注意

- Telegram：bot 必须被拉进目标群，且 `TELEGRAM_CHAT_ID` 是群 ID（`-100...`）。若发送报 `Chat not found`，说明 bot 不在该群或 chat id 不对。
- 网络：本项目在中国大陆网络下需要可用的代理。`.env` 里 `TELEGRAM_PROXY` 用于 Telegram；数据源会自动使用 Windows 系统代理（若设置了代理服务器）。

## 部署（Render）

沿用 `render.yaml` / `Dockerfile` / `deploy.sh`。在 Render 配好 `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID` 环境变量即可。

## 项目结构

```
main.py                      # 主循环 + 健康检查
config.py                    # 配置加载
binance_usdt_perps.txt       # Binance 币种白名单
monitor/
  sources.py                 # Binance / CoinGlass 数据源
  threshold_monitor.py       # 阶梯告警检测 + 状态
  state.py                   # 每日状态持久化
  reporter.py                # 告警文案格式化
  telegram_sender.py         # Telegram 发送
  alert_state.json           # 运行期生成（已 gitignore）
logs/monitor.log             # 运行日志（gitignore）
```

> 旧版 Gate.io pump/dump 监控模块（`gate_fetcher.py`、`detector.py`、`trading_signal.py`、`paper_trader.py`）已不被 main.py 引用，仅作留档。