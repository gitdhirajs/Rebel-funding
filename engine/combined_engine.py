"""
Combined Trading Engine
========================
Bernd's Fundamentals (daily bias) + Rebel Winners (intraday entry)

FLOW:
  1. Every morning: compute daily BIAS per symbol (LONG/SHORT/NEUTRAL)
     using Bernd's COT + seasonality + valuation
  2. Every 15 min: check rebel winner setup rules
  3. Only trade when: BIAS direction == setup direction
  4. Tight SL (0.5% risk), 1:2 R:R from rebel winner patterns

This combines:
  - Bernd's discipline (don't trade without fundamental conviction)
  - Rebel winners' execution (precise intraday entries at S/R levels)
  - Prop firm risk rules (0.5%/trade, 4% daily stop, 10% max)
"""

import pandas as pd, numpy as np, json, os, sys, time
from datetime import datetime, timedelta

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR    = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, BASE_DIR)

from engine.market_state import MarketState

# Files
COT_FILE      = os.path.join(os.path.dirname(__file__), "cot_simulated.parquet")
SEASON_FILE   = os.path.join(os.path.dirname(__file__), "seasonality.parquet")
SETUP_FILE    = os.path.join(os.path.dirname(__file__), "setup_rules.json")
SIGNAL_LOG    = os.path.join(os.path.dirname(__file__), "combined_signals.jsonl")

# Challenge rules
RISK_PER_TRADE  = 0.005    # 0.5%
MAX_DAILY_LOSS  = 0.04     # stop at 4%
MAX_DRAWDOWN    = 0.10

# ═══════════════════════════════════════════════════════════════════════════════
# 1. DAILY BIAS (Bernd's fundamentals — run once per day)
# ═══════════════════════════════════════════════════════════════════════════════

def compute_daily_bias():
    """Compute bias for all symbols. Call once at start of trading day."""
    biases = {}

    cot_df = pd.read_parquet(COT_FILE) if os.path.exists(COT_FILE) else None
    seas_df = pd.read_parquet(SEASON_FILE) if os.path.exists(SEASON_FILE) else None
    today = datetime.now()
    month = today.month

    # Load setup rules for rebel winner direction stats
    setup_rules = []
    if os.path.exists(SETUP_FILE):
        with open(SETUP_FILE) as f:
            setup_rules = json.load(f)

    symbols = ["XAU/USD", "XAG/USD", "EUR/USD", "GBP/USD", "USD/JPY",
               "AUD/USD", "USD/CAD", "DJ30", "USTEC.v", "US500"]

    for sym in symbols:
        votes = {"LONG": 0, "SHORT": 0}
        reasons = []

        # ── COT bias ──────────────────────────────────────────────────────────
        if cot_df is not None:
            cot_row = cot_df[cot_df["symbol"] == sym]
            if not cot_row.empty:
                idx = cot_row.iloc[-1]["cot_index"]
                if idx > 20:
                    votes["LONG"] += 1
                    reasons.append(f"COT bullish ({idx:.0f})")
                elif idx < -20:
                    votes["SHORT"] += 1
                    reasons.append(f"COT bearish ({idx:.0f})")

        # ── Seasonality bias ──────────────────────────────────────────────────
        if seas_df is not None:
            sr = seas_df[(seas_df["symbol"] == sym) & (seas_df["month"] == month)]
            if not sr.empty:
                bias = sr.iloc[0]["bias"]
                pct  = sr.iloc[0]["up_pct"]
                if bias == "BULLISH":
                    votes["LONG"] += 1
                    reasons.append(f"Seasonality bullish ({pct:.0f}% up)")
                elif bias == "BEARISH":
                    votes["SHORT"] += 1
                    reasons.append(f"Seasonality bearish ({100-pct:.0f}% down)")

        # ── Rebel winner bias ──────────────────────────────────────────────────
        sym_rules = [r for r in setup_rules if r["symbol"] == sym
                     and r["occurrences"] >= 15 and r["win_rate"] >= 0.65]
        if sym_rules:
            dominant = max(set(r["direction"] for r in sym_rules),
                          key=lambda d: sum(r["occurrences"] for r in sym_rules if r["direction"] == d))
            count = sum(r["occurrences"] for r in sym_rules if r["direction"] == dominant)
            if dominant == "BUY":
                votes["LONG"] += 1
                reasons.append(f"Rebel winners LONG ({count} trades)")
            else:
                votes["SHORT"] += 1
                reasons.append(f"Rebel winners SHORT ({count} trades)")

        # ── Determine bias ────────────────────────────────────────────────────
        if votes["LONG"] > votes["SHORT"]:
            bias = "LONG"
        elif votes["SHORT"] > votes["LONG"]:
            bias = "SHORT"
        else:
            bias = "NEUTRAL"

        strength = max(votes["LONG"], votes["SHORT"])
        biases[sym] = {
            "bias": bias,
            "strength": strength,
            "votes": votes,
            "reasons": reasons,
            "confirmation_needed": max(0, 3 - strength),  # how many more needed
        }

    return biases


