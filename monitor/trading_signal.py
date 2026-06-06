"""Trading signal engine - improved entry logic with confirmation."""


class TradingSignalEngine:

    TP_LEVELS = [
        (2.5, 40),
        (6.0, 35),
        (10.0, 25),
    ]

    def analyze_long(self, symbol, price, pump_1m, change_5m, volume):
        vol_m = volume / 1e6

        # Trend filter: must align
        trend_ok = change_5m > 0

        # Volume filter
        vol_ok = vol_m >= 0.5

        # Score
        score = 30
        if pump_1m >= 5:
            score += 30
        elif pump_1m >= 3:
            score += 20
        elif pump_1m >= 2:
            score += 10
        if trend_ok:
            score += 20
        if vol_m >= 50:
            score += 20
        elif vol_m >= 10:
            score += 12
        elif vol_m >= 1:
            score += 6
        score = min(100, score)

        # Only suggest entry if score >= 50 AND trend ok
        if score < 50 or not trend_ok:
            position = "WATCH"
            size_pct = 0
        elif score >= 80:
            position = "HEAVY"
            size_pct = 20
        elif score >= 65:
            position = "MEDIUM"
            size_pct = 12
        elif score >= 50:
            position = "LIGHT"
            size_pct = 6
        else:
            position = "WATCH"
            size_pct = 0

        # Wider SL: 2-3% based on momentum
        if pump_1m >= 5:
            sl_pct = 2.0
        elif pump_1m >= 3:
            sl_pct = 2.5
        else:
            sl_pct = 3.0

        support = round(price * (1 - sl_pct/100), 6)
        resistance = round(price * (1 + sl_pct/100*2), 6)
        pullback_entry = round(price * 0.997, 6)
        sl = round(price * (1 - sl_pct/100), 6)

        upside = "HIGH" if pump_1m >= 5 and vol_m >= 20 else ("MEDIUM" if pump_1m >= 3 else "NORMAL")

        tp = []
        for tp_pct, share in self.TP_LEVELS:
            tp.append({
                "price": round(price * (1 + tp_pct / 100), 6),
                "pct": tp_pct,
                "share": share,
            })

        avg_tp = sum(t["pct"] * t["share"] / 100 for t in tp)
        rr = round(avg_tp / sl_pct, 1)

        reasons = []
        if pump_1m >= 3:
            reasons.append("strong 1min momentum")
        if trend_ok:
            reasons.append("5min trend aligned")
        if vol_m >= 10:
            reasons.append("high volume support")
        if not reasons:
            reasons.append("price breakout")

        return {
            "symbol": symbol,
            "price": price,
            "direction": "LONG",
            "support": support,
            "resistance": resistance,
            "pullback_entry": pullback_entry,
            "sl": sl,
            "sl_pct": round(sl_pct, 1),
            "tp": tp,
            "upside": upside,
            "position": position,
            "size_pct": size_pct,
            "score": score,
            "rr": rr,
            "reasons": reasons,
            "pump_1m": round(pump_1m, 1),
            "change_5m": round(change_5m, 1),
            "vol_m": round(vol_m, 1),
            "trend_ok": trend_ok,
            "can_enter": position != "WATCH" and size_pct > 0,
        }

    def analyze_short(self, symbol, price, dump_1m, change_5m, volume):
        vol_m = volume / 1e6

        trend_ok = change_5m < 0
        vol_ok = vol_m >= 0.5

        score = 30
        if dump_1m >= 5:
            score += 30
        elif dump_1m >= 3:
            score += 20
        elif dump_1m >= 2:
            score += 10
        if trend_ok:
            score += 20
        if vol_m >= 50:
            score += 20
        elif vol_m >= 10:
            score += 12
        elif vol_m >= 1:
            score += 6
        score = min(100, score)

        if score < 50 or not trend_ok:
            position = "WATCH"
            size_pct = 0
        elif score >= 80:
            position = "HEAVY"
            size_pct = 20
        elif score >= 65:
            position = "MEDIUM"
            size_pct = 12
        elif score >= 50:
            position = "LIGHT"
            size_pct = 6
        else:
            position = "WATCH"
            size_pct = 0

        if dump_1m >= 5:
            sl_pct = 2.0
        elif dump_1m >= 3:
            sl_pct = 2.5
        else:
            sl_pct = 3.0

        support = round(price * (1 + sl_pct/100*2), 6)
        resistance = round(price * (1 - sl_pct/100), 6)
        pullback_entry = round(price * 1.003, 6)
        sl = round(price * (1 + sl_pct/100), 6)

        upside = "HIGH" if dump_1m >= 5 and vol_m >= 20 else ("MEDIUM" if dump_1m >= 3 else "NORMAL")

        tp = []
        for tp_pct, share in self.TP_LEVELS:
            tp.append({
                "price": round(price * (1 - tp_pct / 100), 6),
                "pct": tp_pct,
                "share": share,
            })

        avg_tp = sum(t["pct"] * t["share"] / 100 for t in tp)
        rr = round(avg_tp / sl_pct, 1)

        reasons = []
        if dump_1m >= 3:
            reasons.append("strong 1min sell-off")
        if trend_ok:
            reasons.append("5min downtrend aligned")
        if vol_m >= 10:
            reasons.append("high volume support")
        if not reasons:
            reasons.append("price breakdown")

        return {
            "symbol": symbol,
            "price": price,
            "direction": "SHORT",
            "support": support,
            "resistance": resistance,
            "pullback_entry": pullback_entry,
            "sl": sl,
            "sl_pct": round(sl_pct, 1),
            "tp": tp,
            "upside": upside,
            "position": position,
            "size_pct": size_pct,
            "score": score,
            "rr": rr,
            "reasons": reasons,
            "pump_1m": round(dump_1m, 1),
            "change_5m": round(change_5m, 1),
            "vol_m": round(vol_m, 1),
            "trend_ok": trend_ok,
            "can_enter": position != "WATCH" and size_pct > 0,
        }
