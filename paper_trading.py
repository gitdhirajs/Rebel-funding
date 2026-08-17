"""
Paper-trading simulator for the live signals feed.

Mirrors every real signal/close event onto a single $2.5K virtual account at
the minimum lot size (0.1), using Goat Funded Trader's real "2-Step GOAT"
evaluation rules (help.goatfundedtrader.com/en/articles/13575348-2-step-goat-model):
  - Daily drawdown: 4% (static, off starting balance)
  - Max drawdown: 10% (static floor at 90% of starting balance)
  - Phase 1 profit target: 8%; Phase 2 profit target: 6%
  - Min. 3 valid trading days per phase (a valid day = that day's P/L >= 0.5%
    of starting balance)

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

TIERS = {"2.5K": 2500.0}
DAILY_LOSS_PCT = 0.04     # GOAT 2-Step: 4% daily drawdown
TOTAL_LOSS_PCT = 0.10     # GOAT 2-Step: 10% static max drawdown
PHASE_TARGET_PCT = 0.08   # GOAT 2-Step Phase 1 profit target (Phase 2 is 6%)
VALID_DAY_PCT = 0.005     # a trading day only counts if that day's P/L >= 0.5% of start
MIN_VALID_DAYS = 3        # required valid trading days to clear a phase

def daily_limit(tier):
    return TIERS[tier] * DAILY_LOSS_PCT

def total_limit(tier):
    return TIERS[tier] * TOTAL_LOSS_PCT

def phase_target(tier):
    return TIERS[tier] * PHASE_TARGET_PCT

def valid_day_threshold(tier):
    return TIERS[tier] * VALID_DAY_PCT

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
    return {"balance": 0.0, "total_pl": 0.0, "daily": {}, "failed": False, "breaches": [],
            "valid_days": [], "passed": False}


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


# Symbols allowed for this account, ranked "safest" -> "moderate" by real
# per-0.01-lot risk observed in trader_trades/ (see symbol_risk_profile_001lot.csv).
# Crypto (high tier: BTCUSDT/ETHUSDT/XMRUSDT/BNBUSDT/...) and all four metals
# (XAU/XAG/XPT/XPD - avoid tier, worst single-trade loss $74-$642 at 0.01 lot)
# are intentionally excluded per account risk tolerance.
ALLOWED_SYMBOLS = {
    # safest - indices & single stocks, worst-case 0.01-lot loss ~$1-12
    "US500", "USTEC", "USTEC.V", "DE30.V", "UK100.V", "F40.V", "STOXX50.V",
    "AAPL.OQ", "AMZN.OQ", "TSLA.OQ", "1NGAS",
    # moderate - major/minor FX, worst-case 0.01-lot loss ~$15-37
    "EUR/USD", "EURUSD", "GBP/USD", "GBPUSD", "AUD/USD", "AUDUSD",
    "USD/JPY", "USDJPY", "NZD/USD", "NZDUSD", "USD/CAD", "USDCAD",
    "USD/CHF", "USDCHF", "GBP/JPY", "GBPJPY", "EUR/JPY", "EURJPY",
    "AUD/JPY", "AUDJPY", "CAD/JPY", "CADJPY", "NZD/JPY", "NZDJPY",
    "EUR/GBP", "EURGBP", "EUR/CHF", "EURCHF", "AUD/CAD", "AUDCAD",
    "NZD/CAD", "NZDCAD", "AUD/CHF", "AUDCHF", "NZD/CHF", "NZDCHF",
    "AUD/NZD", "AUDNZD", "EUR/NZD", "EURNZD", "EUR/CAD", "EURCAD",
    "EUR/AUD", "EURAUD", "GBP/CHF", "GBPCHF", "GBP/AUD", "GBPAUD",
    "GBP/CAD", "GBPCAD", "GBP/NZD", "GBPNZD", "CAD/CHF", "CADCHF",
    "CHF/JPY", "CHFJPY", "DJ30", "1USO",
}

def is_symbol_allowed(symbol):
    return normalize_symbol(symbol) in {normalize_symbol(s) for s in ALLOWED_SYMBOLS}

def tier_can_open(ledger, tier):
    t = ledger["tiers"][tier]
    if t["failed"] or t["passed"]:
        return False
    day_pl = t["daily"].get(today_str(), {}).get("pl", 0.0)
    return day_pl > -daily_limit(tier)


def record_open(ledger, pos_key, symbol, direction, entry_price):
    """Opens a shared virtual position for whichever tiers are currently
    still allowed to trade. Returns False if every tier is locked out, or if
    the symbol isn't on the account's allowed list (metals/crypto excluded)."""
    if not is_symbol_allowed(symbol):
        return False
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
        # Positions opened before a TIERS change (e.g. the old 5K/3%/12% setup)
        # can carry a tier name that no longer exists in the *current* TIERS
        # config - daily_limit/total_limit/phase_target all index TIERS[tier]
        # directly, so check against TIERS (not just ledger["tiers"], which
        # never prunes old entries) before touching any of them.
        if tier not in TIERS:
            continue
        t = ledger["tiers"].get(tier)
        if t is None:
            continue
        t["balance"] += pl
        t["total_pl"] += pl
        day_entry = t["daily"].setdefault(day, {"pl": 0.0, "trades": 0})
        day_entry["pl"] += pl
        day_entry["trades"] += 1
        day_pl = day_entry["pl"]
        tier_line = (f"  [{tier}] Day {day_pl:+.2f}/-{daily_limit(tier):.0f}  "
                     f"Total {t['total_pl']:+.2f}/+{phase_target(tier):.0f} (fail<-{total_limit(tier):.0f})  "
                     f"Bal {t['balance']:.2f}  ValidDays {len(t['valid_days'])}/{MIN_VALID_DAYS}")
        if day not in t["breaches"] and day_pl <= -daily_limit(tier):
            t["breaches"].append(day)
            tier_line += "  ** DAILY LIMIT HIT **"
        if not t["failed"] and t["total_pl"] <= -total_limit(tier):
            t["failed"] = True
            tier_line += "  ** TOTAL LIMIT HIT - FAILED (paper) **"
        if day_pl >= valid_day_threshold(tier) and day not in t["valid_days"]:
            t["valid_days"].append(day)
            tier_line += f"  ** VALID TRADING DAY ({len(t['valid_days'])}/{MIN_VALID_DAYS}) **"
        if not t["failed"] and not t["passed"] and t["total_pl"] >= phase_target(tier) \
                and len(t["valid_days"]) >= MIN_VALID_DAYS:
            t["passed"] = True
            tier_line += "  ** PHASE TARGET MET - PASSED (paper) **"
        lines.append(tier_line)

    save_ledger(ledger)
    return "\n".join(lines)


def status_line(ledger):
    day = today_str()
    parts = ["[PAPER STATUS]"]
    for tier in TIERS:
        t = ledger["tiers"][tier]
        day_pl = t["daily"].get(day, {}).get("pl", 0.0)
        flag = " FAILED" if t["failed"] else (" PASSED" if t["passed"] else "")
        parts.append(f"  [{tier}] Bal {t['balance']:.2f}  Today {day_pl:+.2f}/-{daily_limit(tier):.0f}  "
                      f"Total {t['total_pl']:+.2f}/+{phase_target(tier):.0f} (fail<-{total_limit(tier):.0f})  "
                      f"ValidDays {len(t['valid_days'])}/{MIN_VALID_DAYS}{flag}")
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
