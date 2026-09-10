#!/usr/bin/env bash
# Run BOTH monitors inside a single container (one Render free instance):
#   legacy/ : Monitor v3.4  (Gate.io pump/dump, OI spikes, hot coins, funding)
#   ./      : CoinGlass-style volume/OI ladder monitor (Binance USDT perps)
# Render health-checks the front monitor on $PORT; legacy uses an internal port.
set -uo pipefail

cd /app
mkdir -p /app/logs /app/legacy/logs

LEGACY_PORT="${LEGACY_PORT:-8081}"

echo "[start] Monitor v3.4 (legacy) -> internal port ${LEGACY_PORT}"
(
  cd /app/legacy
  PORT="$LEGACY_PORT" \
  TELEGRAM_BOT_TOKEN="${LEGACY_TELEGRAM_BOT_TOKEN:-${TELEGRAM_BOT_TOKEN:-}}" \
  TELEGRAM_CHAT_ID="${LEGACY_TELEGRAM_CHAT_ID:-${TELEGRAM_CHAT_ID:-}}" \
  exec python -u main.py
) &
LEGACY_PID=$!

echo "[start] CoinGlass-style monitor -> port ${PORT:-8080}"
python -u /app/main.py &
MAIN_PID=$!

shutdown() {
  echo "[start] stop signal; terminating monitors"
  kill "$LEGACY_PID" "$MAIN_PID" 2>/dev/null
  wait "$LEGACY_PID" "$MAIN_PID" 2>/dev/null
}
trap shutdown TERM INT

# If either monitor dies, take the container down so Render restarts it cleanly.
wait -n "$LEGACY_PID" "$MAIN_PID"
STATUS=$?
echo "[start] a monitor exited (status=$STATUS); stopping the other"
kill "$LEGACY_PID" "$MAIN_PID" 2>/dev/null
exit "$STATUS"