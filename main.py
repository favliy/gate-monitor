"""CoinGlass-style volume/OI monitor.

Fetches coin data (CoinGlass Open API when a key is present, otherwise Binance
free data), applies the ladder threshold detector, and pushes alerts to
Telegram. Only coins listed on Binance are monitored.
"""
import io
import logging
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    CHECK_INTERVAL_SECONDS,
    VOLUME_THRESHOLDS,
    OI_THRESHOLDS,
    STATE_FILE,
    ALERT_STATE_RESET_DAILY,
    PORT,
    DRY_RUN,
)
from monitor.reporter import format_alert
from monitor.sources import build_source
from monitor.state import AlertState
from monitor.telegram_sender import TelegramSender
from monitor.threshold_monitor import ThresholdMonitor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/monitor.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, fmt, *args):
        pass


def start_health_server():
    try:
        server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
        logger.info("Health server on port %d", PORT)
        server.serve_forever()
    except Exception as e:
        logger.warning("Health server failed: %s", e)


class MonitorApp:
    def __init__(self):
        self.telegram = TelegramSender(
            bot_token=TELEGRAM_BOT_TOKEN,
            chat_id=TELEGRAM_CHAT_ID,
        )
        self.source = build_source()
        self.state = AlertState(STATE_FILE, reset_daily=ALERT_STATE_RESET_DAILY)
        self.monitor = ThresholdMonitor(
            self.state,
            volume_thresholds=VOLUME_THRESHOLDS,
            oi_thresholds=OI_THRESHOLDS,
        )
        self._running = True
        self._cycles = 0
        self._total_alerts = 0

    def _send(self, text):
        if not text:
            return
        if DRY_RUN:
            logger.info("[DRY_RUN] %s", text.replace("\n", " | "))
            return
        if self.telegram.enabled:
            try:
                self.telegram.send_message_sync(text)
            except Exception as e:
                logger.error("Telegram send failed: %s", e)

    def _test_telegram(self):
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                new_loop = asyncio.new_event_loop()
                result = new_loop.run_until_complete(self.telegram.test_connection())
                new_loop.close()
                return result
            return loop.run_until_complete(self.telegram.test_connection())
        except RuntimeError:
            loop = asyncio.new_event_loop()
            result = loop.run_until_complete(self.telegram.test_connection())
            loop.close()
            return result

    def run(self):
        logger.info("=" * 60)
        logger.info("CoinGlass-style Monitor")
        logger.info("Source: %s", type(self.source).__name__)
        logger.info("Volume thresholds: %s", VOLUME_THRESHOLDS)
        logger.info("OI thresholds: %s", OI_THRESHOLDS)
        logger.info("Check interval: %ss", CHECK_INTERVAL_SECONDS)
        logger.info("=" * 60)

        if self.telegram.enabled and not DRY_RUN:
            if self._test_telegram():
                logger.info("Telegram ready")
            else:
                logger.warning("Telegram bot not reachable; check token/proxy")

        while self._running:
            cycle_start = time.time()
            try:
                self.state.reset_if_day_changed()
                snapshots = self.source.fetch()
                if not snapshots:
                    logger.warning("No snapshots this cycle")
                else:
                    events = self.monitor.process(snapshots)
                    vol_alerts = sum(1 for e in events if e.kind == "VOLUME")
                    oi_alerts = sum(1 for e in events if e.kind == "OI")
                    for e in events:
                        self._send(format_alert(e))
                        logger.info(
                            "ALERT %s %s pct=%.1f%% threshold=%.0f%% level=%d",
                            e.kind, e.symbol, e.pct, e.threshold, e.level,
                        )
                    self._total_alerts += len(events)
                    self._cycles += 1
                    if self._cycles % 5 == 0:
                        logger.info(
                            "cycle=%d snapshots=%d volume_alerts=%d oi_alerts=%d total_alerts=%d",
                            self._cycles, len(snapshots), vol_alerts, oi_alerts,
                            self._total_alerts,
                        )
            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error("Loop error: %s", e, exc_info=True)

            elapsed = time.time() - cycle_start
            sleep_for = max(1.0, CHECK_INTERVAL_SECONDS - elapsed)
            end = time.time() + sleep_for
            while self._running and time.time() < end:
                time.sleep(0.5)

        self.shutdown()

    def shutdown(self):
        logger.info("Shutting down...")
        self._running = False
        try:
            self.source.stop()
        except Exception as e:
            logger.warning("source.stop: %s", e)
        logger.info("Monitor stopped.")


def main():
    threading.Thread(target=start_health_server, daemon=True).start()
    app = MonitorApp()
    signal.signal(signal.SIGINT, lambda s, f: app.shutdown() or sys.exit(0))
    signal.signal(signal.SIGTERM, lambda s, f: app.shutdown() or sys.exit(0))
    app.run()


if __name__ == "__main__":
    main()