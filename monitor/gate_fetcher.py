import logging
import os
import threading
import time
from typing import Dict, Optional, Callable, Set

import requests

logger = logging.getLogger(__name__)

BINANCE_SYMBOLS_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "binance_usdt_perps.txt")


class GateFuturesFetcher:
    """Fetch USDT perpetual futures from Gate.io, filtered to Binance-listed pairs."""

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
        self._binance_symbols: Set[str] = set()

    def _load_binance_symbols(self) -> Set[str]:
        symbols = set()
        if os.path.exists(BINANCE_SYMBOLS_FILE):
            try:
                with open(BINANCE_SYMBOLS_FILE, "r", encoding="utf-8") as f:
                    for line in f:
                        sym = line.strip()
                        if sym:
                            symbols.add(sym)
            except UnicodeDecodeError:
                with open(BINANCE_SYMBOLS_FILE, "r", encoding="gbk") as f:
                    for line in f:
                        sym = line.strip()
                        if sym:
                            symbols.add(sym)
            logger.info(f"Loaded {len(symbols)} Binance USDT perpetual symbols")
        else:
            logger.warning(f"Binance symbols file not found: {BINANCE_SYMBOLS_FILE}")
        return symbols

    def _gate_to_binance(self, gate_contract: str) -> str:
        return gate_contract.replace("_", "")

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
            binance_sym = self._gate_to_binance(contract)
            if binance_sym not in self._binance_symbols:
                continue
            volume = float(t.get("volume_24h_quote", 0))
            if contract in ('BTC_USDT', 'ETH_USDT', 'SOL_USDT'):
                continue
            if volume < 4500000:
                continue
            tickers[contract] = {
                "price": float(t.get("last", 0)),
                "volume": volume,
                "high": float(t.get("high_24h", 0)),
                "low": float(t.get("low_24h", 0)),
                "change_pct": float(t.get("change_percentage", 0)),
                "oi": 0,
            }
            matched += 1

        logger.info(f"{matched} Binance-listed contracts (vol >= 4.5M USDT)")
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
                updates[contract] = {"price": price, "volume": volume, "ts": now}
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

    def _poll_loop(self):
        while self._running:
            try:
                updates = self._fetch_prices()
                if updates:
                    with self._lock:
                        for sym, info in updates.items():
                            if sym in self._tickers:
                                self._tickers[sym]["price"] = info["price"]
                                self._tickers[sym]["volume"] = info.get(
                                    "volume", self._tickers[sym].get("volume", 0)
                                )
                    if self._on_update:
                        self._on_update(updates)
            except Exception as e:
                logger.error(f"Poll error: {e}")
            time.sleep(3)

    def start(self, on_update: Callable = None):
        self._on_update = on_update
        self._session = self._make_session()
        self._binance_symbols = self._load_binance_symbols()
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



