"""
Combined Engine Backtest
=========================
Tests: Bernd daily bias + Rebel intraday entries on historical H1 data.
Fixed risk: 0.5% SL, 1:2 R:R per trade.
Tracks win rate, P&L, drawdown, challenge viability.
"""

import pandas as pd, numpy as np, json, os, sys
from datetime import datetime, timedelta

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR     = r"c:\Users\Administrator\Documents\Rebel Funding"
CANDLE_DIR   = os.path.join(BASE_DIR, "candle_data", "H1")
COT_FILE     = os.path.join(os.path.dirname(__file__), "cot_simulated.parquet")
SEASON_FILE  = os.path.join(os.path.dirname(__file__), "seasonality.parquet")
SETUP_FILE   = os.path.join(os.path.dirname(__file__), "setup_rules.json")
OUTPUT_FILE  = os.path.join(os.path.dirname(__file__), "backtest_combined.parquet")

SYMBOLS = ["XAU/USD", "XAG/USD", "EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD", "USD/CAD"]
START_DATE = "2025-07-01"
END_DATE   = "2026-05-31"
RISK = 0.005  # 0.5% SL
MIN_RR = 1.5
CHECK_INTERVAL_HOURS = 1  # check every hour for more signals

def load_setup_rules():
    if not os.path.exists(SETUP_FILE): return []
    with open(SETUP_FILE) as f:
        return json.load(f)

def load_candles(sym):
    fname = sym.replace("/","_").replace(".","_") + ".parquet"
    path = os.path.join(CANDLE_DIR, fname)
    if not os.path.exists(path): return None
    df = pd.read_parquet(path)
    df["time"] = pd.to_datetime(df["time"])
    return df.set_index("time").sort_index()

def session(hour):
    if 0 <= hour < 9: return "Asia"
    if 8 <= hour < 13: return "London"
    if 13 <= hour < 17: return "London+NY"
    if 17 <= hour < 22: return "NY"
    return "Off-hours"

def compute_daily_bias_hist(symbol, dt):
    """Compute bias as of historical date dt."""
    votes = {"LONG": 0, "SHORT": 0}
    month = dt.month
    dt_naive = pd.Timestamp(dt).tz_localize(None)

    # COT
    if os.path.exists(COT_FILE):
        cot_df = pd.read_parquet(COT_FILE)
        if "date" in cot_df.columns:
            cot_df["date"] = pd.to_datetime(cot_df["date"]).dt.tz_localize(None)
        cot_row = cot_df[(cot_df["symbol"] == symbol) & (cot_df["date"] <= dt_naive)]
        if not cot_row.empty:
            idx = cot_row.iloc[-1]["cot_index"]
            if idx > 20: votes["LONG"] += 1
            elif idx < -20: votes["SHORT"] += 1

    # Seasonality
    if os.path.exists(SEASON_FILE):
        sdf = pd.read_parquet(SEASON_FILE)
        sr = sdf[(sdf["symbol"] == symbol) & (sdf["month"] == month)]
        if not sr.empty:
            b = sr.iloc[0]["bias"]
            if b == "BULLISH": votes["LONG"] += 1
            elif b == "BEARISH": votes["SHORT"] += 1

    # Rebel consensus from setup rules
    rules = load_setup_rules()
    sym_rules = [r for r in rules if r["symbol"] == symbol
                 and r.get("occurrences", 0) >= 15
                 and r.get("win_rate", 0) >= 0.60]
    if sym_rules:
        buy_count  = sum(r["occurrences"] for r in sym_rules if r["direction"] == "BUY")
        sell_count = sum(r["occurrences"] for r in sym_rules if r["direction"] == "SELL")
        if buy_count > sell_count: votes["LONG"] += 1
        elif sell_count > buy_count: votes["SHORT"] += 1

    if votes["LONG"] > votes["SHORT"]: return "LONG"
    if votes["SHORT"] > votes["LONG"]: return "SHORT"
    return "NEUTRAL"


