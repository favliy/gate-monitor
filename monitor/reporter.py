import logging
from datetime import datetime
from typing import List, Optional

logger = logging.getLogger(__name__)


def format_report(pumps, dumps, oi_spikes, window_start, window_end):
    if not pumps and not dumps and not oi_spikes:
        return None

    ts = f"{window_start.strftime('%H:%M')}-{window_end.strftime('%H:%M')}"
    total = len(pumps) + len(dumps) + len(oi_spikes)
    pos_map = {"HEAVY": "重仓", "MEDIUM": "中等", "LIGHT": "轻仓", "WATCH": "观望"}

    lines = [f"Gate.io | {ts} | {total} signals"]
    lines.append("=" * 45)

    if pumps:
        lines.append("LONG")
        lines.append("-" * 45)
        for p in pumps[:5]:
            sym = p["symbol"]
            sig = p.get("signal", {})
            pos = pos_map.get(sig.get("position", ""), "")
            lines.append(
                f"{sym:<12} +{p['pump_pct']:>5.1f}%  5m {p.get('change_5m',0):>+5.1f}%  "
                f"{pos} {sig.get('size_pct','')}%  {sig.get('score','?')}分"
            )
            if sig:
                tp_str = "/".join(f"{t['price']}" for t in sig.get("tp", []))
                lines.append(f"  E:{sig.get('pullback_entry','?')}  SL:{sig.get('sl','?')}  TP:{tp_str}")

    if dumps:
        lines.append("")
        lines.append("SHORT")
        lines.append("-" * 45)
        for d in dumps[:5]:
            sym = d["symbol"]
            sig = d.get("signal", {})
            pos = pos_map.get(sig.get("position", ""), "")
            lines.append(
                f"{sym:<12} {d['drop_pct']:>5.1f}%  5m {d.get('change_5m',0):>+5.1f}%  "
                f"{pos} {sig.get('size_pct','')}%  {sig.get('score','?')}分"
            )
            if sig:
                tp_str = "/".join(f"{t['price']}" for t in sig.get("tp", []))
                lines.append(f"  E:{sig.get('pullback_entry','?')}  SL:{sig.get('sl','?')}  TP:{tp_str}")

    if oi_spikes:
        lines.append("")
        lines.append("OI")
        lines.append("-" * 45)
        for s in oi_spikes[:5]:
            sym = s["symbol"]
            lines.append(
                f"{sym:<12} OI +{s['oi_change_pct']:>5.1f}%  "
                f"5m {s.get('change_5m',0):>+5.1f}%  "
                f"oi {s['current_oi']/1e6:.1f}M  x{s.get('detect_count',1)}"
            )

    return "\n".join(lines)


def format_console(pumps):
    if not pumps:
        return
    for p in pumps:
        logger.info(f"PUMP: {p['symbol']:<16} +{p['pump_pct']:.2f}%")
