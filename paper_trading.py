"""
Paper-trading simulator for the live signals feed.

Mirrors every real signal/close event onto a single $5K virtual account at
the minimum lot size (0.1), using the real firm rules from the pricing page:
3% daily loss limit, 12% max (total) loss limit.

While a signal is open, every scraper run (~every 15 min) polls an
independent live market price (via yfinance - NOT the source site's own
numbers) and records the worst (most adverse) and best point reached before
the position closes. That's the "did it dip hard before working out" record.

Contract specs (pip size / $ value per 0.1 lot) for XAU/USD and EUR/USD are
verified against this feed's real trade history. GBP/JPY, XAG/USD, BTCUSDT and
USTEC.v are reasonable industry-standard approximations - broker contract
specs vary, so treat those as directional. Adjust SYMBOL_SPECS if you know
your actual broker's contract sizes.
"""
import json
import os
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

LEDGER_FILE = "paper_ledger.json"
SIM_DAYS = 7

TIERS = {"5K": 5000.0}
DAILY_LOSS_PCT = 0.03   # from the pricing page: Daily Loss 3%
TOTAL_LOSS_PCT = 0.12   # from the pricing page: Max Loss 12%

def daily_limit(tier):
    return TIERS[tier] * DAILY_LOSS_PCT

def total_limit(tier):
    return TIERS[tier] * TOTAL_LOSS_PCT

# pip_size = smallest price increment to measure distance in.
# pip_value = $ P/L per pip_size move, AT 0.1 LOT (the minimum size) - same
# for every tier, since lot size is fixed regardless of account size.
SYMBOL_SPECS = {
    "XAUUSD": {"pip_size": 1.0, "pip_value": 10.0, "yf": "GC=F"},      # verified
    "XAGUSD": {"pip_size": 1.0, "pip_value": 50.0, "yf": "SI=F"},       # approx
    "EURUSD": {"pip_size": 0.0001, "pip_value": 1.0, "yf": "EURUSD=X"}, # verified
    "GBPUSD": {"pip_size": 0.0001, "pip_value": 1.0, "yf": "GBPUSD=X"}, # approx
    "USDJPY": {"pip_size": 0.01, "pip_value": 0.65, "yf": "JPY=X"},     # approx
    "GBPJPY": {"pip_size": 0.01, "pip_value": 0.65, "yf": "GBPJPY=X"},  # approx
    "EURJPY": {"pip_size": 0.01, "pip_value": 0.65, "yf": "EURJPY=X"},  # approx
    "BTCUSDT": {"pip_size": 1.0, "pip_value": 0.1, "yf": "BTC-USD"},    # approx
    "USTEC": {"pip_size": 1.0, "pip_value": 0.1, "yf": "NQ=F"},         # approx
}
DEFAULT_SPEC = {"pip_size": 1.0, "pip_value": 1.0, "yf": None}


def normalize_symbol(sym):
    return str(sym).upper().replace("/", "").replace(".V", "").replace(" ", "").strip()


def get_spec(symbol):
    norm = normalize_symbol(symbol)
    if norm in SYMBOL_SPECS:
        return SYMBOL_SPECS[norm], True
    return DEFAULT_SPEC, False


def today_str():
    return datetime.now(IST).strftime('%Y-%m-%d')


def _new_tier_state():
    return {"balance": 0.0, "total_pl": 0.0, "daily": {}, "failed": False, "breaches": []}


def load_ledger():
    if os.path.exists(LEDGER_FILE):
        try:
            with open(LEDGER_FILE, 'r') as f:
                ledger = json.load(f)
                ledger.setdefault("open_positions", {})
                ledger.setdefault("tiers", {})
                for t, start in TIERS.items():
                    ledger["tiers"].setdefault(t, {**_new_tier_state(), "balance": start})
                return ledger
        except Exception:
            pass
    return {
        "tiers": {t: {**_new_tier_state(), "balance": start} for t, start in TIERS.items()},
        "trade_log": [],
        "open_positions": {},
        "start_date": today_str(),
        "week_summary_sent": False,
    }


def save_ledger(ledger):
    with open(LEDGER_FILE, 'w') as f:
        json.dump(ledger, f, indent=2)


def tier_can_open(ledger, tier):
    t = ledger["tiers"][tier]
    if t["failed"]:
        return False
    day_pl = t["daily"].get(today_str(), {}).get("pl", 0.0)
    return day_pl > -daily_limit(tier)


def record_open(ledger, pos_key, symbol, direction, entry_price):
    """Opens a shared virtual position for whichever tiers are currently
    still allowed to trade. Returns False if every tier is locked out."""
    eligible = [t for t in TIERS if tier_can_open(ledger, t)]
    if not eligible:
        return False
    try:
        entry = float(str(entry_price).replace(',', '').strip())
    except (ValueError, TypeError):
        entry = None
    ledger["open_positions"][pos_key] = {
        "symbol": symbol,
        "direction": str(direction).upper(),
        "entry": entry,
        "opened": datetime.now(IST).isoformat(),
        "eligible_tiers": eligible,
        "worst_pl": 0.0, "worst_time": None,
        "best_pl": 0.0, "best_time": None,
    }
    return True


