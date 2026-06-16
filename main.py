import logging
import signal
import sys
import time
import io
import os
import threading

from http.server import HTTPServer, BaseHTTPRequestHandler
from collections import defaultdict

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    PUMP_THRESHOLD_PCT, CHECK_INTERVAL_SECONDS,
)
from monitor.gate_fetcher import GateFuturesFetcher
from monitor.detector import PumpDetector, DumpDetector, OIDetector
from monitor.telegram_sender import TelegramSender
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

DEDUP_SECONDS = 300       # 1min alerts: 5min per symbol
DEDUP_5M = 300            # 5min alerts: 5min per symbol
OI_DEDUP = 600            # OI alerts: 10min per symbol
WHALE_INTERVAL = 300      # Whale batch: every 5min


class HealthHandler(BaseHTTPRequestHandler):
    app_ref = None
    def do_GET(self):
        if self.path == "/diag" and HealthHandler.app_ref:
            self.send_response(200)
            self.send_header("Content-type", "text/plain; charset=utf-8")
            self.end_headers()
            app = HealthHandler.app_ref
            ticks = app.fetcher.get_all_tickers()
            lines = [
                f"tickers={len(ticks)}",
                f"fetcher_running={app.fetcher._running}",
                f"pump_1m={len(app.pump_detector._current_pumps)}",
                f"dump_1m={len(app.dump_detector._current_dumps)}",
                f"last_pump={len(app._last_pump)}",
                f"last_dump={len(app._last_dump)}",
                f"oi_spikes={len(app.oi_detector._current_spikes)}",
                f"errors={app.health_guard.total_errors}",
                f"tg_ok={app.telegram.enabled}",
                f"price_hist={sum(len(v) for v in app.pump_detector._price_history.values())}",
            ]
            self.wfile.write("\n".join(lines).encode())
        else:
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK|ba972bc|diag")
    def log_message(self, *a):
        pass


def start_health_server():
    port = int(os.environ.get("PORT", 8080))
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()


