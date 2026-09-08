"""Data sources for the CoinGlass-style monitor.

Every source returns the same shape: {symbol -> CoinSnapshot}. The core monitor
only consumes the already-computed pct metrics (vol_change_pct, oi_change_1h_pct).

- BinanceSource: free, no key, Binance-only USDT perps (matches the "only coins
  listed on Binance" filter). This is the default live source.
- CoinglassSource: all-exchange aggregated data from the CoinGlass Open API.
  Requires a valid COINGLASS_API_KEY. Falls back gracefully if unavailable.
"""
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set

import requests

logger = logging.getLogger(__name__)

BINANCE_SYMBOLS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "binance_usdt_perps.txt"
)

# A valid Binance USDT perpetual symbol: uppercase alnum + "USDT".
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,30}USDT$")


def _read_binance_symbols() -> Set[str]:
    """Read the Binance whitelist file with encoding fallback and clean it.

    The file on disk may be GBK (Chinese Windows) and may contain junk lines;
    only lines that look like real USDT symbols are kept.
    """
    if not os.path.exists(BINANCE_SYMBOLS_FILE):
        logger.warning("Binance symbols file missing: %s", BINANCE_SYMBOLS_FILE)
        return set()
    with open(BINANCE_SYMBOLS_FILE, "rb") as f:
        raw = f.read()
    text = None
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("utf-8", errors="ignore")
    syms: Set[str] = set()
    for line in text.splitlines():
        s = line.strip().upper()
        if _SYMBOL_RE.fullmatch(s):
            syms.add(s)
    return syms




def _system_proxies() -> dict:
    """Determine an explicit http/https proxy map.

    Priority: explicit config proxy, then the OS/Windows system proxy.
    Env vars are deliberately ignored because load_dotenv may inject a
    TELEGRAM_PROXY / empty HTTP(S)_PROXY that breaks requests' own detection.
    """
    try:
        from config import HTTP_PROXY, HTTPS_PROXY
    except Exception:
        HTTP_PROXY = HTTPS_PROXY = ""
    p = HTTPS_PROXY or HTTP_PROXY
    if p:
        return {"http": p, "https": p}
    try:
        import winreg
        import urllib.parse as _up
        key = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            if winreg.QueryValueEx(k, "ProxyEnable")[0]:
                server = winreg.QueryValueEx(k, "ProxyServer")[0].strip()
                if server:
                    if ";" in server:
                        out = {}
                        for part in server.split(";"):
                            if "=" in part:
                                scheme_s, addr = part.split("=", 1)
                                if scheme_s.lower() in ("http", "https") and addr:
                                    out[scheme_s.lower()] = "http://" + addr
                        if out:
                            return out
                    else:
                        url = "http://" + server
                        return {"http": url, "https": url}
    except Exception:
        pass
    return {}
@dataclass
class CoinSnapshot:
    symbol: str
    price: float = 0.0
    volume_24h: float = 0.0
    vol_change_pct: Optional[float] = None
    oi_value: float = 0.0
    oi_change_1h_pct: Optional[float] = None
    ts: float = 0.0


class RateLimiter:
    """Simple global min-interval limiter shared by worker threads."""

    def __init__(self, min_interval: float):
        self.min_interval = max(0.0, min_interval)
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self):
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            delta = now - self._last
            if delta < self.min_interval:
                time.sleep(self.min_interval - delta)
            self._last = time.monotonic()


class _HttpClient:
    """Requests session with rate limiting and retries."""

    def __init__(self, base: str, limiter: Optional[RateLimiter] = None,
                 headers: Optional[dict] = None, proxies: Optional[dict] = None):
        self.base = base.rstrip("/")
        self.limiter = limiter
        self.session = requests.Session()
        # Disable env/system auto-proxy detection: load_dotenv pollutes the env
        # with e.g. TELEGRAM_PROXY / empty HTTP_PROXY which confuses requests'
        # proxy resolution. We build our own proxy map explicitly.
        self.session.trust_env = False
        proxies = proxies or _system_proxies()
        if proxies:
            self.session.proxies.update(proxies)
        if headers:
            self.session.headers.update(headers)

    def get_json(self, path: str, params: Optional[dict] = None,
                 timeout: float = 20.0, retries: int = 3):
        last_err = None
        for attempt in range(1, retries + 1):
            if self.limiter:
                self.limiter.wait()
            try:
                resp = self.session.get(
                    self.base + path, params=params, timeout=timeout
                )
                if resp.status_code == 429:
                    time.sleep(min(15, 2 ** attempt))
                    continue
                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                last_err = e
                if attempt < retries:
                    time.sleep(min(10, 1.5 * attempt))
        raise RuntimeError("GET %s failed: %s" % (path, last_err))


class BaseSource:
    def fetch(self) -> Dict[str, CoinSnapshot]:
        raise NotImplementedError


