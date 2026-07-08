import logging
import time
import re
import requests

logger = logging.getLogger(__name__)

# Binance announcement API (futures catalog ID = 48)
BINANCE_ANNOUNCE = "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
# Also try exchangeInfo as fallback
BINANCE_FAPI = "https://fapi.binance.com/fapi/v1/exchangeInfo"
BINANCE_FSTREAM = "https://fstream.binance.com/fapi/v1/exchangeInfo"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}

# Match patterns like "Binance Futures Will List XXXUSDT Perpetual Contract"
LISTING_RE = re.compile(r'(?:Will List|Lists|Launches?)\s+(\w+USDT)\s+(?:Perpetual|USDⓈ-M)', re.IGNORECASE)
# Match patterns like "Binance Futures Will Delist XXXUSDT"
DELIST_RE = re.compile(r'(?:Will Delist|Delists?|Settles?)\s+(\w+USDT)', re.IGNORECASE)


class BinanceListingMonitor:
    """Monitor Binance USDT perpetual futures for new listings and delistings
    via announcements + API fallback."""

    CHECK_INTERVAL = 300

    def __init__(self):
        self._known_symbols = set()
        self._initialized = False
        self._last_check = 0
        self._last_error = ""
        self._last_announce_ids = set()

    def _fetch_announcements(self):
        """Fetch latest futures announcements from Binance."""
        params = {
            "type": "1",
            "catalogId": "48",  # Futures & Derivatives
            "pageNo": "1",
            "pageSize": "10",
        }
        try:
            resp = requests.get(BINANCE_ANNOUNCE, params=params, headers=HEADERS, timeout=20)
            if resp.status_code == 451:
                return None, "HTTP 451 blocked"
            resp.raise_for_status()
            data = resp.json()
            articles = data.get("data", {}).get("articles", []) or data.get("data", [])
            if not articles:
                articles = data.get("articles", [])
            results = []
            for a in articles:
                aid = a.get("id") or a.get("code")
                title = a.get("title", "")
                release_time = a.get("releaseTime") or a.get("publishTime", 0)
                results.append({"id": aid, "title": title, "time": release_time})
            return results, None
        except Exception as e:
            return None, str(e)[:80]

    def _fetch_exchange_info(self):
        """Fallback: fetch directly from Binance exchangeInfo."""
        for url in [BINANCE_FSTREAM, BINANCE_FAPI]:
            try:
                resp = requests.get(url, headers=HEADERS, timeout=20)
                if resp.status_code == 451:
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
                    logger.info(f"Binance exchangeInfo OK via {url}: {len(symbols)} symbols")
                    return symbols, None
            except Exception as e:
                continue
        return set(), "All exchangeInfo endpoints blocked"

    def _fetch_symbols(self):
        """Fetch current Binance USDT perpetual symbols via announcements or API."""
        # Try announcements first (website, less likely blocked)
        articles, err = self._fetch_announcements()
        if articles is not None:
            self._last_error = f"Announcements: {len(articles)} articles"
            logger.info(f"Binance announcements fetched: {len(articles)} articles")
            # Scan for new listings in recent announcements (24h)
            now_ms = int(time.time() * 1000)
            for a in articles:
                aid = a["id"]
                title = a["title"]
                t = a["time"]
                # Only process new (unseen) announcements from last 7 days
                if aid in self._last_announce_ids:
                    continue
                if now_ms - t > 7 * 86400 * 1000:
                    continue
                self._last_announce_ids.add(aid)
                # Check for listing
                m = LISTING_RE.search(title)
                if m:
                    sym = m.group(1)
                    self._known_symbols.add(sym)
                    logger.info(f"BINANCE LISTING FOUND: {sym} | {title}")
                # Check for delisting
                m = DELIST_RE.search(title)
                if m:
                    sym = m.group(1)
                    if sym in self._known_symbols:
                        self._known_symbols.discard(sym)
                        logger.info(f"BINANCE DELIST FOUND: {sym} | {title}")
            return self._known_symbols if self._known_symbols else set()

        # Fallback: exchangeInfo
        symbols, err2 = self._fetch_exchange_info()
        if symbols:
            self._last_error = "OK via exchangeInfo"
            return symbols

        self._last_error = err or err2 or "Unknown"
        return set()

    def check(self):
        """Check for new listings and delistings."""
        try:
            now = time.time()
            if now - self._last_check < self.CHECK_INTERVAL:
                return None

            prev = set(self._known_symbols)
            current = self._fetch_symbols()
            self._last_check = now

            if not current:
                return None

            if not self._initialized:
                self._known_symbols = current
                self._initialized = True
                self._last_error = f"OK ({len(current)} symbols)"
                logger.info(f"Binance listing monitor initialized: {len(current)} USDT perpetuals")
                return {"new": [], "delisted": []}

            new_listings = current - prev
            delistings = prev - current

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