import logging
import time
import requests
from typing import Dict, List, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


class WhaleMonitor:
    """Detect whale/manipulation signals from Gate.io public APIs."""

    # Mainstream / platform / stablecoins excluded from whale scan
    _EXCLUDE = {
        "BTC_USDT", "ETH_USDT", "SOL_USDT", "BNB_USDT", "XRP_USDT",
        "DOGE_USDT", "ADA_USDT", "AVAX_USDT", "DOT_USDT", "LINK_USDT",
        "MATIC_USDT", "TRX_USDT", "LTC_USDT", "ATOM_USDT", "UNI_USDT",
        "OKB_USDT", "GT_USDT", "APT_USDT", "ARB_USDT", "OP_USDT",
        "NEAR_USDT", "FIL_USDT", "ETC_USDT", "INJ_USDT", "TIA_USDT",
        "SUI_USDT", "SEI_USDT", "ZEC_USDT", "HYPE_USDT", "TAO_USDT",
        "ICP_USDT", "TON_USDT", "XAUT_USDT",
    }

    STATS_URL = "https://api.gateio.ws/api/v4/futures/usdt/contract_stats"
    ORDERBOOK_URL = "https://api.gateio.ws/api/v4/futures/usdt/order_book"
    TRADES_URL = "https://api.gateio.ws/api/v4/futures/usdt/trades"

    def __init__(self):
        self._session = None
        self._last_depth = {}
        self._last_funding = {}
        self._last_oi_div = {}
        self._last_large = {}

    @property
    def session(self):
        if self._session is None:
            self._session = requests.Session()
            self._session.trust_env = False
        return self._session

    # ── Funding rate ────────────────────────────────────────────

    def check_funding(self, symbol: str) -> Optional[dict]:
        try:
            resp = self.session.get(self.STATS_URL, params={
                "contract": symbol, "interval": "5m", "limit": 1
            }, timeout=8)
            data = resp.json()
            if not data:
                return None
            d = data[0]
            rate = float(d.get("funding_rate", 0)) * 100
            lsr = float(d.get("lsr_taker", 0))
            oi = float(d.get("open_interest_usd", 0))

            warning = None
            if abs(rate) > 0.5:
                warning = f"多头拥挤 费率{rate:+.3f}%" if rate > 0 else f"空头拥挤 费率{rate:+.3f}%"
            elif lsr > 3:
                warning = f"多空比{lsr:.1f} 多单过度"

            return {"symbol": symbol, "funding": round(rate, 4), "lsr": round(lsr, 2),
                    "oi": oi, "warning": warning}
        except Exception as e:
            logger.debug(f"Funding {symbol}: {e}")
            return None

    # ── Order book depth ────────────────────────────────────────

    def check_depth(self, symbol: str, price: float, volume: float) -> Optional[dict]:
        try:
            resp = self.session.get(self.ORDERBOOK_URL, params={
                "contract": symbol, "interval": "0", "limit": 20
            }, timeout=8)
            data = resp.json()
            bids, asks = data.get("bids", []), data.get("asks", [])
            if not bids or not asks:
                return None

            bid_sum = sum(float(b[1]) for b in bids if float(b[0]) >= price * 0.98)
            ask_sum = sum(float(a[1]) for a in asks if float(a[0]) <= price * 1.02)
            threshold = volume * 0.005 if volume > 0 else 100000

            thin_bid = bid_sum < threshold
            thin_ask = ask_sum < threshold
            warning = None

            if thin_bid and thin_ask:
                warning = f"盘口极薄 bid:{bid_sum:.0f} ask:{ask_sum:.0f}U 易操纵"
            elif thin_bid:
                warning = f"买盘薄弱 {bid_sum:.0f}U 易砸穿"
            elif thin_ask:
                warning = f"卖盘薄弱 {ask_sum:.0f}U 易拉升"

            return {"symbol": symbol, "bid_depth": round(bid_sum, 0),
                    "ask_depth": round(ask_sum, 0), "warning": warning}
        except Exception as e:
            logger.debug(f"Depth {symbol}: {e}")
            return None

    # ── OI vs Price divergence ──────────────────────────────────

    def check_oi_divergence(self, symbol: str, price_change_pct: float,
                            oi_change_pct: float) -> Optional[dict]:
        """Price up + OI down = distribution (whales selling into strength)."""
        warning = None
        if price_change_pct > 3 and oi_change_pct < -5:
            warning = f"价涨{price_change_pct:+.1f}% OI跌{oi_change_pct:+.1f}% 主力出货"
        elif price_change_pct < -3 and oi_change_pct > 10:
            warning = f"价跌{price_change_pct:+.1f}% OI涨{oi_change_pct:+.1f}% 主力吸筹"

        if warning:
            return {"symbol": symbol, "price_chg": round(price_change_pct, 1),
                    "oi_chg": round(oi_change_pct, 1), "warning": warning}
        return None

    # ── Large trades ────────────────────────────────────────────

    def check_large_trades(self, symbol: str, min_value: float = 30000) -> List[dict]:
        try:
            resp = self.session.get(self.TRADES_URL, params={
                "contract": symbol, "limit": 100
            }, timeout=8)
            data = resp.json()
            large = []
            for t in data:
                size = float(t.get("size", 0))
                price = float(t.get("price", 0))
                value = abs(size) * price
                if value >= min_value:
                    large.append({
                        "symbol": symbol,
                        "side": "买入" if size > 0 else "卖出",
                        "value": round(value, 0),
                        "price": price,
                        "time": datetime.fromtimestamp(float(t.get("create_time", 0))).strftime("%H:%M:%S"),
                    })
            return large
        except Exception as e:
            logger.debug(f"Trades {symbol}: {e}")
            return []

    # ── Full scan ───────────────────────────────────────────────

    def scan(self, tickers: dict, oi_history: dict) -> Dict[str, list]:
        """Returns {funding, depth, oi_div, large_trades}."""
        now = time.time()
        results = {"funding": [], "depth": [], "oi_div": [], "large_trades": []}

        syms = sorted([s for s in tickers if s not in self._EXCLUDE],
                      key=lambda s: tickers[s].get("volume", 0), reverse=True)[:25]

        for sym in syms:
            info = tickers[sym]
            price = info.get("price", 0)
            volume = info.get("volume", 0)

            # 1) Funding rate (30min dedup)
            if now - self._last_funding.get(sym, 0) > 1800:
                f = self.check_funding(sym)
                if f and f.get("warning"):
                    results["funding"].append(f)
                    self._last_funding[sym] = now

            # 2) Order book depth (30min, top 15 vol only)
            if volume >= 5000000 and sym in syms[:15]:
                if now - self._last_depth.get(sym, 0) > 1800:
                    d = self.check_depth(sym, price, volume)
                    if d and d.get("warning"):
                        results["depth"].append(d)
                        self._last_depth[sym] = now

            # 3) OI divergence
            if sym in oi_history and len(oi_history[sym]) >= 2:
                oi_list = oi_history[sym]
                oi_now = oi_list[-1][1]
                oi_old = None
                target = now - 300
                for ts, oi in reversed(oi_list):
                    if ts <= target:
                        oi_old = oi
                        break
                if oi_old and oi_old > 0:
                    oi_chg = ((oi_now - oi_old) / oi_old) * 100
                    price_old = None
                    for ts, p in reversed(oi_list):
                        if ts <= target:
                            price_old = p
                            break
                    if price_old is None:
                        price_old = price
                    price_chg = ((price - price_old) / price_old) * 100 if price_old else 0
                    if now - self._last_oi_div.get(sym, 0) > 1800:
                        div = self.check_oi_divergence(sym, price_chg, oi_chg)
                        if div:
                            results["oi_div"].append(div)
                            self._last_oi_div[sym] = now

            # 4) Large trades (10min, top 10 vol)
            if sym in syms[:10] and now - self._last_large.get(sym, 0) > 600:
                trades = self.check_large_trades(sym)
                if trades:
                    results["large_trades"].extend(trades[:3])
                    self._last_large[sym] = now

        return results
