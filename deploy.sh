#!/bin/bash
# One-click deploy to cloud server (Ubuntu/Debian/CentOS)
# Run as root: bash deploy.sh

set -e

APP_DIR="/opt/gate-monitor"

echo "=== Gate.io Futures Monitor Deployment ==="

# Install Python
if ! command -v python3 &>/dev/null; then
    echo "Installing Python3..."
    if command -v apt &>/dev/null; then
        apt update && apt install -y python3 python3-pip
    elif command -v yum &>/dev/null; then
        yum install -y python3 python3-pip
    fi
fi

# Create directory
mkdir -p "$APP_DIR/logs"

# Copy files (run this script from the project directory)
SRC="$(cd "$(dirname "$0")" && pwd)"
if [ "$SRC" != "$APP_DIR" ]; then
    cp -r "$SRC"/*.py "$SRC"/*.txt "$SRC"/*.sh "$SRC"/.env "$SRC"/monitor "$APP_DIR/" 2>/dev/null || true
    cp "$SRC"/.env "$APP_DIR/" 2>/dev/null || true
fi

# Install deps
pip3 install -r "$APP_DIR/requirements.txt"

# Setup systemd service
cp "$APP_DIR/monitor.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable gate-monitor
systemctl start gate-monitor

echo ""
echo "Deployed!"
echo "  Status:  systemctl status gate-monitor"
echo "  Logs:    tail -f $APP_DIR/logs/monitor.log"
echo "  Stop:    systemctl stop gate-monitor"
echo "  Restart: systemctl restart gate-monitor"
