import logging
import time
import re
import requests

logger = logging.getLogger(__name__)

BINANCE_ANNOUNCE = "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
BINANCE_FSTREAM = "https://fstream.binance.com/fapi/v1/exchangeInfo"
BINANCE_FAPI = "https://fapi.binance.com/fapi/v1/exchangeInfo"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}

LISTING_RE = re.compile(r'(?:Will List|Lists|Launches?)\s+(\w+USDT)\s+(?:Perpetual|USD)', re.IGNORECASE)
DELIST_RE = re.compile(r'(?:Will Delist|Delists?|Settles?|Closes?)\s+(\w+USDT)', re.IGNORECASE)


class BinanceListingMonitor:
    CHECK_INTERVAL = 300

    def __init__(self):
        self._known_symbols = set()
        self._initialized = False
        self._last_check = 0
        self._last_error = ""
        self._last_announce_ids = set()

    def _fetch_announcements(self):
        """Fetch latest futures announcements from Binance."""
        params = {"type": "1", "catalogId": "48", "pageNo": "1", "pageSize": "10"}
        try:
            resp = requests.get(BINANCE_ANNOUNCE, params=params, headers=HEADERS, timeout=20)
            if resp.status_code == 451:
                return None, "HTTP 451 blocked"
            resp.raise_for_status()
            raw = resp.json()
            # Handle various response formats
            if isinstance(raw, dict):
                data = raw.get("data")
                if isinstance(data, dict):
                    articles = data.get("articles") or data.get("catalogues") or []
                    if isinstance(articles, list):
                        return articles, None
                if isinstance(data, list):
                    return data, None
                articles = raw.get("articles") or raw.get("catalogues") or []
                if isinstance(articles, list):
                    return articles, None
            if isinstance(raw, list):
                return raw, None
            return None, f"Unexpected format: {type(raw).__name__}"
        except Exception as e:
            return None, str(e)[:80]

    def _parse_announcements(self, articles):
        """Parse articles for listing/delisting symbols."""
        new_syms = set()
        del_syms = set()
        now_ms = int(time.time() * 1000)
        for a in articles:
            if not isinstance(a, dict):
                continue
            aid = a.get("id") or a.get("code") or hash(a.get("title", ""))
            title = a.get("title", "")
            t = a.get("releaseTime") or a.get("releaseDate") or a.get("publishTime") or 0
            if not title:
                continue
            if aid in self._last_announce_ids:
                continue
            if now_ms - t > 7 * 86400 * 1000 and t > 0:
                continue
            self._last_announce_ids.add(aid)
            # Check listing
            m = LISTING_RE.search(title)
            if m:
                new_syms.add(m.group(1))
                logger.info(f"BINANCE LISTING: {m.group(1)} | {title}")
            # Check delisting
            m = DELIST_RE.search(title)
            if m:
                del_syms.add(m.group(1))
                logger.info(f"BINANCE DELIST: {m.group(1)} | {title}")
        return new_syms, del_syms

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
                    logger.info(f"Binance exchangeInfo OK: {len(symbols)} symbols")
                    return symbols, None
            except Exception as e:
                continue
        return set(), "All endpoints blocked"

    def _fetch_symbols(self):
        """Fetch current Binance USDT perpetual symbols."""
        # Try exchangeInfo first for full list
        symbols, err = self._fetch_exchange_info()
        if symbols:
            self._last_error = "OK (exchangeInfo)"
            return symbols, None, None

        # Fallback: use announcements only
        articles, err = self._fetch_announcements()
        if articles is not None:
            new_syms, del_syms = self._parse_announcements(articles)
            self._last_error = f"Announcements: {len(articles)} articles"
            return self._known_symbols, new_syms, del_syms

        self._last_error = err or "Unknown"
        return set(), None, None

    def check(self):
        """Check for new listings and delistings."""
        try:
            now = time.time()
            if now - self._last_check < self.CHECK_INTERVAL:
                return None

            current, new_from_ann, del_from_ann = self._fetch_symbols()
            self._last_check = now

            if not current and not new_from_ann and not del_from_ann:
                return None

            # Handle announcement-based detection
            if new_from_ann:
                for sym in new_from_ann:
                    self._known_symbols.add(sym)
            if del_from_ann:
                for sym in del_from_ann:
                    self._known_symbols.discard(sym)

            if not self._initialized:
                if current:
                    self._known_symbols = current
                self._initialized = True
                logger.info(f"Binance monitor initialized: {len(self._known_symbols)} symbols")
                return {"new": sorted(new_from_ann or []), "delisted": sorted(del_from_ann or [])}

            # Compute diff if we have full list
            new_listings = list(new_from_ann or [])
            delistings = list(del_from_ann or [])
            if current:
                new_listings += sorted(current - self._known_symbols)
                delistings += sorted(self._known_symbols - current)
                self._known_symbols = current

            return {
                "new": sorted(set(new_listings)),
                "delisted": sorted(set(delistings)),
            }
        except Exception as e:
            logger.error(f"Binance check exception: {e}")
            self._last_error = str(e)[:100]
            self._last_check = time.time()
            return None