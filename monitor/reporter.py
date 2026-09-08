"""Alert message formatting (Chinese, for Telegram)."""
from typing import Optional

def _fmt_usd(x: float) -> str:
    x = float(x or 0)
    if x >= 1e12:
        return "%.2f万亿" % (x / 1e12)
    if x >= 1e8:
        return "%.2f亿" % (x / 1e8)
    if x >= 1e4:
        return "%.2f万" % (x / 1e4)
    return "%.0f" % x

def _fmt_price(x: float) -> str:
    x = float(x or 0)
    if x >= 1000:
        return "{:,.0f}".format(x)
    if x >= 1:
        return "{:,.2f}".format(x)
    return "{:,.4f}".format(x)

def _fmt_named(coin: str) -> str:
    # Strip the USDT suffix for the display name.
    return coin[:-4] if coin.upper().endswith("USDT") else coin

def _fmt_pct(pct: float) -> str:
    return ("+%.1f%%" % pct) if pct >= 0 else ("%.1f%%" % pct)

def format_alert(ev) -> str:
    coin = _fmt_named(ev.symbol)
    if ev.kind == "VOLUME":
        lines = [
            "🚨 成交量异动 | %s" % coin,
            "单日成交量 %s" % _fmt_pct(ev.pct),
            "突破档位：%s%%（第%d档）" % (int(ev.threshold), ev.level),
            "24h量：%s USDT | 现价：%s" % (_fmt_usd(ev.volume_24h), _fmt_price(ev.price)),
            "时间：%s" % ev.time_str,
        ]
    else:
        action = "增仓" if ev.direction == "up" else "减仓"
        lines = [
            "🟠 OI异动 | %s（1小时）" % coin,
            "OI %s（%s）" % (_fmt_pct(ev.pct), action),
            "突破档位：%s%%（第%d档）" % (int(ev.threshold), ev.level),
            "当前OI：%s USDT | 现价：%s" % (_fmt_usd(ev.oi_value), _fmt_price(ev.price)),
            "时间：%s" % ev.time_str,
        ]
    return "\n".join(lines)

def summary_text(snapshot_count: int, volume_alerts: int, oi_alerts: int) -> str:
    return "监控扫描 %d 个币安币种 | 本周期成交量告警 %d / OI告警 %d" % (
        snapshot_count, volume_alerts, oi_alerts
    )