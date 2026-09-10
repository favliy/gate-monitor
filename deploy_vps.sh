#!/usr/bin/env bash
# One-shot deploy for the CoinGlass-style Binance OI/volume monitor.
# Target: a fresh Ubuntu 22.04/24.04 (or Debian 12) VM with a public IP.
# Usage (as root):  bash deploy_vps.sh
set -euo pipefail

APP_DIR="/opt/coinglass-monitor"
REPO="https://github.com/favliy/gate-monitor.git"
BRANCH="coinglass-monitor"
SERVICE="coinglass-monitor"

echo "=== [1/5] system packages ==="
if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y python3 python3-venv python3-pip git curl
else
  echo "This script supports Ubuntu/Debian only." >&2
  exit 1
fi

echo "=== [2/5] fetch code ($BRANCH) ==="
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" fetch --all
  git -C "$APP_DIR" checkout "$BRANCH"
  git -C "$APP_DIR" reset --hard "origin/$BRANCH"
else
  git clone -b "$BRANCH" "$REPO" "$APP_DIR"
fi
mkdir -p "$APP_DIR/logs"

echo "=== [3/5] python venv + deps ==="
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip >/dev/null
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "=== [4/5] systemd service ==="
cat >"/etc/systemd/system/${SERVICE}.service" <<EOF
[Unit]
Description=CoinGlass-style Binance OI/volume monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
EnvironmentFile=-$APP_DIR/.env
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/main.py
Restart=always
RestartSec=10
StandardOutput=append:$APP_DIR/logs/monitor.log
StandardError=append:$APP_DIR/logs/monitor.log

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable "$SERVICE"
systemctl restart "$SERVICE"

echo "=== [5/5] status ==="
sleep 3
systemctl --no-pager --full status "$SERVICE" || true
echo
echo "Done."
echo "  logs:    journalctl -u $SERVICE -f"
echo "  file:    $APP_DIR/logs/monitor.log"
echo "  restart: systemctl restart $SERVICE"
echo
echo "NOTE: put credentials in $APP_DIR/.env (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID / DRY_RUN=...)"