class BinanceSource(BaseSource):
    """Binance USDT perpetual source (free, no API key)."""

    TICKER_URL = "/fapi/v1/ticker/24hr"
    KLINE_URL = "/fapi/v1/klines"
    OI_HIST_URL = "/futures/data/openInterestHist"
    EXCHANGE_INFO_URL = "/fapi/v1/exchangeInfo"

    def __init__(self, min_volume_usdt: float = 10_000_000,
                 workers: int = 8, min_interval: float = 0.08,
                 symbol_refresh: int = 43200, proxies: Optional[dict] = None):
        from config import BINANCE_FAPI_BASE
        self.min_volume_usdt = min_volume_usdt
        self.workers = max(1, workers)
        self.symbol_refresh = symbol_refresh
        self.http = _HttpClient(BINANCE_FAPI_BASE, RateLimiter(min_interval), proxies=proxies)
        self._whitelist: Set[str] = set()
        self._last_refresh = 0.0
        self._lock = threading.Lock()

    def _load_file_symbols(self) -> Set[str]:
        syms = _read_binance_symbols()
        logger.info("Loaded %d cleaned Binance symbols", len(syms))
        return syms

    def _fetch_live_symbols(self) -> Set[str]:
        data = self.http.get_json(self.EXCHANGE_INFO_URL, timeout=30)
        syms: Set[str] = set()
        for item in data.get("symbols", []):
            if item.get("contractType") == "PERPETUAL" and item.get("quoteAsset") == "USDT" \
                    and item.get("status") == "TRADING":
                syms.add(item["symbol"])
        return syms

    def _refresh_symbols(self, force: bool = False):
        now = time.time()
        if not self._whitelist or force or (now - self._last_refresh) >= self.symbol_refresh:
            file_syms = self._load_file_symbols()
            live_syms: Set[str] = set()
            try:
                live_syms = self._fetch_live_symbols()
            except Exception as e:
                logger.warning("Failed to refresh Binance symbol list: %s", e)
            merged = file_syms | live_syms
            with self._lock:
                self._whitelist = merged
                self._last_refresh = now
            logger.info("Whitelist refreshed: %d Binance USDT perpetuals", len(merged))

    def fetch(self) -> Dict[str, CoinSnapshot]:
        self._refresh_symbols()
        tickers = self._fetch_tickers()
        if not tickers:
            return {}

        candidates = []
        for sym in sorted(self._whitelist):
            info = tickers.get(sym)
            if not info:
                continue
            if info["volume"] < self.min_volume_usdt:
                continue
            candidates.append(sym)

        logger.info("BinanceSource: scanning %d symbols", len(candidates))
        results: Dict[str, CoinSnapshot] = {}
        if not candidates:
            return results

        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {pool.submit(self._fetch_one, sym, tickers.get(sym)): sym for sym in candidates}
            for fut in as_completed(futures):
                sym = futures[fut]
                try:
                    snap = fut.result()
                    if snap is not None:
                        results[sym] = snap
                except Exception as e:
                    logger.warning("Binance fetch %s failed: %s", sym, e)
        return results

    def _fetch_tickers(self) -> Dict[str, dict]:
        data = self.http.get_json(self.TICKER_URL, timeout=30)
        out: Dict[str, dict] = {}
        for t in data:
            sym = t.get("symbol")
            if not sym:
                continue
            out[sym] = {
                "price": float(t.get("lastPrice", 0) or 0),
                "volume": float(t.get("quoteVolume", 0) or 0),
            }
        return out

    def _fetch_one(self, sym: str, info: dict) -> Optional[CoinSnapshot]:
        try:
            klines = self.http.get_json(self.KLINE_URL, params={
                "symbol": sym, "interval": "1h", "limit": 72
            }, timeout=25)
        except Exception as e:
            logger.warning("klines %s: %s", sym, e)
            klines = None

        try:
            oi_hist = self.http.get_json(self.OI_HIST_URL, params={
                "symbol": sym, "period": "5m", "limit": 14
            }, timeout=25)
        except Exception as e:
            logger.warning("oiHist %s: %s", sym, e)
            oi_hist = None

        vol_change = self._daily_volume_change(klines) if klines else None
        oi_curr, oi_change = self._oi_1h_change(oi_hist) if oi_hist else (None, None)

        return CoinSnapshot(
            symbol=sym,
            price=info.get("price", 0.0),
            volume_24h=info.get("volume", 0.0),
            vol_change_pct=vol_change,
            oi_value=float(oi_curr or 0.0),
            oi_change_1h_pct=oi_change,
            ts=time.time(),
        )

    @staticmethod
    def _daily_volume_change(klines) -> Optional[float]:
        """Day-over-day volume growth so far: today's completed hours vs
        yesterday's same completed hours, using hourly kline quote volume."""
        now = datetime.now(timezone.utc)
        today = now.date()
        yesterday = today - timedelta(days=1)
        cutoff_hour = now.hour
        vol_today = 0.0
        vol_yest = 0.0
        for k in klines or []:
            dt = datetime.fromtimestamp(int(k[0]) / 1000.0, tz=timezone.utc)
            qvol = float(k[7])
            if dt.date() == today and dt.hour < cutoff_hour:
                vol_today += qvol
            elif dt.date() == yesterday and dt.hour < cutoff_hour:
                vol_yest += qvol
        if vol_yest <= 0:
            return None
        return (vol_today - vol_yest) / vol_yest * 100.0

    @staticmethod
    def _oi_1h_change(oi_hist):
        """Return (current OI USD, 1h change %) from openInterestHist."""
        pts = []
        for p in oi_hist or []:
            ts = int(p.get("timestamp", 0))
            val = float(p.get("sumOpenInterestValue", 0) or 0)
            if val > 0:
                pts.append((ts, val))
        if len(pts) < 2:
            return None, None
        curr_ts, curr = pts[-1]
        target_ts = curr_ts - 3600_000
        ago = None
        for ts, v in reversed(pts):
            if ts <= target_ts:
                ago = v
                break
        if ago is None:
            ago = pts[0][1]
        if ago <= 0 or curr <= 0:
            return None, None
        return curr, (curr - ago) / ago * 100.0

    def stop(self):
        self.http.session.close()


