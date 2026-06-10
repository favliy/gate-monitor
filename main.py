import logging
import signal
import sys
import time
import io
import os
import threading
import traceback
from http.server import HTTPServer, BaseHTTPRequestHandler
from collections import defaultdict
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    PUMP_THRESHOLD_PCT, REPORT_INTERVAL_MINUTES, CHECK_INTERVAL_SECONDS,
)
from monitor.gate_fetcher import GateFuturesFetcher
from monitor.detector import PumpDetector, DumpDetector, OIDetector
from monitor.reporter import format_report, format_console
from monitor.telegram_sender import TelegramSender
from monitor.trading_signal import TradingSignalEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/monitor.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

DEDUP_SECONDS = 300


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        pass


def start_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info(f"Health server on port {port}")
    server.serve_forever()


class HealthGuard:
    """Auto-detect and self-heal: stale data, dead API, broken Telegram."""

    def __init__(self, app):
        self.app = app
        self._running = False
        self._thread = None
        # Tracking
        self.last_data_ts = time.time()
        self.last_ticker_count = 0
        self.consecutive_stale = 0
        self.consecutive_tg_fails = 0
        self.fetcher_restarts = 0
        self.tg_reconnects = 0
        self.total_errors = 0
        self._last_health_report = 0
        # Thresholds
        self.STALE_DATA_SECONDS = 120
        self.MAX_STALE_CYCLES = 3
        self.CHECK_INTERVAL = 30

    def feed_data(self):
        self.last_data_ts = time.time()
        tickers = self.app.fetcher.get_all_tickers()
        count = len(tickers)
        if count > 0:
            self.last_ticker_count = count
            if self.consecutive_stale > 0:
                logger.info(f"[HealthGuard] Data restored ({count} tickers)")
            self.consecutive_stale = 0

    def feed_tg_fail(self):
        self.consecutive_tg_fails += 1

    def feed_tg_ok(self):
        self.consecutive_tg_fails = 0

    def feed_error(self):
        self.total_errors += 1

    def _restart_fetcher(self):
        logger.warning("[HealthGuard] Restarting Gate.io fetcher...")
        try:
            self.app.fetcher.stop()
            time.sleep(3)
            self.app.fetcher = GateFuturesFetcher()
            self.app.fetcher.start()
            self.fetcher_restarts += 1
            self.consecutive_stale = 0
            self.last_data_ts = time.time()
            logger.info(f"[HealthGuard] Fetcher restarted (total restarts: {self.fetcher_restarts})")
            return True
        except Exception as e:
            logger.error(f"[HealthGuard] Fetcher restart failed: {e}")
            return False

    def _reconnect_telegram(self):
        logger.warning("[HealthGuard] Reconnecting Telegram...")
        try:
            self.app.telegram = TelegramSender(
                bot_token=TELEGRAM_BOT_TOKEN,
                chat_id=TELEGRAM_CHAT_ID,
            )
            ok = self.app.test_connection_sync()
            if ok:
                self.tg_reconnects += 1
                self.consecutive_tg_fails = 0
                logger.info(f"[HealthGuard] Telegram reconnected (total: {self.tg_reconnects})")
                return True
            else:
                logger.error("[HealthGuard] Telegram reconnect failed")
                return False
        except Exception as e:
            logger.error(f"[HealthGuard] Telegram reconnect error: {e}")
            return False

    def _send_health_report(self):
        now = time.time()
        if now - self._last_health_report < 3600:
            return
        self._last_health_report = now
        uptime = int(now - self.app._window_start_ts)
        hours = uptime // 3600
        mins = (uptime % 3600) // 60
        lines = [
            "🩺 *系统健康报告*",
            f"运行时间：{hours}h {mins}m",
            f"监控合约：{self.last_ticker_count}",
            f"数据断流恢复：{self.fetcher_restarts}次",
            f"TG重连：{self.tg_reconnects}次",
            f"累计异常：{self.total_errors}次",
            f"状态：{'正常' if self.consecutive_stale == 0 else '⚠️异常'}",
        ]
        self.app._send_alert("\n".join(lines))

    def _check_loop(self):
        while self._running:
            try:
                time.sleep(self.CHECK_INTERVAL)
                now = time.time()

                # 1) Stale data check
                stale_seconds = now - self.last_data_ts
                if stale_seconds > self.STALE_DATA_SECONDS:
                    self.consecutive_stale += 1
                    logger.warning(
                        f"[HealthGuard] No data for {int(stale_seconds)}s "
                        f"(consecutive: {self.consecutive_stale}/{self.MAX_STALE_CYCLES})"
                    )
                    if self.consecutive_stale >= self.MAX_STALE_CYCLES:
                        logger.error("[HealthGuard] Data stream dead — restarting fetcher")
                        self._restart_fetcher()
                else:
                    self.consecutive_stale = 0

                # 2) Telegram health check
                if self.consecutive_tg_fails >= 5:
                    logger.warning(f"[HealthGuard] {self.consecutive_tg_fails} consecutive TG failures — reconnecting")
                    self._reconnect_telegram()

                # 3) Check ticker count anomaly (sudden drop > 50%)
                current_count = len(self.app.fetcher.get_all_tickers())
                if self.last_ticker_count > 0 and current_count > 0:
                    if current_count < self.last_ticker_count * 0.5:
                        logger.warning(
                            f"[HealthGuard] Ticker count dropped {self.last_ticker_count}→{current_count} — may need restart"
                        )

                # 4) Hourly health report
                self._send_health_report()

            except Exception as e:
                logger.error(f"[HealthGuard] Check error: {e}")

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._check_loop, daemon=True)
        self._thread.start()
        logger.info("[HealthGuard] Started — auto-heal active")

    def stop(self):
        self._running = False


