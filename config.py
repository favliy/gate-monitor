import os
from dotenv import load_dotenv
import os as _os

load_dotenv(_os.path.join(_os.path.dirname(__file__), ".env"))


def _floats(name: str, default: str) -> list:
    raw = os.getenv(name, default).strip()
    values = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            values.append(float(part))
        except ValueError:
            pass
    return values


# ---- Telegram ----
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ---- Data source ----
# coinglass: aggregated (all-exchange) OI/volume, requires COINGLASS_API_KEY.
# binance:   Binance-only USDT perps, no key needed. Also exactly matches the
#            "only coins listed on Binance" filter.
COINGLASS_API_KEY = os.getenv("COINGLASS_API_KEY", "")
DATA_SOURCE = os.getenv("DATA_SOURCE", "").strip().lower()
if DATA_SOURCE not in ("coinglass", "binance"):
    DATA_SOURCE = "coinglass" if COINGLASS_API_KEY else "binance"

COINGLASS_BASE = os.getenv("COINGLASS_BASE", "https://open-api.coinglass.com")
BINANCE_FAPI_BASE = os.getenv("BINANCE_FAPI_BASE", "https://fapi.binance.com")

# ---- Volume ladder ----
# Daily volume increase thresholds. Alert once per threshold that gets crossed.
# Default: 70 / 100 / 200, then +100 per level.
VOLUME_THRESHOLDS = _floats(
    "VOLUME_THRESHOLDS",
    "70,100,200,300,400,500,600,700,800,900,1000,1500,2000,3000",
)

# ---- OI ladder ----
# 1-hour OI change (absolute) thresholds. Alert once per threshold.
# Default: 10 / 20 / 30 / 40 / 50, then +10 per level.
OI_THRESHOLDS = _floats(
    "OI_THRESHOLDS",
    "10,20,30,40,50,60,70,80,90,100,150,200,300",
)

# ---- Volume floor ----
# Only monitor Binance coins with 24h quote volume >= MIN_VOLUME_USDT.
MIN_VOLUME_USDT = float(os.getenv("MIN_VOLUME_USDT", "10000000"))

# ---- Cadence ----
# Main loop interval. The binance source polls klines + OI history for every
# symbol each pass, so keep this >= 120s by default.
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "120"))
# How often to refresh the Binance trading-pair whitelist.
SYMBOL_REFRESH_SECONDS = int(os.getenv("SYMBOL_REFRESH_SECONDS", "43200"))

# ---- Concurrency / rate limiting ----
FETCH_WORKERS = int(os.getenv("FETCH_WORKERS", "8"))
FETCH_MIN_INTERVAL_SECONDS = float(os.getenv("FETCH_MIN_INTERVAL_SECONDS", "0.08"))

# ---- Alert state ----
STATE_FILE = os.getenv(
    "STATE_FILE",
    _os.path.join(_os.path.dirname(__file__), "monitor", "alert_state.json"),
)
# Reset per-threshold state each UTC day so daily-volume alerts fire once/day.
ALERT_STATE_RESET_DAILY = os.getenv("ALERT_STATE_RESET_DAILY", "true").lower() == "true"

# ---- Proxies ----
HTTP_PROXY = os.getenv("HTTP_PROXY", "")
HTTPS_PROXY = os.getenv("HTTPS_PROXY", "")
TELEGRAM_PROXY = os.getenv("TELEGRAM_PROXY", "")





# ---- Telegram sending (flood-control protection) ----
# Min interval between two Telegram messages, and retries on 429 flood control.
TELEGRAM_SEND_INTERVAL_SECONDS = float(os.getenv("TELEGRAM_SEND_INTERVAL_SECONDS", "3.5"))
TELEGRAM_MAX_RETRIES = int(os.getenv("TELEGRAM_MAX_RETRIES", "4"))
# ---- Dry run ----
# When true, alerts are logged but NOT sent to Telegram (safe for testing).
DRY_RUN = os.getenv("DRY_RUN", "false").strip().lower() in ("true", "1", "yes")
# ---- Health / misc ----
PORT = int(os.getenv("PORT", "8080"))