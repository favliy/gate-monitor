#!/bin/bash
# docker-entrypoint.sh - Clean old modules every container start
set -e
cd /app
echo "[entrypoint] Cleaning old modules..."
rm -f monitor/reporter.py monitor/trading_signal.py monitor/paper_trader.py 2>/dev/null || true
find /app -name "*.pyc" -delete 2>/dev/null || true
find /app -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
echo "[entrypoint] Clean. Starting monitor..."
exec python /app/main.py