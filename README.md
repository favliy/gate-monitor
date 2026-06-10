# Gate.io Futures Monitor 合约异动监控

监控 Gate.io USDT 永续合约（过滤到 Binance 已上线品种），检测 1 分钟内涨/跌幅 >= 2% 的合约，实时 Telegram 告警 + 每 5 分钟汇总报告。

## 功能

- 📡 实时监控 Gate.io USDT 永续合约（REST API 轮询）
- 📊 仅监控 24h 成交额 >= 450万 USDT 的合约
- 📈 1 分钟涨/跌幅 >= 2% 自动记录 + Telegram 即时告警
- 📉 5 分钟 OI 变化 >= 5% 异动检测
- 🎯 内置交易信号引擎（入场/止损/止盈/仓位评分）
- 📋 每 5 分钟生成汇总报告
- 🤖 通过 Telegram Bot 自动发送

## 本地运行

### 1. 安装 Python 3.9+

```bash
pip install -r requirements.txt
```

### 2. 配置 .env

编辑 `.env` 文件：

```env
TELEGRAM_BOT_TOKEN=你的Bot Token
TELEGRAM_CHAT_ID=你的Chat ID
MIN_VOLUME_USDT=4500000
PUMP_THRESHOLD_PCT=2.0
REPORT_INTERVAL_MINUTES=5
CHECK_INTERVAL_SECONDS=10
```

### 3. 运行

```bash
python main.py
```

## 一键部署到 Render（免费 24/7）

项目已配置 `render.yaml`，支持 Render 一键部署：

### 步骤

1. **Push 到 GitHub**
   ```bash
   git push origin main
   ```

2. **在 Render 创建 Web Service**
   - 打开 https://render.com
   - 用 GitHub 账号登录
   - 点击 "New +" → "Web Service"
   - 连接仓库 `favliy/gate-monitor`
   - Render 会自动读取 `render.yaml` 配置
   - 手动填入两个 Secret 环境变量：
     - `TELEGRAM_BOT_TOKEN`
     - `TELEGRAM_CHAT_ID`
   - 选择 Free 计划，点击 "Create Web Service"

3. **验证**
   - 部署完成后，你的 Bot 会发送一条连接成功的消息
   - 健康检查端点: `https://gate-monitor.onrender.com/`
   - 免费额度: 750 小时/月（足够 24/7 运行）

### 保持在线

Render 免费服务会在 15 分钟无请求后休眠。项目内置了健康检查端点，配合外部监控（如 UptimeRobot 免费版）每 5 分钟 ping 一次 `https://你的服务地址.onrender.com/` 即可保持 24/7 在线。

## 其他部署方式

### Docker

```bash
docker build -t gate-monitor .
docker run -d --env-file .env gate-monitor
```

### VPS (Ubuntu/Debian)

```bash
bash deploy.sh   # 自动安装 systemd 服务
```

## 项目结构

```
.
├── main.py                      # 主入口 + 健康检查服务器
├── config.py                    # 配置加载
├── render.yaml                  # Render 一键部署配置
├── Dockerfile                   # Docker 构建
├── deploy.sh / run.sh           # 部署/运行脚本
├── .env                         # 环境变量 (不提交 git)
├── requirements.txt             # 依赖
├── binance_usdt_perps.txt       # Binance 合约白名单
├── monitor/
│   ├── gate_fetcher.py          # Gate.io 数据抓取
│   ├── detector.py              # Pump/Dump/OI 检测
│   ├── reporter.py              # 报告格式化
│   ├── telegram_sender.py       # Telegram Bot 发送
│   └── trading_signal.py        # 交易信号引擎
└── logs/
    └── monitor.log              # 运行日志
```
