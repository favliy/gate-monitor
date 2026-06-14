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
from monitor.whale_monitor import WhaleMonitor

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
    def __init__(self, app):
        self.app = app
        self._running = False
        self._thread = None
        self.last_data_ts = time.time()
        self.last_ticker_count = 0
        self.consecutive_stale = 0
        self.consecutive_tg_fails = 0
        self.fetcher_restarts = 0
        self.tg_reconnects = 0
        self.total_errors = 0
        self._last_health_report = 0
        self.STALE_DATA_SECONDS = 120
        self.MAX_STALE_CYCLES = 3
        self.CHECK_INTERVAL = 30

    def feed_data(self):
        self.last_data_ts = time.time()
        count = len(self.app.fetcher.get_all_tickers())
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
            logger.info(f"[HealthGuard] Fetcher restarted ({self.fetcher_restarts})")
            return True
        except Exception as e:
            logger.error(f"[HealthGuard] Fetcher restart failed: {e}")
            return False

    def _reconnect_telegram(self):
        logger.warning("[HealthGuard] Reconnecting Telegram...")
        try:
            self.app.telegram = TelegramSender(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
            if self.app.test_connection_sync():
                self.tg_reconnects += 1
                self.consecutive_tg_fails = 0
                logger.info(f"[HealthGuard] TG reconnected ({self.tg_reconnects})")
                return True
        except Exception as e:
            logger.error(f"[HealthGuard] TG reconnect error: {e}")
        return False

    def _send_health_report(self):
        now = time.time()
        if now - self._last_health_report < 3600:
            return
        self._last_health_report = now
        uptime = int(now - self.app._window_start_ts)
        h, m = uptime // 3600, (uptime % 3600) // 60
        self.app._send_alert(
            f"\U0001fa78 *系统健康报告*\n"
            f"运行：{h}h{m}m | 合约：{self.last_ticker_count}\n"
            f"重启：{self.fetcher_restarts} | TG重连：{self.tg_reconnects}\n"
            f"异常：{self.total_errors} | 状态：{'正常' if self.consecutive_stale == 0 else '异常'}"
        )

    def _check_loop(self):
        while self._running:
            try:
                time.sleep(self.CHECK_INTERVAL)
                now = time.time()
                if now - self.last_data_ts > self.STALE_DATA_SECONDS:
                    self.consecutive_stale += 1
                    if self.consecutive_stale >= self.MAX_STALE_CYCLES:
                        self._restart_fetcher()
                else:
                    self.consecutive_stale = 0
                if self.consecutive_tg_fails >= 5:
                    self._reconnect_telegram()
                self._send_health_report()
            except Exception as e:
                logger.error(f"[HealthGuard] {e}")

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._check_loop, daemon=True)
        self._thread.start()
        logger.info("[HealthGuard] Started")

    def stop(self):
        self._running = False


