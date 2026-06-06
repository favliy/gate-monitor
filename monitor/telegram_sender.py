import asyncio
import logging
from typing import Optional

from telegram import Bot
from telegram.error import TelegramError

logger = logging.getLogger(__name__)


class TelegramSender:
    """Send reports to Telegram group via bot."""

    def __init__(self, bot_token: str, chat_id: str):
        from config import TELEGRAM_PROXY
        self._proxy = TELEGRAM_PROXY
        self._bot_token = bot_token
        self.chat_id = chat_id
        self._enabled = bool(bot_token and chat_id)
        self.bot = self._create_bot()

    def _create_bot(self):
        if self._proxy:
            try:
                from telegram.request import HTTPXRequest
                request = HTTPXRequest(proxy=self._proxy, connect_timeout=10, read_timeout=15)
                return Bot(token=self._bot_token, request=request)
            except Exception as e:
                logger.warning(f"Proxy init failed ({e}), trying direct connection")
        return Bot(token=self._bot_token)

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def send_message(self, text: str) -> bool:
        """Send a text message to the configured Telegram chat."""
        if not self._enabled:
            logger.warning("Telegram not configured (missing token or chat_id)")
            return False

        # Try with current bot first, fall back to direct connection
        for attempt in range(2):
            try:
                await self.bot.send_message(
                    chat_id=self.chat_id,
                    text=text,
                    parse_mode=None,
                    disable_web_page_preview=True,
                )
                logger.info(f"Report sent to Telegram chat {self.chat_id}")
                return True
            except Exception as e:
                if attempt == 0 and self._proxy:
                    logger.warning(f"Proxy send failed ({e}), retrying direct...")
                    try:
                        self.bot = Bot(token=self._bot_token)
                    except Exception:
                        pass
                else:
                    logger.error(f"Telegram send failed: {e}")
                    return False
        return False

    def send_message_sync(self, text: str) -> bool:
        """Synchronous wrapper for sending messages."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                new_loop = asyncio.new_event_loop()
                result = new_loop.run_until_complete(self.send_message(text))
                new_loop.close()
                return result
            return loop.run_until_complete(self.send_message(text))
        except RuntimeError:
            loop = asyncio.new_event_loop()
            result = loop.run_until_complete(self.send_message(text))
            loop.close()
            return result

    async def test_connection(self) -> bool:
        """Test the bot connection."""
        if not self._enabled:
            return False

        for attempt in range(2):
            try:
                me = await self.bot.get_me()
                logger.info(f"Telegram bot @{me.username} connected successfully")
                return True
            except Exception as e:
                if attempt == 0 and self._proxy:
                    logger.warning(f"Proxy test failed ({e}), retrying direct...")
                    try:
                        self.bot = Bot(token=self._bot_token)
                    except Exception:
                        pass
                else:
                    logger.error(f"Telegram bot connection failed: {e}")
                    return False
        return False