def backtest_symbol(sym):
    df = load_candles(sym)
    if df is None or len(df) < 100: return []

    df = df[(df.index >= START_DATE) & (df.index <= END_DATE)]
    if len(df) < 100: return []

    # Pre-calc indicators
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma50"] = df["close"].rolling(50).mean()
    # Previous day H/L
    df["date"] = df.index.date
    daily = df.groupby("date").agg(pdh=("high", "max"), pdl=("low", "min"))
    df = df.join(daily, on="date")

    rules = load_setup_rules()
    trades = []
    active = None

    for i, (idx, row) in enumerate(df.iterrows()):
        if i < 50: continue  # warm-up

        # Check if any active trade hits SL or TP
        if active:
            lo, hi = row["low"], row["high"]
            sl, tp = active["sl"], active["tp"]
            d = active["direction"]
            hit_sl = (d == "LONG" and lo <= sl) or (d == "SHORT" and hi >= sl)
            hit_tp = (d == "LONG" and hi >= tp) or (d == "SHORT" and lo <= tp)
            if hit_sl or hit_tp:
                active["exit_time"] = idx
                active["exit_price"] = sl if hit_sl else tp
                active["result"] = "WIN" if hit_tp else "LOSS"
                active["pnl_r"] = 2.0 if hit_tp else -1.0  # 1:2 R:R
                trades.append(active)
                active = None
            continue

        # Only check every N hours
        if i % CHECK_INTERVAL_HOURS != 0:
            continue

        # Compute daily bias
        bias = compute_daily_bias_hist(sym, idx)
        if bias == "NEUTRAL":
            continue

        bias_dir = "BUY" if bias == "LONG" else "SELL"
        price = row["close"]
        hour = idx.hour
        sess = session(hour)

        # H4 trend
        ma20 = row["ma20"]; ma50 = row["ma50"]
        trend = "UP" if ma20 > ma50 * 1.005 else "DOWN" if ma20 < ma50 * 0.995 else "neutral"

        # S/R proximity
        pdh = row.get("pdh"); pdl = row.get("pdl")
        near_pdh = pdh and abs(price - pdh) / price < 0.005
        near_pdl = pdl and abs(price - pdl) / price < 0.005
        sr_zone = "AT_PDH" if near_pdh else "AT_PDL" if near_pdl else "mid_range"

        # Find matching rebel rule (boosts confidence, doesn't gate)
        best_score = 0
        best_rule = None
        for r in rules:
            if r.get("direction") != bias_dir: continue
            if r.get("symbol") != sym: continue
            if r.get("occurrences", 0) < 10: continue
            if r.get("win_rate", 0) < 0.60: continue

            score = 0
            if r.get("session") in (sess, ""): score += 1
            if r.get("h4_trend", "neutral") in (trend, "neutral"): score += 1
            if r.get("sr_zone", "mid_range") in (sr_zone, "mid_range"): score += 1
            score *= r.get("win_rate", 0.5)

            if score > best_score:
                best_score = score
                best_rule = r

        # Entry conditions: trend aligned OR near S/R (not both required)
        trend_ok = (bias_dir == "BUY" and trend == "UP") or (bias_dir == "SELL" and trend == "DOWN")
        sr_ok    = sr_zone in ("AT_PDH", "AT_PDL")
        if not trend_ok and not sr_ok:
            continue  # need at least one technical confirmation

        # Confidence from rule match
        rule_bonus = best_score if best_rule else 0
        confidence = (1 if trend_ok else 0) + (1 if sr_ok else 0) + (1 if rule_bonus > 0.5 else 0)

        # Generate trade
        avg_rr = best_rule.get("avg_rr") or 2.0 if best_rule else 2.0
        sl_pct = RISK  # 0.5%
        if bias_dir == "BUY":
            sl = price * (1 - sl_pct)
            tp = price * (1 + sl_pct * avg_rr)
        else:
            sl = price * (1 + sl_pct)
            tp = price * (1 - sl_pct * avg_rr)

        active = {
            "symbol": sym, "direction": bias_dir,
            "entry_time": idx, "entry": round(price, 5),
            "sl": round(sl, 5), "tp": round(tp, 5),
            "bias": bias, "rule_id": best_rule["id"] if best_rule else 0,
            "session": sess, "trend": trend, "sr_zone": sr_zone,
            "rule_wr": best_rule["win_rate"] if best_rule else 0,
            "confidence": confidence,
        }

    if active:
        active["exit_time"] = df.index[-1]
        px = float(df.iloc[-1]["close"])
        active["exit_price"] = px
        won = (active["direction"] == "LONG" and px > active["entry"]) or \
              (active["direction"] == "SHORT" and px < active["entry"])
        active["result"] = "WIN" if won else "LOSS"
        active["pnl_r"] = 2.0 if won else -1.0
        trades.append(active)

    return trades


