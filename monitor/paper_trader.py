"""Paper trading simulator - 1000 USDT starting capital."""
import logging
import time

logger = logging.getLogger(__name__)


class PaperTrader:
    def __init__(self, initial_balance=1000.0):
        self.initial_balance = initial_balance
        self.balance = initial_balance
        self.positions = {}
        self.closed_trades = []
        self.next_id = 1

    def open_position(self, signal):
        sym = signal["symbol"]
        if sym in self.positions:
            return None

        size_pct = signal.get("size_pct", 8) / 100
        size_usdt = round(self.balance * size_pct, 2)
        if size_usdt < 5:
            return None

        pos = {
            "id": self.next_id,
            "symbol": sym,
            "direction": signal["direction"],
            "entry": signal["price"],
            "sl": signal["sl"],
            "tp": [dict(t) for t in signal.get("tp", [])],
            "size_usdt": size_usdt,
            "size_qty": round(size_usdt / signal["price"], 6),
            "remaining_pct": 100,
            "open_time": time.time(),
            "pnl": 0,
            "status": "open",
        }

        self.positions[sym] = pos
        self.balance -= size_usdt
        self.next_id += 1

        logger.info(
            "TRADE OPEN: " + sym + " " + signal["direction"]
            + " size=" + str(size_usdt) + "U entry=" + str(signal["price"])
            + " sl=" + str(signal["sl"])
        )
        return pos

    def check_positions(self, tickers):
        for sym in list(self.positions.keys()):
            pos = self.positions[sym]
            ticker = tickers.get(sym)
            if not ticker:
                continue
            price = ticker.get("price", 0)
            if price <= 0:
                continue

            direction = pos["direction"]

            # Check SL
            if direction == "LONG" and price <= pos["sl"]:
                self._close_full(sym, price, "SL")
                continue
            elif direction == "SHORT" and price >= pos["sl"]:
                self._close_full(sym, price, "SL")
                continue

            # Check TP levels
            for tp in pos["tp"]:
                if tp.get("hit"):
                    continue
                hit = False
                if direction == "LONG" and price >= tp["price"]:
                    hit = True
                elif direction == "SHORT" and price <= tp["price"]:
                    hit = True

                if hit:
                    tp["hit"] = True
                    share = tp["share"]
                    pnl = self._calc_pnl(pos, price, share)
                    pos["remaining_pct"] -= share
                    self.balance += pos["size_usdt"] * (share / 100) + pnl
                    logger.info(
                        "TP HIT: " + sym + " " + str(tp["price"])
                        + " (+" + str(tp["pct"]) + "%) close " + str(share) + "%"
                        + " pnl=" + str(round(pnl, 2)) + "U"
                    )
                    if pos["remaining_pct"] <= 0:
                        self._close_full(sym, price, "TP")
                    break

    def _calc_pnl(self, pos, price, pct):
        size_part = pos["size_usdt"] * (pct / 100)
        if pos["direction"] == "LONG":
            return round(size_part * (price - pos["entry"]) / pos["entry"], 2)
        else:
            return round(size_part * (pos["entry"] - price) / pos["entry"], 2)

    def _close_full(self, sym, price, reason):
        pos = self.positions.pop(sym, None)
        if not pos:
            return

        remaining = pos["remaining_pct"]
        if pos["direction"] == "LONG":
            pnl = pos["size_usdt"] * (price - pos["entry"]) / pos["entry"]
        else:
            pnl = pos["size_usdt"] * (pos["entry"] - price) / pos["entry"]

        self.balance += pos["size_usdt"] * (remaining / 100) + pnl * (remaining / 100)

        trade = {
            "id": pos["id"],
            "symbol": pos["symbol"],
            "direction": pos["direction"],
            "entry": pos["entry"],
            "exit": price,
            "size": pos["size_usdt"],
            "reason": reason,
            "duration_min": round((time.time() - pos["open_time"]) / 60, 1),
        }

        if pos["direction"] == "LONG":
            trade["pnl"] = round(pos["size_usdt"] * (price - pos["entry"]) / pos["entry"], 2)
        else:
            trade["pnl"] = round(pos["size_usdt"] * (pos["entry"] - price) / pos["entry"], 2)

        trade["pnl_pct"] = round(trade["pnl"] / pos["size_usdt"] * 100, 2)
        trade["win"] = trade["pnl"] > 0
        self.closed_trades.append(trade)

        logger.info(
            "TRADE CLOSE: " + pos["symbol"] + " " + reason
            + " pnl=" + str(trade["pnl"]) + "U (" + str(trade["pnl_pct"]) + "%)"
        )

    def get_summary(self):
        total_trades = len(self.closed_trades)
        wins = sum(1 for t in self.closed_trades if t.get("win"))
        win_rate = round(wins / total_trades * 100, 1) if total_trades else 0
        total_pnl = sum(t.get("pnl", 0) for t in self.closed_trades)

        best = max(self.closed_trades, key=lambda t: t.get("pnl", -9999)) if self.closed_trades else None
        worst = min(self.closed_trades, key=lambda t: t.get("pnl", 9999)) if self.closed_trades else None

        return {
            "balance": round(self.balance, 2),
            "initial": self.initial_balance,
            "total_pnl": round(self.balance - self.initial_balance + sum(
                pos["size_usdt"] for pos in self.positions.values()
            ), 2),
            "total_pnl_pct": round((self.balance - self.initial_balance) / self.initial_balance * 100, 2),
            "positions_open": len(self.positions),
            "positions": list(self.positions.values()),
            "total_trades": total_trades,
            "wins": wins,
            "losses": total_trades - wins,
            "win_rate": win_rate,
            "realized_pnl": round(total_pnl, 2),
            "best": best,
            "worst": worst,
            "closed_trades": self.closed_trades[-10:],
        }

    def format_summary(self):
        s = self.get_summary()
        sign = "+" if s["total_pnl"] >= 0 else ""
        lines = [
            "*ACCOUNT* " + str(s["balance"]) + "U (" + sign + str(s["total_pnl_pct"]) + "%)",
            "",
            "Realized PnL: " + sign + str(s["realized_pnl"]) + "U",
            "Trades: " + str(s["total_trades"]) + " | Win: " + str(s["win_rate"]) + "% | Open: " + str(s["positions_open"]),
        ]
        if s["best"]:
            lines.append("Best: " + s["best"]["symbol"] + " " + str(s["best"]["pnl"]) + "U (" + str(s["best"]["pnl_pct"]) + "%)")
        if s["worst"]:
            lines.append("Worst: " + s["worst"]["symbol"] + " " + str(s["worst"]["pnl"]) + "U (" + str(s["worst"]["pnl_pct"]) + "%)")
        if s["closed_trades"]:
            lines.append("")
            lines.append("*Recent Closed*")
            for t in s["closed_trades"]:
                wl = "WIN " if t.get("win") else "LOSS"
                lines.append(
                    wl + " " + t["symbol"] + " " + t["direction"]
                    + " " + str(t["pnl"]) + "U (" + str(t["pnl_pct"]) + "%)"
                    + " " + str(t["duration_min"]) + "min"
                )
        if s["positions_open"] > 0:
            lines.append("")
            lines.append("*Open Positions*")
            for p in s["positions"]:
                lines.append(
                    p["symbol"] + " " + p["direction"]
                    + " entry=" + str(p["entry"]) + " size=" + str(p["size_usdt"]) + "U"
                )
        return "\n".join(lines)
