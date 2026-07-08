import logging
import time
import requests

logger = logging.getLogger(__name__)

BINANCE_EXCHANGE_INFO = "https://fapi.binance.com/fapi/v1/exchangeInfo"


class BinanceListingMonitor:
    """Monitor Binance USDT perpetual futures for new listings and delistings."""

    CHECK_INTERVAL = 300  # 5 minutes

    def __init__(self):
        self._known_symbols = set()
        self._initialized = False
        self._last_check = 0

    def _fetch_symbols(self):
        """Fetch all currently active USDT perpetual symbols from Binance."""
        try:
            resp = requests.get(BINANCE_EXCHANGE_INFO, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            symbols = set()
            for s in data.get("symbols", []):
                if (s.get("quoteAsset") == "USDT"
                        and s.get("contractType") == "PERPETUAL"
                        and s.get("status") == "TRADING"):
                    symbols.add(s["symbol"])
            return symbols
        except Exception as e:
            logger.error(f"Binance listing fetch error: {e}")
            return set()

    def check(self):
        """Check for new listings and delistings.
        Returns dict with 'new' and 'delisted' lists, or None if not ready."""
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