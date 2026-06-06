# Binance Futures Pump Monitor 币安合约异动监控

监控所有币安 USDT 永续合约，检测 1 分钟内涨幅 >2% 的合约，每 5 分钟生成报告并通过 Telegram Bot 发送到群组。

## 功能

- 📡 实时监控币安所有 USDT 永续合约（WebSocket 推送）
- 📊 仅监控 24h 交易量 > 3000万 USDT 的合约
- ⚡ 1 分钟涨幅超过 2% 自动记录
- 📋 每 5 分钟生成汇总报告
- 🤖 通过 Telegram Bot 自动发送到群组

## 安装

### 1. 安装 Python 3.9+

下载: https://www.python.org/downloads/

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 配置 (.env)

编辑 `.env` 文件，填入你的 Telegram 信息：

```env
# Telegram Bot Token (已预填)
TELEGRAM_BOT_TOKEN=8946385457:AAFG3RKJmDqmScuebTbKfw_zDvAE1zrLq6w

# 群组 Chat ID (必填！)
TELEGRAM_CHAT_ID=-1001234567890

# 监控参数（可选修改）
MIN_VOLUME_USDT=30000000       # 最低24h交易量 (默认3000万)
PUMP_THRESHOLD_PCT=2.0          # 涨幅阈值 (默认2%)
REPORT_INTERVAL_MINUTES=5       # 报告间隔 (默认5分钟)
```

### 4. 获取 Telegram Chat ID

1. 将你的 Bot 加入群组
2. 在群组中发送一条消息
3. 访问: `https://api.telegram.org/bot<TOKEN>/getUpdates`
4. 从返回的 JSON 中找到 `chat.id`

## 运行

```bash
python main.py
```

按 `Ctrl+C` 停止。

## 项目结构

```
.
├── main.py                      # 主入口
├── config.py                    # 配置加载
├── .env                         # 环境变量 (不提交 git)
├── requirements.txt             # 依赖
├── monitor/
│   ├── __init__.py
│   ├── binance_fetcher.py       # 币安数据获取 (REST + WebSocket)
│   ├── detector.py              # 涨幅检测逻辑
│   ├── reporter.py              # 报告格式化
│   └── telegram_sender.py       # Telegram Bot 发送
└── logs/
    └── monitor.log              # 运行日志
```
