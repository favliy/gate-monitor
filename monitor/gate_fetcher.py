import logging
import threading
import time
from typing import Dict, Optional, Callable

import requests

logger = logging.getLogger(__name__)

class GateFuturesFetcher:
    """Fetch USDT perpetual futures from Gate.io, all USDT perpetuals on Gate.io."""

    TICKERS_URL = "https://api.gateio.ws/api/v4/futures/usdt/tickers"
    CONTRACTS_URL = "https://api.gateio.ws/api/v4/futures/usdt/contracts"

    def __init__(self):
        self._tickers: Dict[str, dict] = {}
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._on_update: Optional[Callable] = None
        self._whitelist_symbols: Set[str] = set()
        self._session: Optional[requests.Session] = None


    def _make_session(self) -> requests.Session:
        from config import HTTP_PROXY, HTTPS_PROXY
        session = requests.Session()
        session.trust_env = False
        proxies = {}
        if HTTPS_PROXY:
            proxies["https"] = HTTPS_PROXY
            proxies["http"] = HTTPS_PROXY
        if HTTP_PROXY and not HTTPS_PROXY:
            proxies["http"] = HTTP_PROXY
        if proxies:
            session.proxies.update(proxies)
            logger.info(f"Using proxy: {proxies}")
        return session

    def _get_initial_tickers(self) -> dict:
        logger.info("Fetching tickers from Gate.io...")
        for attempt in range(1, 6):
            try:
                resp = self._session.get(self.TICKERS_URL, timeout=30)
                resp.raise_for_status()
                break
            except Exception as e:
                logger.warning(f"Attempt {attempt}/5 failed: {e}")
                if attempt == 5:
                    raise
                time.sleep(attempt * 3)

        data = resp.json()
        tickers = {}
        matched = 0
        for t in data:
            contract = t.get("contract", "")
            if not contract.endswith("_USDT"):
                continue
            volume = float(t.get("volume_24h_quote", 0))
            if volume < 4500000:
                continue
            tickers[contract] = {
                "price": float(t.get("last", 0)),
                "volume": volume,
                "high": float(t.get("high_24h", 0)),
                "low": float(t.get("low_24h", 0)),
                "change_pct": float(t.get("change_percentage", 0)),
                "funding_rate": float(t.get("funding_rate") or 0),
                "oi": 0,
            }
            matched += 1

        logger.info(f"{matched} Gate.io USDT contracts (vol >= 4.5M USDT)")
        return tickers

    def _fetch_prices(self) -> dict:
        try:
            resp = self._session.get(self.TICKERS_URL, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            updates = {}
            now = time.time()
            for t in data:
                contract = t.get("contract", "")
                if contract not in self._whitelist_symbols:
                    continue
                price = float(t.get("last", 0))
                volume = float(t.get("volume_24h_quote", 0))
                if price <= 0:
                    continue
                fr = float(t.get("funding_rate") or 0)
                updates[contract] = {"price": price, "volume": volume, "funding_rate": fr, "ts": now}
            return updates
        except Exception as e:
            logger.error(f"Failed to fetch prices: {e}")
            return {}

    def fetch_all_open_interest(self) -> Dict[str, float]:
        try:
            resp = self._session.get(self.CONTRACTS_URL, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            results = {}
            for c in data:
                name = c.get("name", "")
                if name not in self._whitelist_symbols:
                    continue
                pos_size = float(c.get("position_size", 0))
                if pos_size > 0:
                    results[name] = pos_size
            return results
        except Exception as e:
            logger.error(f"Failed to fetch OI: {e}")
            return {}

    def _refresh_whitelist(self):
        """Periodically refresh whitelist to include newly active contracts."""
        try:
            resp = self._session.get(self.TICKERS_URL, timeout=15)
            resp.raise_for_status()
            new_symbols = set()
            for t in resp.json():
                contract = t.get("contract", "")
                if not contract.endswith("_USDT"):
                    continue
                if float(t.get("volume_24h_quote", 0)) >= 4500000:
                    new_symbols.add(contract)
            added = new_symbols - self._whitelist_symbols
            if added:
                logger.info(f"Whitelist refresh: added {len(added)} new contracts")
                with self._lock:
                    self._whitelist_symbols |= added
                    for sym in added:
                        self._tickers[sym] = {
                            "price": 0, "volume": 0, "high": 0, "low": 0, "change_pct": 0, "oi": 0,
                        }
        except Exception as e:
            logger.debug(f"Whitelist refresh failed: {e}")

    def _poll_loop(self):
        last_refresh = 0
        while self._running:
            try:
                # Refresh whitelist every 5 minutes
                if time.time() - last_refresh > 300:
                    self._refresh_whitelist()
                    last_refresh = time.time()

                updates = self._fetch_prices()
                if updates:
                    with self._lock:
                        for sym, info in updates.items():
                            if sym in self._tickers:
                                self._tickers[sym]["price"] = info["price"]
                                self._tickers[sym]["volume"] = info.get("volume", self._tickers[sym].get("volume", 0))
                                if "funding_rate" in info:
                                    self._tickers[sym]["funding_rate"] = info["funding_rate"]
                    if self._on_update:
                        self._on_update(updates)
            except Exception as e:
                logger.error(f"Poll error: {e}")
            time.sleep(3)

    def start(self, on_update: Callable = None):
        self._on_update = on_update
        self._session = self._make_session()
        tickers = self._get_initial_tickers()
        self._whitelist_symbols = set(tickers.keys())
        with self._lock:
            self._tickers = tickers
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        logger.info(f"GateFuturesFetcher started, monitoring {len(tickers)} contracts")

    def stop(self):
        self._running = False
        if self._session:
            self._session.close()

    def get_all_tickers(self) -> dict:
        with self._lock:
            return dict(self._tickers)

    def get_ticker(self, symbol: str) -> Optional[dict]:
        with self._lock:
            return self._tickers.get(symbol)