def _pl_at_price(pos, price):
    spec, known = get_spec(pos["symbol"])
    move = (price - pos["entry"]) if pos["direction"] == "BUY" else (pos["entry"] - price)
    return (move / spec["pip_size"]) * spec["pip_value"], known


def poll_open_positions(ledger):
    """Call every run (independent of any close event) to update the
    worst/best excursion of every still-open position using a LIVE market
    price - not the source site's own P/L. Requires yfinance + internet;
    fails silently (per-position) if a price can't be fetched."""
    if not ledger["open_positions"]:
        return
    try:
        import yfinance as yf
    except ImportError:
        print("yfinance not installed - skipping live excursion poll")
        return

    now_iso = datetime.now(IST).isoformat()
    for pos_key, pos in ledger["open_positions"].items():
        if pos.get("entry") is None:
            continue
        spec, known = get_spec(pos["symbol"])
        ticker = spec.get("yf")
        if not ticker:
            continue
        try:
            t = yf.Ticker(ticker)
            price = t.fast_info["last_price"]
        except Exception as e:
            print(f"Price fetch failed for {pos['symbol']} ({ticker}): {e}")
            continue

        pl, _ = _pl_at_price(pos, price)
        if pl < pos["worst_pl"]:
            pos["worst_pl"] = round(pl, 2)
            pos["worst_time"] = now_iso
        if pl > pos["best_pl"]:
            pos["best_pl"] = round(pl, 2)
            pos["best_time"] = now_iso
    save_ledger(ledger)


def record_close(ledger, pos_key, close_price):
    """Closes the virtual position (if one was opened) and returns a Discord
    message string to post, or None if there was nothing to report."""
    pos = ledger["open_positions"].pop(pos_key, None)
    if not pos or pos.get("entry") is None:
        return None
    try:
        close = float(str(close_price).replace(',', '').strip())
    except (ValueError, TypeError):
        return None

    pl, known = _pl_at_price(pos, close)
    # final close might exceed the worst/best seen during polling (e.g. gaps)
    worst = min(pos["worst_pl"], pl)
    best = max(pos["best_pl"], pl)

    day = today_str()
    ledger["trade_log"].append({
        "symbol": pos["symbol"], "direction": pos["direction"],
        "entry": pos["entry"], "close": close, "pl": round(pl, 2),
        "worst_pl": worst, "best_pl": best,
        "closed": datetime.now(IST).isoformat(), "spec_known": known,
    })

    lines = [
        f"[PAPER] {pos['symbol']} {pos['direction']} closed  Final P/L: {pl:+.2f}"
        + ("" if known else "  (approx contract spec)"),
        f"  Path while open -> worst: {worst:+.2f}   best: {best:+.2f}",
    ]

    for tier in pos["eligible_tiers"]:
        t = ledger["tiers"][tier]
        t["balance"] += pl
        t["total_pl"] += pl
        day_entry = t["daily"].setdefault(day, {"pl": 0.0, "trades": 0})
        day_entry["pl"] += pl
        day_entry["trades"] += 1
        day_pl = day_entry["pl"]
        tier_line = (f"  [{tier}] Day {day_pl:+.2f}/-{daily_limit(tier):.0f}  "
                     f"Total {t['total_pl']:+.2f}/-{total_limit(tier):.0f}  Bal {t['balance']:.2f}")
        if day not in t["breaches"] and day_pl <= -daily_limit(tier):
            t["breaches"].append(day)
            tier_line += "  ** DAILY LIMIT HIT **"
        if not t["failed"] and t["total_pl"] <= -total_limit(tier):
            t["failed"] = True
            tier_line += "  ** TOTAL LIMIT HIT - FAILED (paper) **"
        lines.append(tier_line)

    save_ledger(ledger)
    return "\n".join(lines)


def status_line(ledger):
    day = today_str()
    parts = ["[PAPER STATUS]"]
    for tier in TIERS:
        t = ledger["tiers"][tier]
        day_pl = t["daily"].get(day, {}).get("pl", 0.0)
        flag = " FAILED" if t["failed"] else ""
        parts.append(f"  [{tier}] Bal {t['balance']:.2f}  Today {day_pl:+.2f}/-{daily_limit(tier):.0f}  "
                      f"Total {t['total_pl']:+.2f}/-{total_limit(tier):.0f}{flag}")
    return "\n".join(parts)


def week_summary_if_due(ledger):
    if ledger.get("week_summary_sent"):
        return None
    start = datetime.strptime(ledger["start_date"], '%Y-%m-%d').replace(tzinfo=IST)
    if (datetime.now(IST) - start).days < SIM_DAYS:
        return None
    ledger["week_summary_sent"] = True
    save_ledger(ledger)
    trades = ledger["trade_log"]
    wins = [t for t in trades if t["pl"] > 0]
    lines = [f"[PAPER] 7-DAY SIMULATION COMPLETE - {len(trades)} trades ({len(wins)}W/{len(trades)-len(wins)}L)"]
    for tier in TIERS:
        t = ledger["tiers"][tier]
        result = "FAILED" if t["failed"] else ("profitable" if t["total_pl"] > 0 else "net loss")
        lines.append(f"  [{tier}] {result}  Total P/L {t['total_pl']:+.2f}  Final balance {t['balance']:.2f}")
    return "\n".join(lines)
