import logging
import requests

logger = logging.getLogger(__name__)


class TelegramSender:
    """Send reports to Telegram group via bot (sync HTTP)."""

    API = "https://api.telegram.org/bot{token}/{method}"

    def __init__(self, bot_token: str, chat_id: str):
        from config import TELEGRAM_PROXY
        self._token = bot_token
        self.chat_id = chat_id
        self._enabled = bool(bot_token and chat_id)
        self._session = requests.Session()
        if TELEGRAM_PROXY:
            self._session.proxies = {"https": TELEGRAM_PROXY, "http": TELEGRAM_PROXY}

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _call(self, method: str, data: dict) -> dict:
        url = self.API.format(token=self._token, method=method)
        try:
            resp = self._session.post(url, json=data, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            raise

    def send_message(self, text: str) -> bool:
        if not self._enabled:
            return False
        try:
            self._call("sendMessage", {
                "chat_id": self.chat_id,
                "text": text,
                "disable_web_page_preview": True,
            })
            logger.info(f"TG sent to {self.chat_id}")
            return True
        except Exception as e:
            logger.error(f"TG send failed: {e}")
            return False

    def test_connection(self) -> bool:
        if not self._enabled:
            return False
        try:
            me = self._call("getMe", {})
            logger.info(f"TG bot @{me['result']['username']} OK")
            return True
        except Exception as e:
            logger.error(f"TG connect failed: {e}")
            return False