class MonitorApp:
    def __init__(self):
        self.fetcher = GateFuturesFetcher()
        self.pump_detector = PumpDetector(threshold_pct=PUMP_THRESHOLD_PCT)
        self.dump_detector = DumpDetector(threshold_pct=PUMP_THRESHOLD_PCT)
        self.oi_detector = OIDetector()
        self.signal_engine = TradingSignalEngine()
        self.telegram = TelegramSender(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        self.whale_monitor = WhaleMonitor()
        self.health_guard = HealthGuard(self)
        self._running = True
        self._window_start = datetime.now()
        self._window_start_ts = time.time()
        self._last_report_time = time.time()
        self._last_oi_fetch_time = 0
        self._last_whale_scan = 0
        self._last_pump_alert = {}
        self._last_dump_alert = {}
        self._pump_counts = defaultdict(int)
        self._dump_counts = defaultdict(int)
        self._oi_counts = defaultdict(int)
        self._price_snapshot = {}

    def _send_alert(self, text):
        if self.telegram.enabled and text:
            try:
                ok = self.telegram.send_message_sync(text)
                if ok: self.health_guard.feed_tg_ok()
                else: self.health_guard.feed_tg_fail()
            except Exception as e:
                logger.error("Alert: " + str(e))
                self.health_guard.feed_tg_fail()

    # ── Whale alert formatters ──────────────────────────────────

    def _fmt_funding_alert(self, f):
        return (
            f"\U0001f4ca *{f['symbol']} 资金费率异常*\n"
            f"费率：{f['funding']:+.4f}% | 多空比：{f['lsr']}\n"
            f"OI：{f['oi']/1e6:.1f}M USDT\n"
            f"\u26a0\ufe0f {f['warning']}"
        )

    def _fmt_depth_alert(self, d):
        return (
            f"\U0001f4d6 *{d['symbol']} 盘口异常*\n"
            f"买盘：{d['bid_depth']:.0f}U | 卖盘：{d['ask_depth']:.0f}U\n"
            f"\u26a0\ufe0f {d['warning']}"
        )

    def _fmt_oi_div_alert(self, d):
        return (
            f"\U0001f440 *{d['symbol']} OI背离*\n"
            f"价格变化：{d['price_chg']:+.1f}% | OI变化：{d['oi_chg']:+.1f}%\n"
            f"\U0001f6a8 {d['warning']}"
        )

    def _fmt_large_trade_alert(self, ts):
        trades_str = "\n".join([
            f"  {t['side']} {t['value']:.0f}U @ {t['price']} ({t['time']})"
            for t in ts
        ])
        return f"\U0001f433 *{ts[0]['symbol']} 大单活跃*\n{trades_str}"

    # ── Main loop ───────────────────────────────────────────────

    def run(self):
        logger.info("=" * 50)
        logger.info("  Gate.io Futures Monitor + Whale Detector")
        logger.info("  Pump/Dump >= " + str(PUMP_THRESHOLD_PCT) + "% 1min | OI >= 5% 5min")
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

                # ── 1min pump/dump ──
                self.pump_detector.update_prices(tickers)
                self.dump_detector.update_prices(tickers)
                pumps = self.pump_detector.check_pumps(tickers)
                dumps = self.dump_detector.check_dumps(tickers)

                for p in pumps:
                    sym = p["symbol"]
                    self._pump_counts[sym] += 1
                    if now - self._last_pump_alert.get(sym, 0) < DEDUP_SECONDS:
                        continue
                    self._last_pump_alert[sym] = now
                    snap = self._price_snapshot.get(sym, p["current_price"])
                    chg_5m = round(((p["current_price"] - snap) / snap * 100), 1) if snap else 0
                    sig = self.signal_engine.analyze_long(
                        sym, p["current_price"], p["pump_pct"],
                        chg_5m, p.get("volume", 0))
                    self._send_alert(self._fmt_alert(sig))
                    logger.info("PUMP: " + sym + " +" + str(round(p["pump_pct"], 2)) + "%")

                for d in dumps:
                    sym = d["symbol"]
                    self._dump_counts[sym] += 1
                    if now - self._last_dump_alert.get(sym, 0) < DEDUP_SECONDS:
                        continue
                    self._last_dump_alert[sym] = now
                    snap = self._price_snapshot.get(sym, d["current_price"])
                    chg_5m = round(((d["current_price"] - snap) / snap * 100), 1) if snap else 0
                    sig = self.signal_engine.analyze_short(
                        sym, d["current_price"], abs(d["drop_pct"]),
                        chg_5m, d.get("volume", 0))
                    self._send_alert(self._fmt_alert(sig))
                    logger.info("DUMP: " + sym + " " + str(round(d["drop_pct"], 2)) + "%")

                if pumps or dumps:
                    format_console(pumps)

                # ── 60s: OI + Whale scan ──
                if now - self._last_oi_fetch_time >= 60:
                    oi_data = self.fetcher.fetch_all_open_interest()
                    if oi_data:
                        self.oi_detector.update_oi(oi_data)
                        spikes = self.oi_detector.check_oi_spikes()
                        for s in spikes:
                            sym = s["symbol"]
                            self._oi_counts[sym] += 1
                            self._send_alert(
                                f"\u26a1 *{sym} OI异动*\n"
                                f"OI 5min：+{s['oi_change_pct']}%\n"
                                f"当前OI：{s['current_oi']:.0f}"
                            )
                            logger.info("OI: " + sym + " +" + str(round(s["oi_change_pct"], 2)) + "%")
                    self._last_oi_fetch_time = now

                # ── 120s: Whale scans ──
                if now - self._last_whale_scan >= 120:
                    whale_results = self.whale_monitor.scan(
                        tickers, self.oi_detector._oi_history)

                    for f in whale_results.get("funding", []):
                        self._send_alert(self._fmt_funding_alert(f))
                        logger.info(f"WHALE-FUNDING: {f['symbol']} {f['funding']}%")

                    for d in whale_results.get("depth", []):
                        self._send_alert(self._fmt_depth_alert(d))
                        logger.info(f"WHALE-DEPTH: {d['symbol']} {d['warning']}")

                    for d in whale_results.get("oi_div", []):
                        self._send_alert(self._fmt_oi_div_alert(d))
                        logger.info(f"WHALE-OI: {d['symbol']} {d['warning']}")

                    for ts_batch in [whale_results.get("large_trades", [])]:
                        if ts_batch:
                            # Group by symbol
                            by_sym = {}
                            for t in ts_batch:
                                by_sym.setdefault(t["symbol"], []).append(t)
                            for sym, trades in by_sym.items():
                                self._send_alert(self._fmt_large_trade_alert(trades))
                                logger.info(f"WHALE-TRADE: {sym} {len(trades)} large")

                    self._last_whale_scan = now

                # ── 5min report ──
                if now - self._last_report_time >= REPORT_INTERVAL_MINUTES * 60:
                    pumps_win = self.pump_detector.get_current_window_pumps()
                    dumps_win = self.dump_detector.get_current_window_dumps()
                    oi_win = self.oi_detector.get_current_window_spikes()

                    for p in pumps_win:
                        p["detect_count"] = self._pump_counts.get(p["symbol"], 0)
                        snap = self._price_snapshot.get(p["symbol"], p["current_price"])
                        chg = round(((p["current_price"] - snap)/snap*100), 1) if snap else 0
                        p["change_5m"] = chg
                        p["signal"] = self.signal_engine.analyze_long(
                            p["symbol"], p["current_price"], p["pump_pct"],
                            chg, p.get("volume", 0))
                    for d in dumps_win:
                        d["detect_count"] = self._dump_counts.get(d["symbol"], 0)
                        snap = self._price_snapshot.get(d["symbol"], d["current_price"])
                        chg = round(((d["current_price"] - snap)/snap*100), 1) if snap else 0
                        d["change_5m"] = chg
                        d["signal"] = self.signal_engine.analyze_short(
                            d["symbol"], d["current_price"], abs(d["drop_pct"]),
                            chg, d.get("volume", 0))
                    for s in oi_win:
                        s["detect_count"] = self._oi_counts.get(s["symbol"], 0)
                        curr = tickers.get(s["symbol"], {}).get("price", 0)
                        snap = self._price_snapshot.get(s["symbol"], curr)
                        s["change_5m"] = round(((curr-snap)/snap*100), 1) if snap and curr else 0

                    report = format_report(pumps_win, dumps_win, oi_win,
                                           self._window_start, datetime.now())
                    if report:
                        try: print(report)
                        except: pass
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
                r = new_loop.run_until_complete(self.telegram.test_connection())
                new_loop.close()
                return r
            return loop.run_until_complete(self.telegram.test_connection())
        except RuntimeError:
            loop = asyncio.new_event_loop()
            r = loop.run_until_complete(self.telegram.test_connection())
            loop.close()
            return r

    def _fmt_alert(self, sig):
        d = sig["direction"]
        emoji = "\U0001f4c8" if d == "LONG" else "\U0001f4c9"
        status = "拉升" if d == "LONG" else "下跌"
        pos_map = {"HEAVY": "重仓", "MEDIUM": "中等", "LIGHT": "轻仓", "WATCH": "观望"}

        lines = [
            emoji + " *" + sig["symbol"] + " | " + status + "信号*",
            "",
            "\U0001f50d *原因*",
        ]
        for r in sig["reasons"]:
            lines.append("  \u2022 " + r)
        lines.append("")
        lines.append("\U0001f4ca *数据*")
        lines.append("  现价：" + str(sig["price"]))
        lines.append("  1min：" + str(sig["pump_1m"]) + "% | 5min：" + str(sig["change_5m"]) + "%")
        lines.append("  成交额：" + str(sig["vol_m"]) + "M USDT")
        if d == "LONG":
            lines.append("  支撑：" + str(sig["support"]) + " | 压力：" + str(sig["resistance"]))
        else:
            lines.append("  压力：" + str(sig["support"]) + " | 支撑：" + str(sig["resistance"]))
        lines.append("")
        if sig.get("can_enter"):
            lines.append("\U0001f3af *交易计划*")
            lines.append("  入场：" + str(sig["pullback_entry"]))
            lines.append("  止损：" + str(sig["sl"]) + " (-" + str(sig["sl_pct"]) + "%)")
            tp_str = " | ".join([
                f"TP{i+1} {t['price']}(+{t['pct']}%)" for i, t in enumerate(sig["tp"])
            ])
            lines.append("  止盈：" + tp_str)
            lines.append("")
            lines.append("\u2699\ufe0f *仓位* " + pos_map.get(sig["position"], "") +
                         " (" + str(sig["size_pct"]) + "%) | RR 1:" + str(sig["rr"]))
            lines.append("\U0001f6e1 *风控* 止损必执行 | 分批止盈")
        else:
            lines.append("\u23f3 *状态* 趋势未确认，仅观察")
        lines.append("\u2b50 评分：" + str(sig["score"]) + "/100")
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
