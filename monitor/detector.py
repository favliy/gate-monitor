import logging
import time
from collections import defaultdict
from datetime import datetime
from typing import Dict, List

logger = logging.getLogger(__name__)


class PumpDetector:
    """Detect pumps: symbols that rose > threshold_pct within 1 minute."""

    def __init__(self, threshold_pct: float = 2.0):
        self.threshold_pct = threshold_pct
        self._price_history: Dict[str, List[tuple]] = defaultdict(list)
        self._current_pumps: Dict[str, dict] = {}

    def update_prices(self, tickers: dict):
        now = time.time()
        for sym, info in tickers.items():
            price = info.get("price", 0)
            if price <= 0:
                continue
            self._price_history[sym].append((now, price))
            cutoff = now - 600
            self._price_history[sym] = [
                (ts, p) for ts, p in self._price_history[sym] if ts >= cutoff
            ]

    def check_pumps(self, tickers: dict) -> List[dict]:
        now = time.time()
        pumps = []
        for sym, info in tickers.items():
            current_price = info.get("price", 0)
            if current_price <= 0:
                continue
            history = self._price_history.get(sym, [])
            if not history:
                continue
            target_ts = now - 60
            price_1m_ago = None
            for ts, p in reversed(history):
                if ts <= target_ts:
                    price_1m_ago = p
                    break
            if price_1m_ago is None or price_1m_ago <= 0:
                continue
            pump_pct = ((current_price - price_1m_ago) / price_1m_ago) * 100
            if pump_pct >= self.threshold_pct:
                event = {
                    "symbol": sym,
                    "pump_pct": round(pump_pct, 2),
                    "current_price": current_price,
                    "price_1m_ago": round(price_1m_ago, 6),
                    "volume": info.get("volume", 0),
                    "timestamp": now,
                    "time_str": datetime.now().strftime("%H:%M:%S"),
                }
                pumps.append(event)
                self._current_pumps[sym] = event
        pumps.sort(key=lambda x: x["pump_pct"], reverse=True)
        return pumps

    def get_current_window_pumps(self) -> List[dict]:
        pumps = list(self._current_pumps.values())
        pumps.sort(key=lambda x: x["pump_pct"], reverse=True)
        return pumps

    def get_5min_change(self, symbol: str, window_start_ts: float) -> float:
        """Get price change % over the last 5 minutes for a symbol."""
        history = self._price_history.get(symbol, [])
        if len(history) < 2:
            return 0
        current_price = history[-1][1]
        price_5m_ago = None
        for ts, p in reversed(history):
            if ts <= window_start_ts:
                price_5m_ago = p
                break
        if price_5m_ago is None or price_5m_ago <= 0:
            return 0
        return round(((current_price - price_5m_ago) / price_5m_ago) * 100, 2)

    def check_5m_pumps(self, tickers: dict) -> list:
        """Detect 5-minute pumps."""
        now = time.time()
        pumps = []
        for sym, info in tickers.items():
            price = info.get("price", 0)
            if price <= 0:
                continue
            history = self._price_history.get(sym, [])
            if len(history) < 2:
                continue
            target = now - 300
            old_price = None
            for ts, p in reversed(history):
                if ts <= target:
                    old_price = p
                    break
            if old_price is None or old_price <= 0:
                continue
            pct = ((price - old_price) / old_price) * 100
            if pct >= 3.5:  # 5min threshold
                pumps.append({
                    "symbol": sym, "pct": round(pct, 2),
                    "price": price, "old_price": round(old_price, 6),
                    "volume": info.get("volume", 0),
                    "timestamp": now,
                })
        pumps.sort(key=lambda x: x["pct"], reverse=True)
        return pumps

    def reset_window(self):
        self._current_pumps.clear()