class CoinglassSource(BaseSource):
    """CoinGlass Open API source (all-exchange aggregated data).

    Requires COINGLASS_API_KEY. Due to the strict 30 req/min rate limit, symbols
    are refreshed in rotating batches each cycle and OI/volume series are cached.
    """

    COIN_OI_URL = "/public/v2/futures/coin_oi"
    COIN_VOLUME_URL = "/public/v2/futures/coin_volume"
    COIN_LIST_URL = "/public/v2/futures/coin_list"

    def __init__(self, api_key: str, min_volume_usdt: float = 10_000_000,
                 batch_size: int = 20, proxies: Optional[dict] = None):
        from config import COINGLASS_BASE
        self.api_key = api_key
        self.min_volume_usdt = min_volume_usdt
        self.batch_size = max(1, batch_size)
        self.http = _HttpClient(
            COINGLASS_BASE, RateLimiter(0.5),
            headers={"CG-API-KEY": api_key, "accept": "application/json"},
            proxies=proxies,
        )
        self._symbols: List[str] = []
        self._cursor = 0
        self._cache: Dict[str, dict] = {}
        self._lock = threading.Lock()

    def _load_whitelist(self) -> Set[str]:
        syms: Set[str] = set()
        for s in _read_binance_symbols():
            base = s.replace("USDT", "")
            if base:
                syms.add(base)
        return syms

    def fetch(self) -> Dict[str, CoinSnapshot]:
        if not self.api_key:
            logger.warning("CoinGlass API key missing; CoinglassSource disabled")
            return {}

        try:
            bulk = self._try_bulk()
            if bulk:
                return bulk
        except Exception as e:
            logger.warning("CoinGlass bulk fetch failed: %s", e)

        if not self._symbols:
            self._symbols = sorted(self._load_whitelist())
        if not self._symbols:
            return {}

        batch = self._symbols[self._cursor:self._cursor + self.batch_size]
        self._cursor = (self._cursor + self.batch_size) % len(self._symbols)

        results: Dict[str, CoinSnapshot] = {}
        for sym in batch:
            try:
                snap = self._fetch_one(sym)
                if snap is not None:
                    results[sym] = snap
            except Exception as e:
                logger.warning("CoinGlass %s: %s", sym, e)
        self._merge_cached(results)
        return results

    def _merge_cached(self, results: Dict[str, CoinSnapshot]):
        """Surface previously computed metrics for symbols not in this batch so
        alerts are not delayed by a full rotation."""
        with self._lock:
            for sym in list(self._cache.keys()):
                if sym in results:
                    continue
                snap = self._build_snapshot(sym)
                if snap is not None:
                    results[sym] = snap

    def _try_bulk(self) -> Optional[Dict[str, CoinSnapshot]]:
        data = self.http.get_json(self.COIN_LIST_URL, timeout=20)
        items = _unwrap_list(data)
        if not items:
            return None
        out: Dict[str, CoinSnapshot] = {}
        for it in items:
            sym = it.get("symbol") or it.get("coin") or it.get("name")
            if not sym:
                continue
            sym = str(sym).upper().replace("USDT", "")
            out[sym] = CoinSnapshot(
                symbol=sym,
                price=_num(it, ("price", "lastPrice")) or 0.0,
                volume_24h=_num(it, ("volume24h", "volume_24h", "amount", "turnover")) or 0.0,
                vol_change_pct=_num(it, ("volumeChangePct", "volumeChange", "volumeChangePercent")),
                oi_value=_num(it, ("openInterest", "openInterestUsd", "oi")) or 0.0,
                oi_change_1h_pct=_num(it, ("oiChange1hPct", "openInterestChange", "oiChangePct")),
                ts=time.time(),
            )
        return out or None

    def _fetch_one(self, sym: str) -> Optional[CoinSnapshot]:
        try:
            oi_series = self.http.get_json(self.COIN_OI_URL, params={"symbol": sym}, timeout=20)
        except Exception as e:
            oi_series = None
            logger.debug("CoinGlass oi %s: %s", sym, e)
        try:
            vol_series = self.http.get_json(self.COIN_VOLUME_URL, params={"symbol": sym}, timeout=20)
        except Exception as e:
            vol_series = None
            logger.debug("CoinGlass volume %s: %s", sym, e)

        with self._lock:
            if oi_series is not None:
                self._cache.setdefault(sym, {})["oi"] = oi_series
            if vol_series is not None:
                self._cache.setdefault(sym, {})["volume"] = vol_series

        return self._build_snapshot(sym)

    def _build_snapshot(self, sym: str) -> Optional[CoinSnapshot]:
        with self._lock:
            entry = self._cache.get(sym, {})
            oi_series = entry.get("oi")
            vol_series = entry.get("volume")
        oi_curr, oi_change = self._oi_from_series(oi_series)
        vol_change, vol24 = self._volume_from_series(vol_series)
        return CoinSnapshot(
            symbol=sym,
            price=0.0,
            volume_24h=vol24 or 0.0,
            vol_change_pct=vol_change,
            oi_value=oi_curr or 0.0,
            oi_change_1h_pct=oi_change,
            ts=time.time(),
        )

    @staticmethod
    def _oi_from_series(series):
        if not series:
            return None, None
        pts = []
        for it in _unwrap_list(series):
            ts = int(it.get("time") or it.get("timestamp") or 0)
            val = _num(it, ("openInterest", "openInterestValue", "openInterestUsd", "sumOpenInterestValue"))
            if val is None:
                val = _num(it, ("amount", "value"))
            if val is not None and val > 0 and ts > 0:
                pts.append((ts, val))
        if len(pts) < 2:
            return None, None
        pts.sort(key=lambda x: x[0])
        curr_ts, curr = pts[-1]
        target_ts = curr_ts - 3600_000
        ago = None
        for ts, v in reversed(pts):
            if ts <= target_ts:
                ago = v
                break
        if ago is None:
            ago = pts[0][1]
        if ago <= 0 or curr <= 0:
            return curr, None
        return curr, (curr - ago) / ago * 100.0

    @staticmethod
    def _volume_from_series(series):
        if not series:
            return None, None
        now = datetime.now(timezone.utc)
        today = now.date()
        yesterday = today - timedelta(days=1)
        cutoff_hour = now.hour
        vol_today = 0.0
        vol_yest = 0.0
        for it in _unwrap_list(series):
            ts = int(it.get("time") or it.get("timestamp") or 0)
            amt = _num(it, ("amount", "volume", "quoteVolume", "turnover"))
            if amt is None or ts <= 0:
                continue
            dt = datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc)
            if dt.date() == today and dt.hour < cutoff_hour:
                vol_today += amt
            elif dt.date() == yesterday and dt.hour < cutoff_hour:
                vol_yest += amt
        if vol_yest <= 0:
            return None, vol_today
        change = (vol_today - vol_yest) / vol_yest * 100.0
        return change, vol_today

    def stop(self):
        self.http.session.close()


def _unwrap_list(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for k in ("list", "items", "result", "rows"):
                if isinstance(data.get(k), list):
                    return data[k]
    return []


def _num(item: dict, keys) -> Optional[float]:
    for k in keys:
        if k in item and item[k] is not None:
            try:
                return float(item[k])
            except (TypeError, ValueError):
                pass
    return None


def build_source() -> BaseSource:
    from config import (COINGLASS_API_KEY, DATA_SOURCE, MIN_VOLUME_USDT,
                        FETCH_WORKERS, FETCH_MIN_INTERVAL_SECONDS, SYMBOL_REFRESH_SECONDS)
    if DATA_SOURCE == "coinglass" and COINGLASS_API_KEY:
        logger.info("Using CoinGlass data source")
        return CoinglassSource(api_key=COINGLASS_API_KEY, min_volume_usdt=MIN_VOLUME_USDT)
    logger.info("Using Binance data source (free, Binance-only coins)")
    return BinanceSource(
        min_volume_usdt=MIN_VOLUME_USDT,
        workers=FETCH_WORKERS,
        min_interval=FETCH_MIN_INTERVAL_SECONDS,
        symbol_refresh=SYMBOL_REFRESH_SECONDS,
    )