class MonitorApp:
    def __init__(self):
        self.fetcher = GateFuturesFetcher()
        self.pump_detector = PumpDetector(threshold_pct=PUMP_THRESHOLD_PCT)
        self.dump_detector = DumpDetector(threshold_pct=PUMP_THRESHOLD_PCT)
        self.oi_detector = OIDetector()
        self.signal_engine = TradingSignalEngine()
        self.telegram = TelegramSender(
            bot_token=TELEGRAM_BOT_TOKEN,
            chat_id=TELEGRAM_CHAT_ID,
        )
        self.health_guard = HealthGuard(self)
        self._running = True
        self._window_start = datetime.now()
        self._window_start_ts = time.time()
        self._last_report_time = time.time()
        self._last_oi_fetch_time = 0
        self._last_pump_alert = {}
        self._last_dump_alert = {}
        self._pump_counts = defaultdict(int)
        self._dump_counts = defaultdict(int)
        self._oi_counts = defaultdict(int)
        self._price_snapshot = {}
        self._tg_ok = False

    def _send_alert(self, text):
        if self.telegram.enabled and text:
            try:
                ok = self.telegram.send_message_sync(text)
                if ok:
                    self.health_guard.feed_tg_ok()
                else:
                    self.health_guard.feed_tg_fail()
            except Exception as e:
                logger.error("Alert failed: " + str(e))
                self.health_guard.feed_tg_fail()

    def run(self):
        logger.info("=" * 50)
        logger.info("  Gate.io Futures Monitor")
        logger.info("  Pump/Dump >= " + str(PUMP_THRESHOLD_PCT) + "% 1min | OI >= 5% 5min")
        logger.info("  HealthGuard: auto-heal enabled")
        logger.info("=" * 50)

        self.fetcher.start()
        self.health_guard.feed_data()

        if self.telegram.enabled and self.test_connection_sync():
            logger.info("Telegram ready")
            self.health_guard.feed_tg_ok()

        self.health_guard.start()

        self._window_start = datetime.now()
        self._window_start_ts = time.time()
        self._last_report_time = time.time()
        self._price_snapshot = {}

        logger.info("Monitoring started.")

        while self._running:
            try:
                time.sleep(CHECK_INTERVAL_SECONDS)
                now = time.time()

                tickers = self.fetcher.get_all_tickers()
                if not tickers:
                    self.health_guard.feed_error()
                    continue

                self.health_guard.feed_data()

                if not self._price_snapshot:
                    for sym, info in tickers.items():
                        self._price_snapshot[sym] = info.get("price", 0)

                self.pump_detector.update_prices(tickers)
                self.dump_detector.update_prices(tickers)
                pumps = self.pump_detector.check_pumps(tickers)
                dumps = self.dump_detector.check_dumps(tickers)

                for p in pumps:
                    sym = p["symbol"]
                    self._pump_counts[sym] += 1
                    last_alert = self._last_pump_alert.get(sym, 0)
                    if now - last_alert < DEDUP_SECONDS:
                        continue
                    self._last_pump_alert[sym] = now
                    snap = self._price_snapshot.get(sym, p["current_price"])
                    chg_5m = round(((p["current_price"] - snap) / snap * 100), 1) if snap else 0
                    sig = self.signal_engine.analyze_long(
                        sym, p["current_price"], p["pump_pct"],
                        chg_5m, p.get("volume", 0)
                    )
                    alert = self._fmt_alert(sig)
                    self._send_alert(alert)
                    logger.info("PUMP: " + sym + " +" + str(round(p["pump_pct"], 2)) + "%")

                for d in dumps:
                    sym = d["symbol"]
                    self._dump_counts[sym] += 1
                    last_alert = self._last_dump_alert.get(sym, 0)
                    if now - last_alert < DEDUP_SECONDS:
                        continue
                    self._last_dump_alert[sym] = now
                    snap = self._price_snapshot.get(sym, d["current_price"])
                    chg_5m = round(((d["current_price"] - snap) / snap * 100), 1) if snap else 0
                    sig = self.signal_engine.analyze_short(
                        sym, d["current_price"], abs(d["drop_pct"]),
                        chg_5m, d.get("volume", 0)
                    )
                    alert = self._fmt_alert(sig)
                    self._send_alert(alert)
                    logger.info("DUMP: " + sym + " " + str(round(d["drop_pct"], 2)) + "%")

                if pumps or dumps:
                    format_console(pumps)

                if now - self._last_oi_fetch_time >= 60:
                    oi_data = self.fetcher.fetch_all_open_interest()
                    if oi_data:
                        self.oi_detector.update_oi(oi_data)
                        self.oi_detector.check_oi_spikes()
                    self._last_oi_fetch_time = now

                if now - self._last_report_time >= REPORT_INTERVAL_MINUTES * 60:
                    pumps_win = self.pump_detector.get_current_window_pumps()
                    dumps_win = self.dump_detector.get_current_window_dumps()
                    oi_win = self.oi_detector.get_current_window_spikes()

                    for p in pumps_win:
                        p["detect_count"] = self._pump_counts.get(p["symbol"], 0)
                        snap = self._price_snapshot.get(p["symbol"], p["current_price"])
                        chg = round(((p["current_price"] - snap) / snap * 100), 1) if snap else 0
                        p["change_5m"] = chg
                        p["signal"] = self.signal_engine.analyze_long(
                            p["symbol"], p["current_price"], p["pump_pct"],
                            chg, p.get("volume", 0)
                        )
                    for d in dumps_win:
                        d["detect_count"] = self._dump_counts.get(d["symbol"], 0)
                        snap = self._price_snapshot.get(d["symbol"], d["current_price"])
                        chg = round(((d["current_price"] - snap) / snap * 100), 1) if snap else 0
                        d["change_5m"] = chg
                        d["signal"] = self.signal_engine.analyze_short(
                            d["symbol"], d["current_price"], abs(d["drop_pct"]),
                            chg, d.get("volume", 0)
                        )
                    for s in oi_win:
                        s["detect_count"] = self._oi_counts.get(s["symbol"], 0)
                        curr = tickers.get(s["symbol"], {}).get("price", 0)
                        snap = self._price_snapshot.get(s["symbol"], curr)
                        s["change_5m"] = round(((curr - snap) / snap * 100), 1) if snap and curr else 0

                    report = format_report(
                        pumps_win, dumps_win, oi_win,
                        self._window_start, datetime.now(),
                    )

                    if report:
                        try:
                            print(report)
                        except Exception:
                            pass
                        print()
                        self._send_alert(report)

                    self._last_report_time = now
                    self._window_start = datetime.now()
                    self._window_start_ts = now
                    self._price_snapshot = {}
                    self.pump_detector.reset_window()
                    self.dump_detector.reset_window()
                    self.oi_detector.reset_window()
                    self._pump_counts.clear()
                    self._dump_counts.clear()
                    self._oi_counts.clear()

            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error("Loop: " + str(e), exc_info=True)
                self.health_guard.feed_error()
                time.sleep(5)

        self.shutdown()

    def test_connection_sync(self):
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

    def _fmt_alert(self, sig):
        d = sig["direction"]
        emoji = "📈" if d == "LONG" else "📉"
        status = "拉升" if d == "LONG" else "下跌"
        pos_map = {"HEAVY": "重仓", "MEDIUM": "中等", "LIGHT": "轻仓", "WATCH": "观望"}
        upside_map = {"HIGH": "高", "MEDIUM": "中", "NORMAL": "一般"}

        lines = [
            emoji + " *" + sig["symbol"] + "｜" + status + "信号*",
            "",
            "🔍 *检测原因*",
        ]
        for r in sig["reasons"]:
            lines.append("  • " + r)
        lines.append("")
        lines.append("📊 *数据*")
        lines.append("  现价：" + str(sig["price"]))
        lines.append("  1min：" + str(sig["pump_1m"]) + "% ｜ 5min：" + str(sig["change_5m"]) + "%")
        lines.append("  成交额：" + str(sig["vol_m"]) + "M USDT")
        if d == "LONG":
            lines.append("  支撑：" + str(sig["support"]) + " ｜ 压力：" + str(sig["resistance"]))
        else:
            lines.append("  压力：" + str(sig["support"]) + " ｜ 支撑：" + str(sig["resistance"]))
        lines.append("")
        if sig.get("can_enter"):
            lines.append("🎯 *交易计划*")
            lines.append("  入场：" + str(sig["pullback_entry"]))
            lines.append("  止损：" + str(sig["sl"]) + "（-" + str(sig["sl_pct"]) + "%）")
            lines.append("  止盈：")
            for i, tp in enumerate(sig["tp"]):
                lines.append("    TP" + str(i + 1) + " " + str(tp["price"]) + "（+" + str(tp["pct"]) + "%）平" + str(tp["share"]) + "%")
            lines.append("")
            lines.append("⚙️ *仓位建议* " + pos_map.get(sig["position"], "") + "（" + str(sig["size_pct"]) + "%）｜盈亏比 1:" + str(sig["rr"]))
            lines.append("🛡 *风控* 止损必执行 ｜ 分批止盈")
        else:
            lines.append("⏳ *状态* 趋势未确认，仅观察")
        lines.append("⭐ *评分* " + str(sig["score"]) + "/100")
        return "\n".join(lines)

    def shutdown(self):
        logger.info("Shutting down...")
        self._running = False
        self.health_guard.stop()
        self.fetcher.stop()
        logger.info("Monitor stopped.")


def main():
    threading.Thread(target=start_health_server, daemon=True).start()

    app = MonitorApp()
    signal.signal(signal.SIGINT, lambda s, f: app.shutdown() or sys.exit(0))
    signal.signal(signal.SIGTERM, lambda s, f: app.shutdown() or sys.exit(0))
    app.run()


if __name__ == "__main__":
    main()
