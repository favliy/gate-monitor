"""Ladder threshold detector.

For both metrics (daily volume increase, 1h OI change) an alert is emitted the
first time a threshold is crossed, and never again for the same threshold that
day. OI tracks up and down moves separately so a fresh +10% and a later -20%
each get their own alerts.
"""
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .state import AlertState

logger = logging.getLogger(__name__)


@dataclass
class AlertEvent:
    kind: str            # "VOLUME" or "OI"
    metric: str          # "volume" / "oi_up" / "oi_down"
    symbol: str
    threshold: float
    level: int           # 1-based threshold slot
    pct: float
    price: float
    volume_24h: float
    oi_value: float
    direction: Optional[str] = None   # for OI: "up"/"down"
    ts: float = 0.0
    time_str: str = ""


class ThresholdMonitor:
    def __init__(self, state: AlertState,
                 volume_thresholds: List[float],
                 oi_thresholds: List[float]):
        self.state = state
        self.volume_thresholds = sorted(v for v in volume_thresholds if v > 0)
        self.oi_thresholds = sorted(v for v in oi_thresholds if v > 0)

    def process(self, snapshots: Dict[str, object]) -> List[AlertEvent]:
        events: List[AlertEvent] = []
        now = time.time()
        for sym, snap in snapshots.items():
            if getattr(snap, "vol_change_pct", None) is not None:
                events += self._check(
                    "volume", "VOLUME", sym, snap.vol_change_pct,
                    self.volume_thresholds, snap, now, direction=None,
                )
            oi_pct = getattr(snap, "oi_change_1h_pct", None)
            if oi_pct is not None:
                direction = "up" if oi_pct > 0 else "down"
                metric = "oi_" + direction
                events += self._check(
                    metric, "OI", sym, abs(oi_pct),
                    self.oi_thresholds, snap, now, direction=direction,
                )
        events.sort(key=lambda e: (e.kind, e.pct), reverse=True)
        return events

    def _check(self, metric: str, kind: str, sym: str, pct: float,
               thresholds: List[float], snap, now: float,
               direction: Optional[str]) -> List[AlertEvent]:
        if pct is None or not thresholds:
            return []
        last = self.state.get_last_index(metric, sym)
        new_indexes = []
        for i, th in enumerate(thresholds):
            if i > last and pct >= th:
                new_indexes.append(i)
        if not new_indexes:
            return []
        new_last = max(new_indexes)
        # Bootstrap guard: the first time we see this symbol+metric today, report
        # only the highest reached threshold (one message) instead of firing every
        # ladder step retroactively. Prevents a flood when the monitor starts up
        # with coins already far above their baseline.
        if last < 0:
            new_indexes = [new_last]
        events = []
        ts = datetime.now(timezone.utc)
        time_str = datetime.now().strftime("%H:%M:%S")
        for i in new_indexes:
            events.append(AlertEvent(
                kind=kind,
                metric=metric,
                symbol=sym,
                threshold=thresholds[i],
                level=i + 1,
                pct=pct,
                price=getattr(snap, "price", 0.0) or 0.0,
                volume_24h=getattr(snap, "volume_24h", 0.0) or 0.0,
                oi_value=getattr(snap, "oi_value", 0.0) or 0.0,
                direction=direction,
                ts=now,
                time_str=time_str,
            ))
        self.state.set_last_index(metric, sym, new_last)
        logger.info("%s %s crossed %.0f%% (level %d) pct=%.1f%%",
                    kind, sym, thresholds[new_last], new_last + 1, pct)
        return events