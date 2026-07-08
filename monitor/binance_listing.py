import logging
import time
import requests

logger = logging.getLogger(__name__)

BINANCE_URLS = [
    "https://api.binance.com/fapi/v1/exchangeInfo",
    "https://api1.binance.com/fapi/v1/exchangeInfo",
    "https://api2.binance.com/fapi/v1/exchangeInfo",
    "https://fapi.binance.com/fapi/v1/exchangeInfo",
]

# Public CORS proxies as fallback
CORS_PROXIES = [
    "https://api.allorigins.win/raw?url={}",
    "https://corsproxy.io/?url={}",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}


class BinanceListingMonitor:
    """Monitor Binance USDT perpetual futures for new listings and delistings."""

    CHECK_INTERVAL = 300

    def __init__(self):
        self._known_symbols = set()
        self._initialized = False
        self._last_check = 0
        self._last_error = ""

    def _try_url(self, url, timeout=20):
        """Try fetching from a direct URL."""
        try:
            resp = requests.get(url, headers=HEADERS, timeout=timeout)
            if resp.status_code == 451:
                return None, "HTTP 451 blocked"
            resp.raise_for_status()
            return resp.json(), None
        except Exception as e:
            return None, str(e)[:80]

    def _try_cors_proxy(self, target_url):
        """Try fetching via public CORS proxy."""
        import urllib.parse
        encoded = urllib.parse.quote(target_url, safe='')
        for proxy_tpl in CORS_PROXIES:
            proxy_url = proxy_tpl.format(encoded)
            try:
                resp = requests.get(proxy_url, headers=HEADERS, timeout=30)
                resp.raise_for_status()
                data = resp.json()
                # allorigins wraps in {contents: "..."}
                if "contents" in data:
                    import json
                    data = json.loads(data["contents"])
                return data, None
            except Exception as e:
                logger.debug(f"CORS proxy {proxy_tpl[:30]}...: {e}")
                continue
        return None, "All CORS proxies failed"

    def _fetch_symbols(self):
        """Fetch all currently active USDT perpetual symbols from Binance."""
        # Try direct endpoints first
        for url in BINANCE_URLS:
            for attempt in range(1, 3):
                data, err = self._try_url(url)
                if data:
                    return self._parse_symbols(data, url)
                if "451" in (err or ""):
                    break  # 451 means blocked, try next URL
                if attempt < 2:
                    time.sleep(3)

        # Fallback: try CORS proxy
        logger.info("Direct Binance blocked, trying CORS proxy...")
        data, err = self._try_cors_proxy(BINANCE_URLS[0])
        if data:
            return self._parse_symbols(data, "CORS proxy")

        logger.error("Binance fetch FAILED: all methods exhausted")
        self._last_error = err or "Unknown error"
        return set()

    def _parse_symbols(self, data, source):
        """Parse Binance exchangeInfo response."""
        symbols = set()
        for s in data.get("symbols", []):
            if (s.get("quoteAsset") == "USDT"
                    and s.get("contractType") == "PERPETUAL"
                    and s.get("status") == "TRADING"):
                symbols.add(s["symbol"])
        if symbols:
            logger.info(f"Binance fetch OK via {source}: {len(symbols)} symbols")
        return symbols

    def check(self):
        """Check for new listings and delistings."""
        try:
            now = time.time()
            if now - self._last_check < self.CHECK_INTERVAL:
                return None

            current = self._fetch_symbols()
            self._last_check = now

            if not current:
                return None

            if not self._initialized:
                self._known_symbols = current
                self._initialized = True
                logger.info(f"Binance listing monitor initialized: {len(current)} USDT perpetuals")
                return {"new": [], "delisted": []}

            new_listings = current - self._known_symbols
            delistings = self._known_symbols - current

            if new_listings or delistings:
                self._known_symbols = current

            return {
                "new": sorted(new_listings),
                "delisted": sorted(delistings),
            }
        except Exception as e:
            logger.error(f"Binance check exception: {e}")
            self._last_error = str(e)[:100]
            self._last_check = time.time()
            return None