class DumpDetector:
    """Detect dumps: symbols that dropped > threshold_pct within 1 minute."""

    def __init__(self, threshold_pct: float = 2.0):
        self.threshold_pct = threshold_pct
        self._price_history: Dict[str, List[tuple]] = defaultdict(list)
        self._current_dumps: Dict[str, dict] = {}

    def update_prices(self, tickers: dict):
        now = time.time()
        for sym, info in tickers.items():
            price = info.get("price", 0)
            if price <= 0:
                continue
            self._price_history[sym].append((now, price))
            cutoff = now - 600
            self._price_history[sym] = [
                (ts, p) for ts, p in self._price_history[sym] if ts >= cutoff
            ]

    def check_dumps(self, tickers: dict) -> List[dict]:
        now = time.time()
        dumps = []
        for sym, info in tickers.items():
            current_price = info.get("price", 0)
            if current_price <= 0:
                continue
            history = self._price_history.get(sym, [])
            if not history:
                continue
            target_ts = now - 60
            price_1m_ago = None
            for ts, p in reversed(history):
                if ts <= target_ts:
                    price_1m_ago = p
                    break
            if price_1m_ago is None or price_1m_ago <= 0:
                continue
            drop_pct = ((current_price - price_1m_ago) / price_1m_ago) * 100
            if drop_pct <= -self.threshold_pct:
                event = {
                    "symbol": sym,
                    "drop_pct": round(drop_pct, 2),
                    "current_price": current_price,
                    "price_1m_ago": round(price_1m_ago, 6),
                    "volume": info.get("volume", 0),
                    "timestamp": now,
                    "time_str": datetime.now().strftime("%H:%M:%S"),
                }
                dumps.append(event)
                self._current_dumps[sym] = event
        dumps.sort(key=lambda x: x["drop_pct"])
        return dumps

    def get_current_window_dumps(self) -> List[dict]:
        dumps = list(self._current_dumps.values())
        dumps.sort(key=lambda x: x["drop_pct"])
        return dumps

    def check_5m_dumps(self, tickers: dict) -> list:
        """Detect 5-minute dumps."""
        now = time.time()
        dumps = []
        for sym, info in tickers.items():
            price = info.get("price", 0)
            if price <= 0:
                continue
            history = self._price_history.get(sym, [])
            if len(history) < 2:
                continue
            target = now - 300
            old_price = None
            for ts, p in reversed(history):
                if ts <= target:
                    old_price = p
                    break
            if old_price is None or old_price <= 0:
                continue
            pct = ((price - old_price) / old_price) * 100
            if pct <= -3.5:  # 5min threshold
                dumps.append({
                    "symbol": sym, "pct": round(pct, 2),
                    "price": price, "old_price": round(old_price, 6),
                    "volume": info.get("volume", 0),
                    "timestamp": now,
                })
        dumps.sort(key=lambda x: x["pct"])
        return dumps

    def reset_window(self):
        self._current_dumps.clear()


class OIDetector:
    """Detect Open Interest spikes (5-minute window)."""

    def __init__(self, oi_threshold_pct: float = 5.0):
        self.oi_threshold_pct = oi_threshold_pct
        self._oi_history: Dict[str, List[tuple]] = defaultdict(list)
        self._current_spikes: Dict[str, dict] = {}

    def update_oi(self, oi_data: Dict[str, float]):
        now = time.time()
        for sym, oi_value in oi_data.items():
            if oi_value <= 0:
                continue
            self._oi_history[sym].append((now, oi_value))
            cutoff = now - 900
            self._oi_history[sym] = [
                (ts, oi) for ts, oi in self._oi_history[sym] if ts >= cutoff
            ]

    def check_oi_spikes(self) -> List[dict]:
        now = time.time()
        spikes = []
        for sym, history in self._oi_history.items():
            if len(history) < 2:
                continue
            current_oi = history[-1][1]
            if current_oi <= 0:
                continue
            target_ts = now - 300
            oi_5m_ago = None
            for ts, oi in reversed(history):
                if ts <= target_ts:
                    oi_5m_ago = oi
                    break
            if oi_5m_ago is None or oi_5m_ago <= 0:
                continue
            oi_change_pct = ((current_oi - oi_5m_ago) / oi_5m_ago) * 100
            if oi_change_pct >= self.oi_threshold_pct:
                event = {
                    "symbol": sym,
                    "oi_change_pct": round(oi_change_pct, 2),
                    "current_oi": current_oi,
                    "oi_5m_ago": round(oi_5m_ago, 2),
                    "timestamp": now,
                    "time_str": datetime.now().strftime("%H:%M:%S"),
                }
                spikes.append(event)
                self._current_spikes[sym] = event
        spikes.sort(key=lambda x: x["oi_change_pct"], reverse=True)
        return spikes

    def get_current_window_spikes(self) -> List[dict]:
        spikes = list(self._current_spikes.values())
        spikes.sort(key=lambda x: x["oi_change_pct"], reverse=True)
        return spikes

    def reset_window(self):
        self._current_spikes.clear()
