import logging
import signal
import sys
import time
import io
import os
import threading
import requests

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
from monitor.binance_listing import BinanceListingMonitor

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
                f"alerts={len(app._last_alert)}",
                f"oi_spikes={len(app.oi_detector._current_spikes)}",
                f"errors={app.health_guard.total_errors}",
                f"tg_ok={app.telegram.enabled}",
                f"whitelist={len(app._binance_symbols)}",
                f"sent={app._sent_count}",
                f"filtered={app._filtered_count}",
                f"tg_fail={app._tg_fail_count}",
                f"tg_last_err={app._last_tg_err}",
                f"price_hist={sum(len(v) for v in app.pump_detector._price_history.values())}",
                f"hot_hist={sum(len(v) for v in app._hot_price_history.values())}",
                f"binance_init={app.binance_listing._initialized}",
                f"binance_symbols={len(app.binance_listing._known_symbols)}",
                f"binance_last={int(time.time() - app.binance_listing._last_check)}s_ago",
                f"binance_err={app.binance_listing._last_error}",
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
                        f"🛡 *健康* {h}h{m}m | 合约{self.last_ticker_count} | "
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
        self.binance_listing = BinanceListingMonitor()
        self.health_guard = HealthGuard(self)
        self._running = True
        self._window_start_ts = time.time()
        self._last_oi_fetch = 0
        self._last_whale = 0
        self._last_funding_report = 0
        self._last_alert = {}  # unified dedup
        self._last_oi_alert = {}
        self._price_snap = {}
        self._hot_price_history = {}  # prices for coins not in main monitoring
        self._hot_last_scan = 0
        # Binance contract whitelist (from local file) - used to gate notifications
        self._binance_symbols = self._load_binance_symbols()
        self._sent_count = 0
        self._filtered_count = 0
        self._tg_fail_count = 0
        self._last_tg_err = ""
        self._whitelist_warned = False

        
    def _load_binance_symbols(self) -> set:
        """Load Binance USDT perpetual symbols from local whitelist file (encoding-tolerant)."""
        syms = set()
        try:
            here = os.path.dirname(os.path.abspath(__file__))
            raw = open(os.path.join(here, "binance_usdt_perps.txt"), "rb").read()
            for enc in ("utf-8", "gb18030", "latin-1"):
                try:
                    text = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            else:
                text = raw.decode("utf-8", errors="replace")
            for line in text.splitlines():
                s = line.strip()
                if s and s.endswith("USDT"):
                    syms.add(s)
        except Exception as e:
            logger.warning(f"binance_usdt_perps.txt load failed: {e}")
        if not syms:
            logger.error("binance_usdt_perps.txt loaded 0 symbols - Binance filter disabled")
        logger.info(f"Loaded {len(syms)} Binance symbols from binance_usdt_perps.txt")
        return syms

    def _in_binance(self, symbol: str) -> bool:
        """Return True only if symbol maps to a Binance USDT perpetual."""
        if not symbol:
            return True  # non-symbol notifications (e.g. startup) allowed
        if not self._binance_symbols:
            if not getattr(self, "_whitelist_warned", False):
                self._whitelist_warned = True
                logger.error("Binance whitelist is EMPTY; falling back to notify-all")
            return True
        # normalize BTC_USDT -> BTCUSDT
        norm = symbol.replace("_USDT", "USDT")
        return norm in self._binance_symbols

    def _send(self, text, symbol=None):
        if self.telegram.enabled and text:
            if symbol is not None and not self._in_binance(symbol):
                self._filtered_count += 1
                return
            try:
                ok = self.telegram.send_message(text)
                if ok:
                    self._sent_count += 1
                    self.health_guard.feed_tg_ok()
                else:
                    self._tg_fail_count += 1
                    self.health_guard.feed_tg_fail()
            except Exception as e:
                self._tg_fail_count += 1
                self._last_tg_err = str(e)[:120]
                logger.error("TG: " + str(e))
                self.health_guard.feed_tg_fail()

    def _scan_funding_rates(self, tickers: dict) -> list:
        """Return list of {symbol, rate_pct} for |rate| > 0.3%."""
        extreme = []
        for sym, info in tickers.items():
            fr = float(info.get("funding_rate", 0) or 0)
            rate_pct = fr * 100
            if abs(rate_pct) > 0.3:
                extreme.append({"symbol": sym, "rate_pct": round(rate_pct, 4)})
        extreme.sort(key=lambda x: abs(x["rate_pct"]), reverse=True)
        return extreme

    # ── Hot-coin 1min scan (independent API, no volume filter) ──

    def _scan_hot_1min(self):
        """Fetch ALL Gate.io USDT futures tickers (no volume filter),
        detect 24h>=20% + 1min>=2%. Only notifies if the contract is on Binance.
        Symbols stored in local BTC_USDT form. Shared dedup with main alerts."""
        try:
            resp = requests.get(
                "https://api.gateio.ws/api/v4/futures/usdt/tickers",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=15,
            )
            resp.raise_for_status()
            now = time.time()
            found = 0
            for t in resp.json():
                contract = t.get("contract", "")
                if not contract.endswith("_USDT"):
                    continue
                chg = float(t.get("change_percentage", 0) or 0)
                if chg < 10:
                    continue
                found += 1
                price = float(t.get("last", 0) or 0)
                if price <= 0:
                    continue
                # Track price history
                if contract not in self._hot_price_history:
                    self._hot_price_history[contract] = []
                self._hot_price_history[contract].append((now, price))
                # Clean old entries (> 2min)
                cutoff = now - 120
                self._hot_price_history[contract] = [
                    (ts, p) for ts, p in self._hot_price_history[contract] if ts >= cutoff
                ]
                # Check 1min change
                hist = self._hot_price_history[contract]
                if len(hist) < 2:
                    continue
                target = now - 60
                old_price = None
                for ts, p in reversed(hist):
                    if ts <= target:
                        old_price = p
                        break
                # If no entry older than 60s, use oldest available
                if old_price is None:
                    old_price = hist[0][1]
                if old_price and old_price > 0:
                    pct = ((price - old_price) / old_price) * 100
                    if abs(pct) >= 1.5:
                        # Unified dedup
                        if now - self._last_alert.get(contract, 0) < 300:
                            continue
                        self._last_alert[contract] = now
                        direction = "拉升" if pct > 0 else "下跌"
                        self._send(f"🔥 *{contract} 24h+{chg:.0f}% 1min{direction}{abs(pct):.1f}% | {price}", symbol=contract)
                        logger.info(f"HOT_1M {contract} {pct:+.1f}% (24h+{chg:.0f}%)")
            if found > 0:
                logger.debug(f"Hot scan: {found} candidates with 24h>=20%")
        except Exception as e:
            logger.debug(f"Hot 1min scan error: {e}")


    # ── Main loop ──

    def run(self):
        logger.info("=" * 50)
        logger.info("  Gate.io Futures Monitor v3.4")
        logger.info("  1min>=2% | 5min>=3.5% | OI>=5% | Hot-coin | Funding | Binance Listing")
        logger.info("=" * 50)

        self.fetcher.start()
        self.health_guard.feed_data()
        logger.info("Telegram sender ready")

        self.health_guard.start()
        self._window_start_ts = time.time()
        self._price_snap = {}
        self._hot_price_history = {}
        self._hot_last_scan = 0
        
        logger.info("Monitoring started. [v3.4-binance]")

        self._send("✅ Monitor v3.4 启动 | 纯通知 | 涨幅榜已修复")

        while self._running:
            try:
                time.sleep(CHECK_INTERVAL_SECONDS)
                now = time.time()
                tickers = self.fetcher.get_all_tickers()
                if not tickers:
                    self.health_guard.feed_error()
                    continue

                self.health_guard.feed_data()
                # Keep-alive (ping this service's own public URL so Render does not idle-spin-down)
                try:
                    self_url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
                    if self_url:
                        requests.get(self_url + "/", timeout=5)
                except Exception:
                    pass

                if not self._price_snap:
                    for sym, info in tickers.items():
                        self._price_snap[sym] = info.get("price", 0)

                # ── 1min pump/dump ──
                self.pump_detector.update_prices(tickers)
                self.dump_detector.update_prices(tickers)
                pumps = self.pump_detector.check_pumps(tickers)
                dumps = self.dump_detector.check_dumps(tickers)

                for p in pumps:
                    sym = p["symbol"]
                    if now - self._last_alert.get(sym, 0) < DEDUP_SECONDS:
                        continue
                    self._last_alert[sym] = now
                    vol_m = p.get("volume", 0) / 1_000_000
                    msg = (
                        "📈 *" + sym + " 拉升 +" + str(round(p["pump_pct"], 1)) + "%*\n"
                        "💹 " + str(p["current_price"]) + " | 1min +" + str(round(p["pump_pct"], 1)) + "% | 量" + str(round(vol_m)) + "M"
                    )
                    self._send(msg, symbol=sym)

                for d in dumps:
                    sym = d["symbol"]
                    if now - self._last_alert.get(sym, 0) < DEDUP_SECONDS:
                        continue
                    self._last_alert[sym] = now
                    vol_m = d.get("volume", 0) / 1_000_000
                    msg = (
                        "📉 *" + sym + " 下跌 " + str(round(d["drop_pct"], 1)) + "%*\n"
                        "💹 " + str(d["current_price"]) + " | 1min " + str(round(d["drop_pct"], 1)) + "% | 量" + str(round(vol_m)) + "M"
                    )
                    self._send(msg, symbol=sym)

                # ── 5min pump/dump ──
                pumps_5m = self.pump_detector.check_5m_pumps(tickers)
                for p in pumps_5m:
                    sym = p["symbol"]
                    if now - self._last_alert.get(sym, 0) < DEDUP_5M:
                        continue
                    self._last_alert[sym] = now
                    vm = p.get("volume", 0) / 1_000_000
                    self._send("🔥 *" + sym + " 5m +" + str(round(p["pct"], 1)) + "% | " + str(p["price"]) + " | " + str(round(vm)) + "M", symbol=sym)
                    logger.info("PUMP5 " + sym + " +" + str(round(p["pct"], 2)) + "%")

                dumps_5m = self.dump_detector.check_5m_dumps(tickers)
                for d in dumps_5m:
                    sym = d["symbol"]
                    if now - self._last_alert.get(sym, 0) < DEDUP_5M:
                        continue
                    self._last_alert[sym] = now
                    vm = d.get("volume", 0) / 1_000_000
                    self._send("📉 *" + sym + " 5m " + str(round(d["pct"], 1)) + "% | " + str(d["price"]) + " | " + str(round(vm)) + "M", symbol=sym)
                    logger.info("DUMP5 " + sym + " " + str(round(d["pct"], 2)) + "%")

                # ── Hot-coin scan (independent, every 30s) ──
                if now - self._hot_last_scan >= 30:
                    self._scan_hot_1min()
                    self._hot_last_scan = now

                # ── Binance listing/delisting ──
                result = self.binance_listing.check()
                if result:
                    for sym in result.get("new", []):
                        base = sym.replace("USDT", "_USDT")
                        self._send("\U0001f195 *\u5e01\u5b89\u4e0a\u65b0* " + base + "\n\u5408\u7ea6 " + sym + " \u5df2\u4e0a\u7ebf\u5e01\u5b89\u6c38\u7eed\u5408\u7ea6", symbol=sym)
                        logger.info(f"BINANCE_NEW {sym}")
                    for sym in result.get("delisted", []):
                        base = sym.replace("USDT", "_USDT")
                        self._send("\U0001f53b *\u5e01\u5b89\u4e0b\u67b6* " + base + "\n\u5408\u7ea6 " + sym + " \u5df2\u4ece\u5e01\u5b89\u6c38\u7eed\u5408\u7ea6\u4e0b\u67b6", symbol=sym)
                        logger.info(f"BINANCE_DELIST {sym}")

                # ── 60s: OI ──
                if now - self._last_oi_fetch >= 60:
                    oi_data = self.fetcher.fetch_all_open_interest()
                    if oi_data:
                        self.oi_detector.update_oi(oi_data)
                        spikes = self.oi_detector.check_oi_spikes()
                        for s in spikes:
                            sym = s["symbol"]
                            if now - self._last_oi_alert.get(sym, 0) < OI_DEDUP:
                                continue
                            if s["current_oi"] < 2_000_000:
                                continue
                            self._last_oi_alert[sym] = now
                            self._send(
                                f"⚡ *{sym} OI异动*\n"
                                f"5min：{s['oi_change_pct']}%｜OI：{s['current_oi']/1e6:.1f}M", symbol=sym)
                            logger.info(f"OI {sym} +{round(s['oi_change_pct'],2)}%")
                    self._last_oi_fetch = now

                # ── 30min: Funding rate extremes ──
                if now - self._last_funding_report >= 1800:
                    extreme = self._scan_funding_rates(tickers)
                    if extreme:
                        lines = ["[费率异动] " + time.strftime("%H:%M")]
                        for e in extreme[:10]:
                            emoji = "[L]" if e["rate_pct"] > 0 else "[S]"
                            lines.append(f"{emoji} {e['symbol']} {e['rate_pct']:+.3f}%")
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