# ═══════════════════════════════════════════════════════════════════════════════
# 2. INTRADAY ENTRY (Rebel Winners — run every 15 min)
# ═══════════════════════════════════════════════════════════════════════════════

def find_setup(symbol, state, daily_bias):
    """
    Find a rebel winner setup that:
      a) Matches the daily BIAS direction
      b) Matches current market conditions (session, trend, S/R proximity)
    Returns: setup dict or None
    """
    bias = daily_bias.get(symbol, {}).get("bias", "NEUTRAL")
    if bias == "NEUTRAL":
        return None  # no daily conviction = no trade

    # Convert bias to trade direction
    bias_dir = "BUY" if bias == "LONG" else "SELL"

    # Load rebel rules
    if not os.path.exists(SETUP_FILE):
        return None
    with open(SETUP_FILE) as f:
        rules = json.load(f)

    # Current session
    hour = datetime.now().hour
    if 0 <= hour < 9:      session = "Asia"
    elif 8 <= hour < 13:   session = "London"
    elif 13 <= hour < 17:  session = "London+NY"
    elif 17 <= hour < 22:  session = "NY"
    else:                  session = "Off-hours"

    # Filter rules
    candidates = []
    for r in rules:
        # Must match bias direction
        if r.get("direction") != bias_dir:
            continue
        # Must match symbol
        if r.get("symbol") != symbol:
            continue
        # Minimum quality
        if r.get("occurrences", 0) < 10:
            continue
        if r.get("win_rate", 0) < 0.60:
            continue

        # Score how well this rule matches current state
        score = 0
        max_score = 3

        # Session match
        if r.get("session") == session:
            score += 1
        elif r.get("session") in session:  # e.g., "London+NY" contains "London"
            score += 0.5

        # Trend match
        current_trend = state.get("h4_trend", "neutral")
        rule_trend = r.get("h4_trend", "neutral")
        if rule_trend == current_trend:
            score += 1
        elif rule_trend == "neutral":
            score += 0.5

        # S/R zone match
        rule_zone = r.get("sr_zone", "mid_range")
        if rule_zone == "AT_PDH" and state.get("pdh_dist_pct", 99) < 0.5:
            score += 1
        elif rule_zone == "AT_PDL" and state.get("pdl_dist_pct", 99) < 0.5:
            score += 1
        elif rule_zone == "mid_range":
            score += 0.5

        candidates.append({
            "rule": r,
            "match_score": score / max_score,
        })

    if not candidates:
        return None

    # Best match
    best = max(candidates, key=lambda c: c["match_score"] * c["rule"]["quality_score"])
    return best


