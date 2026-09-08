"""Persistent alert state.

Tracks, per metric and per symbol, the highest threshold index already alerted
for the current UTC day. This is what makes each threshold fire exactly once
("70% one alert / 100% one alert / 200% one alert ...").
"""
import json
import logging
import os
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class AlertState:
    def __init__(self, path: str, reset_daily: bool = True):
        self.path = path
        self.reset_daily = reset_daily
        self._lock = threading.Lock()
        self._day = None
        self._data = {}  # metric -> {symbol: last_alerted_threshold_index}
        self._load()

    def _load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                self._day = raw.get("day")
                self._data = raw.get("data", {})
                if not isinstance(self._data, dict):
                    self._data = {}
        except Exception as e:
            logger.warning("Could not load alert state %s: %s", self.path, e)
            self._day = None
            self._data = {}

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _ensure_day(self):
        today = self._today()
        if self._day != today:
            self._day = today
            if self.reset_daily:
                self._data = {}

    def get_last_index(self, metric: str, symbol: str) -> int:
        with self._lock:
            self._ensure_day()
            return int(self._data.get(metric, {}).get(symbol, -1))

    def set_last_index(self, metric: str, symbol: str, index: int):
        with self._lock:
            self._ensure_day()
            self._data.setdefault(metric, {})[symbol] = int(index)
            self._save()

    def reset_if_day_changed(self):
        with self._lock:
            self._ensure_day()

    def _save(self):
        try:
            d = os.path.dirname(os.path.abspath(self.path))
            if d:
                os.makedirs(d, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(
                    {"day": self._day, "data": self._data},
                    f, ensure_ascii=False, indent=0,
                )
        except Exception as e:
            logger.warning("Could not save alert state: %s", e)