class HealthGuard:
    def __init__(self, app):
        self.app = app; self._running = False
        self.last_data_ts = time.time(); self.last_ticker_count = 0
        self.consecutive_stale = 0; self.consecutive_tg_fails = 0
        self.fetcher_restarts = 0; self.tg_reconnects = 0
        self.total_errors = 0; self._last_report = 0
        self.STALE = 120; self.MAX_STALE = 3; self.IV = 30

    def feed_data(self):
        self.last_data_ts = time.time()
        n = len(self.app.fetcher.get_all_tickers())
        if n > 0: self.last_ticker_count = n; self.consecutive_stale = 0

    def feed_tg_fail(self): self.consecutive_tg_fails += 1
    def feed_tg_ok(self): self.consecutive_tg_fails = 0
    def feed_error(self): self.total_errors += 1

    def _restart_fetcher(self):
        try:
            self.app.fetcher.stop(); time.sleep(3)
            self.app.fetcher = GateFuturesFetcher()
            self.app.fetcher.start()
            self.fetcher_restarts += 1
            self.consecutive_stale = 0
            self.last_data_ts = time.time()
        except Exception as e:
            logger.error(f"[HG] restart: {e}")

    def _reconnect_tg(self):
        try:
            self.app.telegram = TelegramSender(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
            if self.app.test_connection_sync():
                self.tg_reconnects += 1; self.consecutive_tg_fails = 0
        except Exception as e:
            logger.error(f"[HG] tg: {e}")

    def _check_loop(self):
        while self._running:
            try:
                time.sleep(self.IV)
                now = time.time()
                if now - self.last_data_ts > self.STALE:
                    self.consecutive_stale += 1
                    if self.consecutive_stale >= self.MAX_STALE:
                        self._restart_fetcher()
                else:
                    self.consecutive_stale = 0
                if self.consecutive_tg_fails >= 5:
                    self._reconnect_tg()
                if now - self._last_report >= 3600:
                    self._last_report = now
                    uptime = int(now - self.app._window_start_ts)
                    h, m = uptime // 3600, (uptime % 3600) // 60
                    self.app._send(
                        f"🩺 *健康* {h}h{m}m | 合约{self.last_ticker_count} | "
                        f"重启{self.fetcher_restarts} | 异常{self.total_errors}")
            except Exception as e:
                logger.error(f"[HG] {e}")

    def start(self):
        self._running = True
        t = threading.Thread(target=self._check_loop, daemon=True)
        t.start()
    def stop(self): self._running = False


class MonitorApp:
    def __init__(self):
        self.fetcher = GateFuturesFetcher()
        self.pump_detector = PumpDetector(threshold_pct=PUMP_THRESHOLD_PCT)
        self.dump_detector = DumpDetector(threshold_pct=PUMP_THRESHOLD_PCT)
        self.oi_detector = OIDetector()
        self.telegram = TelegramSender(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        self.whale_monitor = WhaleMonitor()
        self.health_guard = HealthGuard(self)
        self._running = True
        self._window_start_ts = time.time()
        self._last_oi_fetch = 0
        self._last_whale = 0
        self._last_funding_report = 0
        self._last_pump = {}
        self._last_dump = {}
        self._last_5m_pump = {}
        self._last_5m_dump = {}
        self._last_oi_alert = {}
        self._price_snap = {}

    def _send(self, text):
        if self.telegram.enabled and text:
            try:
                ok = self.telegram.send_message(text)
                if ok: self.health_guard.feed_tg_ok()
                else: self.health_guard.feed_tg_fail()
            except Exception as e:
                logger.error("TG: " + str(e))
                self.health_guard.feed_tg_fail()

    # ── 1min pump/dump formatter ────────────────────────────────


    def _fmt_whale_batch(self, results):
        """Batch all whale signals into one clean message."""
        lines = []
        for f in results.get("funding", [])[:3]:
            lines.append(f"📊 {f['symbol']} 费{f['funding']:+.3f}%")
        for d in results.get("depth", [])[:3]:
            lines.append(f"📖 {d['symbol']} bid{d['bid_depth']:.0f} ask{d['ask_depth']:.0f}")
        for d in results.get("oi_div", [])[:3]:
            lines.append(f"👀 {d['symbol']} 价{d['price_chg']:+.1f}% OI{d['oi_chg']:+.1f}%")
        for sym, trades in [(t['symbol'], t) for t in results.get("large_trades", [])][:2]:
            lines.append(f"🐳 {sym} 大单")
        if not lines:
            return None

        return "🔍 *庄家监控 " + time.strftime("%H:%M") + "*\n" + "\n\n".join(lines)


    # Funding rate scan

    def _scan_funding_rates(self, tickers: dict) -> list:
        """Return list of {symbol, rate_pct} for |rate| > 0.5%."""
        extreme = []
        for sym, info in tickers.items():
            fr = float(info.get("funding_rate", 0) or 0)
            rate_pct = fr * 100
            if abs(rate_pct) > 0.3:
                extreme.append({"symbol": sym, "rate_pct": round(rate_pct, 4)})
        extreme.sort(key=lambda x: abs(x["rate_pct"]), reverse=True)
        return extreme
    # ── Main loop ───────────────────────────────────────────────

    def run(self):
        logger.info("=" * 50)
        logger.info("  Gate.io Futures Monitor + Whale Detector")
        logger.info("  1min >=2% | OI >=5% | Whale batch 5min")
        logger.info("=" * 50)

        self.fetcher.start()
        self.health_guard.feed_data()

        logger.info("Telegram sender ready")

        self.health_guard.start()
        self._window_start_ts = time.time()
        self._price_snap = {}

        logger.info("Monitoring started. [v3-clean]")

        # Verify clean environment
        import importlib.util as _iu
        bad = ["reporter", "trading_signal", "paper_trader"]
        found = [m for m in bad if _iu.find_spec("monitor." + m)]
        if found:
            self._send("⚠️ OLD MODULES FOUND: " + ", ".join(found))
        else:
            self._send("✅ Code is clean - no old modules")

        self._send("✅ Monitor v3.0 启动 | 纯通知 无报告")

        while self._running:
            try:
                time.sleep(CHECK_INTERVAL_SECONDS)
                now = time.time()
                tickers = self.fetcher.get_all_tickers()
                if not tickers:
                    self.health_guard.feed_error()
                    continue

                self.health_guard.feed_data()

                if not self._price_snap:
                    for sym, info in tickers.items():
                        self._price_snap[sym] = info.get("price", 0)

                # ═══ 1min pump/dump ═══
                self.pump_detector.update_prices(tickers)
                self.dump_detector.update_prices(tickers)
                pumps = self.pump_detector.check_pumps(tickers)
                dumps = self.dump_detector.check_dumps(tickers)

                for p in pumps:
                    sym = p["symbol"]
                    if now - self._last_pump.get(sym, 0) < DEDUP_SECONDS:
                        continue
                    self._last_pump[sym] = now
                    vol_m = p.get("volume", 0) / 1_000_000
                    msg = (
                        "📈 *" + sym + " 拉升 +" + str(round(p["pump_pct"], 1)) + "%*\n"
                        "📊 " + str(p["current_price"]) + " | 1min +" + str(round(p["pump_pct"], 1)) + "% | 量 " + str(round(vol_m)) + "M"
                    )
                    self._send(msg)

                for d in dumps:
                    sym = d["symbol"]
                    if now - self._last_dump.get(sym, 0) < DEDUP_SECONDS:
                        continue
                    self._last_dump[sym] = now
                    vol_m = d.get("volume", 0) / 1_000_000
                    msg = (
                        "📉 *" + sym + " 下跌 " + str(round(d["drop_pct"], 1)) + "%*\n"
                        "📊 " + str(d["current_price"]) + " | 1min " + str(round(d["drop_pct"], 1)) + "% | 量 " + str(round(vol_m)) + "M"
                    )
                    self._send(msg)


                # 5min pump/dump
                pumps_5m = self.pump_detector.check_5m_pumps(tickers)
                for p in pumps_5m:
                    sym = p["symbol"]
                    if now - self._last_5m_pump.get(sym, 0) < DEDUP_5M:
                        continue
                    self._last_5m_pump[sym] = now
                    vm = p.get("volume", 0) / 1_000_000
                    self._send(chr(0x1f525) + " *" + sym + " 5m +" + str(round(p["pct"], 1)) + "% | " + str(p["price"]) + " | " + str(round(vm)) + "M")
                    logger.info("PUMP5 " + sym + " +" + str(round(p["pct"], 2)) + "%")

                dumps_5m = self.dump_detector.check_5m_dumps(tickers)
                for d in dumps_5m:
                    sym = d["symbol"]
                    if now - self._last_5m_dump.get(sym, 0) < DEDUP_5M:
                        continue
                    self._last_5m_dump[sym] = now
                    vm = d.get("volume", 0) / 1_000_000
                    self._send(chr(0x1f4c9) + " *" + sym + " 5m " + str(round(d["pct"], 1)) + "% | " + str(d["price"]) + " | " + str(round(vm)) + "M")
                    logger.info("DUMP5 " + sym + " " + str(round(d["pct"], 2)) + "%")


                # ═══ 60s: OI ═══
                if now - self._last_oi_fetch >= 60:
                    oi_data = self.fetcher.fetch_all_open_interest()
                    if oi_data:
                        self.oi_detector.update_oi(oi_data)
                        spikes = self.oi_detector.check_oi_spikes()
                        for s in spikes:
                            sym = s["symbol"]
                            if now - self._last_oi_alert.get(sym, 0) < OI_DEDUP:
                                continue
                            # Only alert if OI > 2M USD (skip micro caps)
                            if s["current_oi"] < 2_000_000:
                                continue
                            self._last_oi_alert[sym] = now
                            self._send(
                                f"⚡ *{sym} OI异动*\n"
                                f"5min：+{s['oi_change_pct']}%｜OI：{s['current_oi']/1e6:.1f}M")
                            logger.info(f"OI {sym} +{round(s['oi_change_pct'],2)}%")
                    self._last_oi_fetch = now

                # Whale monitoring disabled

                # 30min: Funding rate extremes
                if now - self._last_funding_report >= 1800:
                    extreme = self._scan_funding_rates(tickers)
                    if extreme:
                        lines = ["[费率异动] " + time.strftime("%H:%M")]
                        for e in extreme[:10]:
                            emoji = "[L]" if e["rate_pct"] > 0 else "[S]"
                            lines.append(f"{emoji} {e["symbol"]} {e["rate_pct"]:+.3f}%")
                        self._send("\n".join(lines))
                        logger.info(f"Funding report: {len(extreme)} extreme rates")
                    self._last_funding_report = now

            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error("Loop: " + str(e), exc_info=True)
                self.health_guard.feed_error()
                time.sleep(5)

        self.shutdown()

    def test_connection_sync(self):
        try:
            return self.telegram.test_connection()
        except:
            return False

    def shutdown(self):
        self._running = False
        self.health_guard.stop()
        self.fetcher.stop()
        logger.info("Stopped.")


def main():
    threading.Thread(target=start_health_server, daemon=True).start()
    app = MonitorApp()
    HealthHandler.app_ref = app
    signal.signal(signal.SIGINT, lambda s, f: app.shutdown() or sys.exit(0))
    signal.signal(signal.SIGTERM, lambda s, f: app.shutdown() or sys.exit(0))
    app.run()

if __name__ == "__main__":
    main()
