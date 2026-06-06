import logging
import signal
import sys
import time
import io
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from collections import defaultdict
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

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

    def _send_alert(self, text):
        if self.telegram.enabled and text:
            try:
                self.telegram.send_message_sync(text)
            except Exception as e:
                logger.error("Alert failed: " + str(e))

    def run(self):
        logger.info("=" * 50)
        logger.info("  Gate.io Futures Monitor")
        logger.info("  Pump/Dump >= " + str(PUMP_THRESHOLD_PCT) + "% 1min | OI >= 5% 5min")
        logger.info("=" * 50)

        self.fetcher.start()

        if self.telegram.enabled and self.test_connection_sync():
            logger.info("Telegram ready")

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
                    continue

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
                        chg = round(((p["current_price"] - snap)/snap*100),1) if snap else 0
                        p["change_5m"] = chg
                        p["signal"] = self.signal_engine.analyze_long(
                            p["symbol"], p["current_price"], p["pump_pct"],
                            chg, p.get("volume",0)
                        )
                    for d in dumps_win:
                        d["detect_count"] = self._dump_counts.get(d["symbol"], 0)
                        snap = self._price_snapshot.get(d["symbol"], d["current_price"])
                        chg = round(((d["current_price"] - snap)/snap*100),1) if snap else 0
                        d["change_5m"] = chg
                        d["signal"] = self.signal_engine.analyze_short(
                            d["symbol"], d["current_price"], abs(d["drop_pct"]),
                            chg, d.get("volume",0)
                        )
                    for s in oi_win:
                        s["detect_count"] = self._oi_counts.get(s["symbol"], 0)
                        curr = tickers.get(s["symbol"], {}).get("price", 0)
                        snap = self._price_snapshot.get(s["symbol"], curr)
                        s["change_5m"] = round(((curr-snap)/snap*100),1) if snap and curr else 0

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
                time.sleep(5)

        self.shutdown()

    def _fmt_alert(self, sig):
        d = sig["direction"]
        emoji = "LONG" if d == "LONG" else "SHORT"
        status = "PUMP" if d == "LONG" else "DUMP"
        pos_map = {"HEAVY": "重仓", "MEDIUM": "中等", "LIGHT": "轻仓", "WATCH": "观望"}
        upside_map = {"HIGH": "高", "MEDIUM": "中", "NORMAL": "一般"}

        lines = [
            emoji + " *" + sig["symbol"] + " | " + status + " 启动确认*",
            "",
            "*原因*",
        ]
        for r in sig["reasons"]:
            lines.append("  " + r)
        lines.append("")
        lines.append("*跟踪信息*")
        lines.append("现价：" + str(sig["price"]))
        lines.append("动能：" + str(sig["pump_1m"]) + "% 1min | " + str(sig["change_5m"]) + "% 5min")
        lines.append("量能：" + str(sig["vol_m"]) + "M USDT")
        lines.append("空间：" + upside_map.get(sig["upside"], "一般"))

        if d == "LONG":
            lines.append("支撑：" + str(sig["support"]))
            lines.append("压力：" + str(sig["resistance"]))
        else:
            lines.append("压力：" + str(sig["support"]))
            lines.append("支撑：" + str(sig["resistance"]))

        lines.append("")
        if sig.get("can_enter"):
            lines.append("入场：" + str(sig["pullback_entry"]))
            lines.append("止损：" + str(sig["sl"]) + " (" + str(sig["sl_pct"]) + "%)")
            lines.append("")
            lines.append("*分批止盈*")
            for i, tp in enumerate(sig["tp"]):
                lines.append("TP" + str(i+1) + "：" + str(tp["price"]) + " (+" + str(tp["pct"]) + "%) 平" + str(tp["share"]) + "%")
            lines.append("")
            lines.append("*风控*")
            lines.append("止损严格执行 | 分批止盈不贪")
            lines.append("")
            lines.append("*仓位* " + pos_map.get(sig["position"], "") + " (" + str(sig["size_pct"]) + "%) | RR 1:" + str(sig["rr"]))
        else:
            lines.append("*状态* 趋势未确认，仅观察不交易")

        lines.append("*评分* " + str(sig["score"]) + "/100")
        return "\n".join(lines)

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

    def shutdown(self):
        logger.info("Shutting down...")
        self._running = False
        self.fetcher.stop()
        logger.info("Monitor stopped.")


def main():
    # Start health check server for Render
    threading.Thread(target=start_health_server, daemon=True).start()

    app = MonitorApp()
    signal.signal(signal.SIGINT, lambda s, f: app.shutdown() or sys.exit(0))
    signal.signal(signal.SIGTERM, lambda s, f: app.shutdown() or sys.exit(0))
    app.run()


if __name__ == "__main__":
    main()