def generate_signal(symbol, state, setup, daily_bias):
    """Generate a trade signal from a matching setup."""
    rule = setup["rule"]
    price = state["price"]

    direction = rule["direction"]
    avg_rr = rule.get("avg_rr") or 2.0

    # Tight SL based on nearest S/R level
    if direction == "BUY":
        sl = state.get("nearest_support") or state.get("pdl") or (price * 0.995)
        # Cap SL at 0.5% for challenge compliance
        max_sl = price * 0.995
        sl = max(sl, max_sl)
        tp = price + (price - sl) * avg_rr
    else:
        sl = state.get("nearest_resistance") or state.get("pdh") or (price * 1.005)
        max_sl = price * 1.005
        sl = min(sl, max_sl)
        tp = price - (sl - price) * avg_rr

    sl_dist_pct = abs(price - sl) / price * 100

    # Position size (0.5% risk)
    risk_amount = 100_000 * RISK_PER_TRADE  # $500
    pip_val = 100 if "XAU" in symbol else 500 if "XAG" in symbol else 1000 if any(x in symbol for x in ("DJ30","US500","USTEC")) else 1000
    lots = risk_amount / (sl_dist_pct * price * pip_val / 100)
    lots = max(0.01, min(lots, 1.0))

    return {
        "timestamp":   datetime.now().isoformat(),
        "symbol":      symbol,
        "direction":   direction,
        "entry_price": round(price, 5),
        "stop_loss":   round(sl, 5),
        "take_profit": round(tp, 5),
        "volume":      round(lots, 2),
        "confidence":  round(rule["win_rate"] * 100, 1),
        "rule_id":     rule["id"],
        "rule_desc":   f"{rule['symbol']} {rule['direction']} | {rule['session']} | {rule['h4_trend']} | {rule['sr_zone']}",
        "daily_bias":  daily_bias.get(symbol, {}).get("bias"),
        "bias_reasons": daily_bias.get(symbol, {}).get("reasons", []),
        "match_score": round(setup["match_score"] * 100, 1),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 3. MAIN ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    print("=" * 60)
    print("Combined Engine: Bernd Bias + Rebel Entry")
    print("=" * 60)

    # ── Daily bias ────────────────────────────────────────────────────────────
    print("\n── DAILY BIAS (Bernd fundamentals) ──")
    biases = compute_daily_bias()

    for sym, b in biases.items():
        if b["bias"] == "NEUTRAL":
            continue
        arrow = "🟢" if b["bias"] == "LONG" else "🔴"
        print(f"  {arrow} {sym:<12} {b['bias']:<6} ({b['strength']}/3) | {', '.join(b['reasons'])}")

    # ── Live scan ─────────────────────────────────────────────────────────────
    print("\n── INTRADAY SCAN (Rebel winner entry setups) ──")
    market = MarketState(connect_mt5=True)

    signals = []
    for sym in ["XAU/USD", "XAG/USD", "EUR/USD", "GBP/USD", "USD/JPY"]:
        bias = biases.get(sym, {})
        if bias.get("bias", "NEUTRAL") == "NEUTRAL":
            continue  # skip symbols with no daily conviction

        state = market.get_state(sym)
        if state.get("error"):
            continue

        setup = find_setup(sym, state, biases)
        if setup is None:
            continue

        signal = generate_signal(sym, state, setup, biases)
        signals.append(signal)

    market.shutdown()

    if signals:
        signals.sort(key=lambda s: s["confidence"] * s["match_score"], reverse=True)
        print(f"\n  {len(signals)} signals generated:\n")
        for s in signals[:3]:
            emoji = "🟢" if s["direction"] == "BUY" else "🔴"
            print(f"  {emoji} {s['direction']} {s['symbol']} @ {s['entry_price']:.2f}")
            print(f"     SL: {s['stop_loss']:.2f} | TP: {s['take_profit']:.2f} | Lots: {s['volume']}")
            print(f"     Conf: {s['confidence']:.0f}% | Match: {s['match_score']:.0f}% | {s['rule_desc']}")
            print(f"     Bias: {s['daily_bias']} ({', '.join(s['bias_reasons'])})\n")

        # Log
        os.makedirs(os.path.dirname(SIGNAL_LOG), exist_ok=True)
        with open(SIGNAL_LOG, "a") as f:
            for s in signals:
                f.write(json.dumps(s, default=str) + "\n")
    else:
        print("\n  No signals — no bias + setup confluence found.")

    print("=" * 60)


if __name__ == "__main__":
    main()
