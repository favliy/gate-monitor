import os
from dotenv import load_dotenv
import os as _os

load_dotenv(_os.path.join(_os.path.dirname(__file__), ".env"), override=True)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
MIN_VOLUME_USDT = float(os.getenv("MIN_VOLUME_USDT", "30000000"))
PUMP_THRESHOLD_PCT = float(os.getenv("PUMP_THRESHOLD_PCT", "2.0"))
REPORT_INTERVAL_MINUTES = int(os.getenv("REPORT_INTERVAL_MINUTES", "5"))
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "10"))
HTTP_PROXY = os.getenv("HTTP_PROXY", "")
HTTPS_PROXY = os.getenv("HTTPS_PROXY", "")
TELEGRAM_PROXY = os.getenv('TELEGRAM_PROXY', '')