def main():
    print("=" * 60)
    print("Combined Engine Backtest")
    print(f"Period: {START_DATE} -> {END_DATE}")
    print(f"Risk: {RISK*100:.1f}% SL | Min R:R: {MIN_RR}")
    print("=" * 60 + "\n")

    all_trades = []
    for sym in SYMBOLS:
        print(f"  {sym} ...", end=" ", flush=True)
        trades = backtest_symbol(sym)
        all_trades.extend(trades)
        w = sum(1 for t in trades if t["result"] == "WIN")
        print(f"{len(trades)} trades, {w}W/{len(trades)-w}L" if trades else "no trades")

    if not all_trades:
        print("\nNo trades. Adjust parameters or check data.")
        return

    df = pd.DataFrame(all_trades)
    df = df.sort_values("entry_time").reset_index(drop=True)
    df.to_parquet(OUTPUT_FILE, index=False)

    wins   = df[df["result"] == "WIN"]
    losses = df[df["result"] == "LOSS"]
    wr     = len(wins) / len(df) * 100
    total_r = df["pnl_r"].sum()

    # Daily P&L for drawdown
    df["date"] = df["entry_time"].dt.date
    daily = df.groupby("date")["pnl_r"].sum()
    cum = daily.cumsum()
    peak = cum.cummax()
    dd = cum - peak
    max_dd_r = dd.min()

    print(f"\n{'=' * 60}")
    print(f"BACKTEST RESULTS (Combined Engine)")
    print(f"{'=' * 60}")
    print(f"  Total trades:   {len(df)}")
    print(f"  Win rate:       {wr:.1f}% ({len(wins)}W / {len(losses)}L)")
    print(f"  Profit factor:  {abs(wins['pnl_r'].sum() / losses['pnl_r'].sum()):.2f}" if len(losses) > 0 else "  Profit factor:  ∞ (no losses)")
    print(f"  Total R:        {total_r:+.1f}R")
    print(f"  Max drawdown:   {max_dd_r:+.1f}R")

    # 1R = 0.5% of account, so convert to %
    pnl_pct = total_r * RISK * 100
    dd_pct  = abs(max_dd_r) * RISK * 100
    print(f"\n  Total P&L:      {pnl_pct:+.1f}% (at {RISK*100:.1f}% risk/R)")
    print(f"  Max drawdown:   {dd_pct:.1f}% {'✅' if dd_pct < 10 else '❌'}")

    # Monthly
    df["month"] = df["entry_time"].dt.strftime("%Y-%m")
    monthly = df.groupby("month").agg(
        trades=("result", "count"),
        wins=("result", lambda x: (x == "WIN").sum()),
        wr=("result", lambda x: (x == "WIN").mean() * 100),
        r=("pnl_r", "sum"),
    )
    print(f"\n  Monthly:")
    for _, r in monthly.iterrows():
        pnl_m = r["r"] * RISK * 100
        print(f"    {r.name}: {int(r['trades']):>3} trades | {int(r['wins']):>2}W | {r['wr']:.0f}% WR | {pnl_m:+.1f}%")

    # By symbol
    print(f"\n  By Symbol:")
    for sym in SYMBOLS:
        s = df[df["symbol"] == sym]
        if len(s) == 0: continue
        w = (s["result"] == "WIN").sum()
        print(f"    {sym:<12} {len(s):>3} trades | {w}/{len(s)} wins | {w/len(s)*100:.0f}% WR | {s['pnl_r'].sum():+.1f}R")

    # By bias
    print(f"\n  By Bias:")
    for bias in ["LONG", "SHORT"]:
        s = df[df["bias"] == bias]
        if len(s) == 0: continue
        w = (s["result"] == "WIN").sum()
        print(f"    {bias:<6} {len(s):>3} trades | {w}/{len(s)} wins | {w/len(s)*100:.0f}% WR")

    # Challenge check
    print(f"\n{'=' * 60}")
    print(f"CHALLENGE VIABILITY ({RISK*100:.1f}% risk per trade)")
    print(f"{'=' * 60}")
    if dd_pct <= 10:
        print(f"  ✅ Drawdown {dd_pct:.1f}% within 10% limit")
    else:
        print(f"  ❌ Drawdown {dd_pct:.1f}% exceeds 10% limit")
    if pnl_pct > 0:
        print(f"  ✅ Total P&L positive (+{pnl_pct:.1f}%)")
    else:
        print(f"  ❌ Total P&L negative ({pnl_pct:.1f}%)")
    if wr >= 50:
        print(f"  ✅ Win rate {wr:.0f}% >= 50%")
    else:
        print(f"  ❌ Win rate {wr:.0f}% < 50%")

    print(f"\n  Saved -> {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
