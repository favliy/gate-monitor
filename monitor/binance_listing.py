import logging
import time
import requests

logger = logging.getLogger(__name__)

# Binance futures API endpoints (try different CDN edges)
BINANCE_URLS = [
    "https://fstream.binance.com/fapi/v1/exchangeInfo",
    "https://fapi.binance.com/fapi/v1/exchangeInfo",
    "https://api.binance.com/fapi/v1/exchangeInfo",
    "https://www.binance.com/fapi/v1/exchangeInfo",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}


class BinanceListingMonitor:
    """Monitor Binance USDT perpetual futures for new listings and delistings."""

    CHECK_INTERVAL = 300  # 5 minutes

    def __init__(self):
        self._known_symbols = set()
        self._initialized = False
        self._last_check = 0
        self._last_error = ""
        self._working_url = ""

    def _fetch_symbols(self):
        """Fetch all currently active USDT perpetual symbols from Binance."""
        for url in BINANCE_URLS:
            try:
                resp = requests.get(url, headers=HEADERS, timeout=20)
                if resp.status_code == 451:
                    logger.debug(f"Binance {url}: HTTP 451 blocked")
                    continue
                if resp.status_code == 403:
                    logger.debug(f"Binance {url}: HTTP 403 forbidden")
                    continue
                resp.raise_for_status()
                data = resp.json()
                symbols = set()
                for s in data.get("symbols", []):
                    if (s.get("quoteAsset") == "USDT"
                            and s.get("contractType") == "PERPETUAL"
                            and s.get("status") == "TRADING"):
                        symbols.add(s["symbol"])
                if symbols:
                    self._working_url = url
                    logger.info(f"Binance fetch OK via {url}: {len(symbols)} symbols")
                    return symbols
            except Exception as e:
                logger.debug(f"Binance {url}: {e}")
                continue

        logger.error("Binance fetch FAILED: all endpoints blocked")
        self._last_error = "All Binance endpoints blocked (451/403)"
        return set()

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
                self._last_error = f"OK via {self._working_url}"
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