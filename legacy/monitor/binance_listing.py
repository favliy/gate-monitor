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
    "Accept-Language": "en-US,en;q=0.9",
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
        """Fetch latest futures announcements trying multiple methods."""
        # Method 1: CMS article list
        for params in [
            {"type": 1, "catalogId": 48, "pageNo": 1, "pageSize": 10},
            {"catalogId": 48, "pageNo": 1, "pageSize": 10},
            {"pageNo": 1, "pageSize": 10},
        ]:
            try:
                resp = requests.get(BINANCE_ANNOUNCE, params=params, headers=HEADERS, timeout=20)
                if resp.status_code == 451:
                    continue
                resp.raise_for_status()
                raw = resp.json()
                # Log first 200 chars of raw response for debugging
                logger.info(f"Binance CMS raw keys: {list(raw.keys()) if isinstance(raw, dict) else type(raw).__name__}")
                if isinstance(raw, dict):
                    for key in ["data", "articles", "catalogs", "list", "rows"]:
                        val = raw.get(key)
                        if isinstance(val, list) and val:
                            logger.info(f"Found articles in key='{key}': {len(val)} items")
                            return val, None
                        if isinstance(val, dict):
                            for sub in ["articles", "catalogs", "list", "rows", "items"]:
                                subval = val.get(sub)
                                if isinstance(subval, list) and subval:
                                    logger.info(f"Found articles in data.{sub}: {len(subval)} items")
                                    return subval, None
                if isinstance(raw, list) and raw:
                    return raw, None
            except Exception as e:
                logger.debug(f"CMS params {params}: {e}")
                continue

        # Method 2: Try Binance Futures page HTML
        try:
            resp = requests.get(
                "https://www.binance.com/en/support/announcement/futures-48",
                headers=HEADERS,
                timeout=20,
            )
            if resp.status_code == 200:
                # Search for listing patterns in HTML
                text = resp.text[:5000]
                logger.info(f"Binance futures page HTML: {len(resp.text)} chars")
                return [], "HTML page fetched (no parsing)"
        except Exception as e:
            logger.debug(f"HTML page: {e}")

        return None, "All methods failed"

    def _parse_announcements(self, articles):
        new_syms, del_syms = set(), set()
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
            if t > 0 and now_ms - t > 7 * 86400 * 1000:
                continue
            self._last_announce_ids.add(aid)
            m = LISTING_RE.search(title)
            if m:
                new_syms.add(m.group(1))
                logger.info(f"BINANCE LISTING: {m.group(1)} | {title}")
            m = DELIST_RE.search(title)
            if m:
                del_syms.add(m.group(1))
                logger.info(f"BINANCE DELIST: {m.group(1)} | {title}")
        return new_syms, del_syms

    def _fetch_exchange_info(self):
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
        symbols, err = self._fetch_exchange_info()
        if symbols:
            self._last_error = "OK (exchangeInfo)"
            return symbols, None, None

        articles, err = self._fetch_announcements()
        if articles is not None:
            new_s, del_s = self._parse_announcements(articles)
            self._last_error = f"Ann: {len(articles)} arts"
            return self._known_symbols, new_s, del_s

        self._last_error = err or "Unknown"
        return set(), None, None

    def check(self):
        try:
            now = time.time()
            if now - self._last_check < self.CHECK_INTERVAL:
                return None
            current, new_ann, del_ann = self._fetch_symbols()
            self._last_check = now
            if not current and not new_ann and not del_ann:
                return None
            if new_ann:
                for sym in new_ann:
                    self._known_symbols.add(sym)
            if del_ann:
                for sym in del_ann:
                    self._known_symbols.discard(sym)
            if not self._initialized:
                if current:
                    self._known_symbols = current
                self._initialized = True
                logger.info(f"Binance init: {len(self._known_symbols)} symbols")
                return {"new": sorted(new_ann or []), "delisted": sorted(del_ann or [])}
            new_listings = list(new_ann or [])
            delistings = list(del_ann or [])
            if current:
                new_listings += sorted(current - self._known_symbols)
                delistings += sorted(self._known_symbols - current)
                self._known_symbols = current
            return {
                "new": sorted(set(new_listings)),
                "delisted": sorted(set(delistings)),
            }
        except Exception as e:
            logger.error(f"Binance check: {e}")
            self._last_error = str(e)[:100]
            self._last_check = time.time()
            return None