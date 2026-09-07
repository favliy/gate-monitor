
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Optional, Callable

import requests
from requests.adapters import HTTPAdapter

logger = logging.getLogger(__name__)


def _to_binance(symbol: str) -> str:
    """'BTC_USDT' -> 'BTCUSDT'."""
    return symbol.replace("_USDT", "USDT")


def _to_local(symbol: str) -> str:
    """'BTCUSDT' -> 'BTC_USDT'."""
    return symbol.replace("USDT", "_USDT")


class BinanceFuturesFetcher:
    """Fetch USDT perpetual futures from Binance, filtered to Binance USDT contracts."""

    # Endpoints (fapi primary; fstream is NOT a REST host) 
    TICKERS_URLS = [
        "https://fapi.binance.com/fapi/v1/ticker/24hr",
    ]
    PREMIUM_URLS = [
        "https://fapi.binance.com/fapi/v1/premiumIndex",
    ]
    EXCHANGE_INFO_URLS = [
        "https://fapi.binance.com/fapi/v1/exchangeInfo",
    ]
    OI_URLS = [
        "https://fapi.binance.com/fapi/v1/openInterest",
    ]

    MIN_VOLUME = 4_500_000  # USDT 24h quote volume
    OI_CONCURRENCY = 10
    REFRESH_INTERVAL = 300  # refresh whitelist every 5 min

    def __init__(self):
        self._tickers: Dict[str, dict] = {}
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._on_update: Optional[Callable] = None
        self._whitelist_symbols: set = set()
        self._session: Optional[requests.Session] = None

    def _make_session(self) -> requests.Session:
        from config import HTTP_PROXY, HTTPS_PROXY
        session = requests.Session()
        # If no explicit proxy is configured, keep environment proxy support
        # (many setups reach exchanges only through a system proxy).
        session.trust_env = (not HTTP_PROXY and not HTTPS_PROXY)
        proxies = {}
        if HTTPS_PROXY:
            proxies["https"] = HTTPS_PROXY
            proxies["http"] = HTTPS_PROXY
        if HTTP_PROXY and not HTTPS_PROXY:
            proxies["http"] = HTTP_PROXY
        if proxies:
            session.proxies.update(proxies)
            logger.info(f"Using proxy: {proxies}")
        # Larger connection pool so concurrent OI fetches don't exhaust the pool
        adapter = HTTPAdapter(pool_connections=32, pool_maxsize=32)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
        })
        return session

    def _get_first(self, urls: list, timeout: int = 20, **kwargs) -> requests.Response:
        """Try each URL, returning first acceptable response. Logs per-endpoint status."""
        errors = []
        for url in urls:
            try:
                resp = self._session.get(url, timeout=timeout, **kwargs)
                if resp.status_code in (403, 451):  # banned / geo-restricted
                    logger.warning(f"Endpoint blocked {url} -> {resp.status_code}")
                    errors.append(f"{url} HTTP {resp.status_code}")
                    continue
                resp.raise_for_status()
                return resp
            except Exception as e:
                msg = f"{url} -> {type(e).__name__}: {str(e)[:120]}"
                logger.warning(f"Endpoint failed {msg}")
                errors.append(msg)
        if errors:
            raise RuntimeError("All Binance endpoints failed: " + "; ".join(errors))
        raise RuntimeError("All endpoints failed (no detail)")
    def _get_initial_tickers(self) -> dict:
        logger.info("Fetching tickers from Binance...")
        aliases = {"lastPrice": "price", "quoteVolume": "volume",
                   "highPrice": "high", "lowPrice": "low",
                   "priceChangePercent": "change_pct"}
        tickers = {}
        matched = 0

        # 24h tickers (price / volume / change)
        ticker_data = self._get_first(self.TICKERS_URLS).json()
        ticker_by_symbol = {t.get("symbol"): t for t in ticker_data}

        # funding rates indexed by symbol
        funding_data = self._get_first(self.PREMIUM_URLS).json()
        funding_by_symbol = {t.get("symbol"): t for t in funding_data}

        exchange_data = self._get_first(self.EXCHANGE_INFO_URLS).json()
        perp_symbols = set()
        for s in exchange_data.get("symbols", []):
            if (s.get("quoteAsset") == "USDT"
                    and s.get("contractType") == "PERPETUAL"
                    and s.get("status") == "TRADING"):
                perp_symbols.add(s["symbol"])

        for sym in sorted(perp_symbols):
            t = ticker_by_symbol.get(sym)
            if not t:
                continue
            volume = float(t.get("quoteVolume", 0) or 0)
            if volume < self.MIN_VOLUME:
                continue
            local_sym = _to_local(sym)
            tickers[local_sym] = {
                "price": float(t.get("lastPrice", 0) or 0),
                "volume": volume,
                "high": float(t.get("highPrice", 0) or 0),
                "low": float(t.get("lowPrice", 0) or 0),
                "change_pct": float(t.get("priceChangePercent", 0) or 0),
                "funding_rate": float(funding_by_symbol.get(sym, {}).get("lastFundingRate", 0) or 0),
                "oi": 0,
            }
            matched += 1

        logger.info(f"{matched} Binance USDT perp contracts (quoteVolume >= {self.MIN_VOLUME})")
        return tickers

    def _fetch_prices(self) -> dict:
        try:
            resp = self._get_first(self.TICKERS_URLS, timeout=15)
            data = resp.json()
            updates = {}
            now = time.time()
            for t in data:
                sym = t.get("symbol", "")
                local_sym = _to_local(sym)
                if local_sym not in self._whitelist_symbols:
                    continue
                price = float(t.get("lastPrice", 0) or 0)
                volume = float(t.get("quoteVolume", 0) or 0)
                if price <= 0:
                    continue
                updates[local_sym] = {
                    "price": price,
                    "volume": volume,
                    "change_pct": float(t.get("priceChangePercent", 0) or 0),
                    "ts": now,
                }
            return updates
        except Exception as e:
            logger.error(f"Failed to fetch prices: {e}")
            return {}

    def fetch_all_open_interest(self) -> Dict[str, float]:
        """Fetch open interest (in base-asset units) for monitored symbols."""
        results: Dict[str, float] = {}
        if not self._whitelist_symbols:
            return results

        def _one(local_sym: str):
            bin_sym = _to_binance(local_sym)
            try:
                resp = self._get_first(self.OI_URLS, timeout=10, params={"symbol": bin_sym})
                data = resp.json()
                oi = float(data.get("openInterest", 0) or 0)
                if oi > 0:
                    return local_sym, oi
            except Exception as e:
                logger.debug(f"OI {local_sym}: {e}")
            return None

        syms = list(self._whitelist_symbols)
        with ThreadPoolExecutor(max_workers=self.OI_CONCURRENCY) as ex:
            futs = [ex.submit(_one, s) for s in syms]
            for fut in as_completed(futs):
                r = fut.result()
                if r:
                    results[r[0]] = r[1]
        return results

    def _refresh_whitelist(self):
        try:
            resp = self._get_first(self.TICKERS_URLS, timeout=15)
            ticker_by_symbol = {t.get("symbol"): t for t in resp.json()}

            exchange_data = self._get_first(self.EXCHANGE_INFO_URLS, timeout=15).json()
            perp_symbols = set()
            for s in exchange_data.get("symbols", []):
                if (s.get("quoteAsset") == "USDT"
                        and s.get("contractType") == "PERPETUAL"
                        and s.get("status") == "TRADING"):
                    perp_symbols.add(s["symbol"])

            new_symbols = set()
            for sym in perp_symbols:
                t = ticker_by_symbol.get(sym)
                if not t:
                    continue
                if float(t.get("quoteVolume", 0) or 0) >= self.MIN_VOLUME:
                    new_symbols.add(_to_local(sym))

            added = new_symbols - self._whitelist_symbols
            removed = self._whitelist_symbols - new_symbols
            if added or removed:
                logger.info(f"Whitelist refresh: +{len(added)} / -{len(removed)}")
                with self._lock:
                    self._whitelist_symbols = new_symbols
                    for sym in added:
                        self._tickers[sym] = {
                            "price": 0, "volume": 0, "high": 0,
                            "low": 0, "change_pct": 0, "funding_rate": 0, "oi": 0,
                        }
                    for sym in removed:
                        self._tickers.pop(sym, None)
        except Exception as e:
            logger.debug(f"Whitelist refresh failed: {e}")

    def _poll_loop(self):
        last_refresh = 0
        while self._running:
            try:
                if time.time() - last_refresh > self.REFRESH_INTERVAL:
                    self._refresh_whitelist()
                    last_refresh = time.time()

                updates = self._fetch_prices()
                if updates:
                    with self._lock:
                        for sym, info in updates.items():
                            cur = self._tickers.get(sym)
                            if cur is None:
                                continue
                            cur["price"] = info["price"]
                            cur["volume"] = info.get("volume", cur.get("volume", 0))
                            cur["change_pct"] = info.get("change_pct", cur.get("change_pct", 0))
                    if self._on_update:
                        self._on_update(updates)
            except Exception as e:
                logger.error(f"Poll error: {e}")
            time.sleep(3)

    def start(self, on_update: Callable = None):
        self._on_update = on_update
        self._session = self._make_session()
        # Initial fetch with retry: Binance may be transiently blocked on cloud IPs.
        # Keep retrying with backoff instead of crashing the whole monitor.
        tickers = {}
        attempt = 0
        max_attempts = 30
        while attempt < max_attempts:
            attempt += 1
            try:
                tickers = self._get_initial_tickers()
                if tickers:
                    break
            except Exception as e:
                logger.warning(f"Init attempt {attempt}/{max_attempts} failed: {str(e)[:160]}")
            time.sleep(min(10 * attempt, 60))
        if not tickers:
            raise RuntimeError("Unable to initialize Binance data after retries")
        self._whitelist_symbols = set(tickers.keys())
        with self._lock:
            self._tickers = tickers
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        logger.info(f"BinanceFuturesFetcher started, monitoring {len(tickers)} contracts")
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
