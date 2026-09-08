"""Telegram sender with flood-control protection.

Sends alerts to a Telegram chat via the bot API. Consecutive messages are
spaced out (TELEGRAM_SEND_INTERVAL_SECONDS) and a 429 "Flood control" response
is retried after Telegram's Retry-After window instead of failing.
"""
import asyncio
import logging
import re
import threading
import time
from typing import Optional

from telegram import Bot
from telegram.error import TelegramError

logger = logging.getLogger(__name__)


class TelegramSender:
    """Send reports to a Telegram group via bot."""

    def __init__(self, bot_token: str, chat_id: str):
        from config import (TELEGRAM_PROXY, TELEGRAM_SEND_INTERVAL_SECONDS,
                            TELEGRAM_MAX_RETRIES)
        self._proxy = TELEGRAM_PROXY
        self._bot_token = bot_token
        self.chat_id = chat_id
        self._enabled = bool(bot_token and chat_id)
        self._send_interval = max(0.0, TELEGRAM_SEND_INTERVAL_SECONDS)
        self._max_retries = max(1, TELEGRAM_MAX_RETRIES)
        self._last_send = 0.0
        self._send_lock = threading.Lock()
        self.bot = self._create_bot()

    def _create_bot(self):
        if self._proxy:
            try:
                from telegram.request import HTTPXRequest
                request = HTTPXRequest(proxy=self._proxy, connect_timeout=10, read_timeout=20)
                return Bot(token=self._bot_token, request=request)
            except Exception as e:
                logger.warning("Proxy init failed (%s), trying direct connection", e)
        return Bot(token=self._bot_token)

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def send_message(self, text: str) -> bool:
        """Send a text message to the configured Telegram chat."""
        if not self._enabled:
            logger.warning("Telegram not configured (missing token or chat_id)")
            return False

        # Enforce a minimum spacing between consecutive sends.
        with self._send_lock:
            now = time.monotonic()
            delta = now - self._last_send
            if delta < self._send_interval:
                await asyncio.sleep(self._send_interval - delta)

        for attempt in range(self._max_retries):
            try:
                await self.bot.send_message(
                    chat_id=self.chat_id,
                    text=text,
                    parse_mode=None,
                    disable_web_page_preview=True,
                )
                with self._send_lock:
                    self._last_send = time.monotonic()
                logger.info("Report sent to Telegram chat %s", self.chat_id)
                return True
            except Exception as e:
                retry_after = self._retry_after(e)
                if retry_after is not None:
                    logger.warning(
                        "Telegram flood control (%s), waiting %.0fs",
                        type(e).__name__, retry_after,
                    )
                    await asyncio.sleep(min(retry_after, 60.0))
                    continue
                # Not flood control: if we are on the proxy bot, try direct once.
                if attempt == 0 and self._proxy:
                    logger.warning("Proxy send failed (%s), retrying direct...", e)
                    try:
                        self.bot = Bot(token=self._bot_token)
                    except Exception:
                        pass
                    continue
                logger.error("Telegram send failed: %s", e)
                return False
        logger.error("Telegram send failed after %d attempts", self._max_retries)
        return False

    @staticmethod
    def _retry_after(e: Exception) -> Optional[float]:
        """Return the Retry-After seconds if the error is Telegram flood control."""
        retry = getattr(e, "retry_after", None)
        if retry is not None:
            try:
                return float(retry)
            except (TypeError, ValueError):
                pass
        msg = str(e)
        m = re.search(r"Retry in (\d+)", msg, re.I)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
        if re.search(r"Flood|Too Many|429", msg, re.I):
            return 20.0
        return None

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
                logger.info("Telegram bot @%s connected successfully", me.username)
                return True
            except Exception as e:
                if attempt == 0 and self._proxy:
                    logger.warning("Proxy test failed (%s), retrying direct...", e)
                    try:
                        self.bot = Bot(token=self._bot_token)
                    except Exception:
                        pass
                else:
                    logger.error("Telegram bot connection failed: %s", e)
                    return False
        return False