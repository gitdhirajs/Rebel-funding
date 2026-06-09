"""
Bernd Strategy Backtest
========================
Walks through historical D1 candle data day-by-day,
runs Bernd's zone detection + bias synthesis,
tracks every signal and its outcome.

Output: engine/backtest_results.parquet + printed performance report
"""

import pandas as pd, numpy as np, os, sys
from datetime import datetime, timedelta

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR    = r"c:\Users\Administrator\Documents\Rebel Funding"
CANDLE_DIR  = os.path.join(BASE_DIR, "candle_data", "D1")
COT_FILE    = os.path.join(os.path.dirname(__file__), "cot_simulated.parquet")
SEASON_FILE = os.path.join(os.path.dirname(__file__), "seasonality.parquet")
OUTPUT_FILE = os.path.join(os.path.dirname(__file__), "backtest_results.parquet")

SYMBOLS     = ["XAU/USD", "XAG/USD", "EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD", "USD/CAD"]
BACKTEST_START = "2025-07-01"
BACKTEST_END   = "2026-05-31"

# Challenge rules
RISK_PER_TRADE   = 0.005   # 0.5%
MAX_DAILY_LOSS   = 0.05
MAX_DRAWDOWN     = 0.10
MIN_CONFIDENCE   = 60

# ═══════════════════════════════════════════════════════════════════════════════
# Simplified Bernd strategy (zone detection + COT + seasonality)
# ═══════════════════════════════════════════════════════════════════════════════

def body_pct(o, h, l, c):
    r = h - l
    return abs(c - o) / r if r > 0 else 0

def detect_zones_hist(df, idx):
    """Detect zones using candles up to index idx. Returns nearest demand/supply."""
    data = df.iloc[:idx+1]
    if len(data) < 30:
        return None, None

    zones = []
    for i in range(25, len(data)):
        window = data.iloc[i-25:i+1]
        # Find base: 1-4 indecisive candles
        b_start = i - 3
        while b_start >= i - 20 and body_pct(
            window.iloc[b_start - (i-25)]["open"] if b_start >= 0 else 0,
            window.iloc[b_start - (i-25)]["high"] if b_start >= 0 else 0,
            window.iloc[b_start - (i-25)]["low"]  if b_start >= 0 else 0,
            window.iloc[b_start - (i-25)]["close"]if b_start >= 0 else 0
        ) > 0.50:
            b_start -= 1
        if b_start >= i - 3:
            continue

        # Check 2+ leg-in candles
        if b_start < i - 5:
            continue

        # Explosive leg-out
        lo = data.iloc[i]
        lo_body = body_pct(lo["open"], lo["high"], lo["low"], lo["close"])
        if lo_body < 0.60:
            continue

        # Determine zone
        base_candles = data.iloc[b_start+1:i]
        if len(base_candles) < 1 or len(base_candles) > 6:
            continue

        legin_dir = "up" if data.iloc[b_start]["close"] > data.iloc[b_start]["open"] else "down"
        lo_dir    = "up" if lo["close"] > lo["open"] else "down"

        if legin_dir == "down" and lo_dir == "up":
            zone_type = "demand"
            proximal = max(max(c["open"], c["close"]) for _, c in base_candles.iterrows())
            distal   = min(c["low"] for _, c in base_candles.iterrows())
            distal   = min(distal, lo["low"])
        elif legin_dir == "down" and lo_dir == "down":
            zone_type = "supply"
            proximal = min(min(c["open"], c["close"]) for _, c in base_candles.iterrows())
            distal   = max(c["high"] for _, c in base_candles.iterrows())
            distal   = max(distal, lo["high"])
        elif legin_dir == "up" and lo_dir == "up":
            zone_type = "demand"
            proximal = max(max(c["open"], c["close"]) for _, c in base_candles.iterrows())
            distal   = min(c["low"] for _, c in base_candles.iterrows())
            distal   = min(distal, lo["low"])
        elif legin_dir == "up" and lo_dir == "down":
            zone_type = "supply"
            proximal = min(min(c["open"], c["close"]) for _, c in base_candles.iterrows())
            distal   = max(c["high"] for _, c in base_candles.iterrows())
            distal   = max(distal, lo["high"])
        else:
            continue

        zones.append({"type": zone_type, "proximal": proximal, "distal": distal, "date": data.index[i]})

    # Nearest zones to price at idx
    price = float(data.iloc[-1]["close"])
    demands = [z for z in zones if z["type"] == "demand" and z["proximal"] < price * 1.05]
    supplies = [z for z in zones if z["type"] == "supply" and z["proximal"] > price * 0.95]

    nd = max(demands, key=lambda z: z["proximal"]) if demands else None
    ns = min(supplies, key=lambda z: z["proximal"]) if supplies else None
    return nd, ns


def get_location(price, nearest_d, nearest_s):
    if nearest_d and nearest_s:
        rng = nearest_s["proximal"] - nearest_d["proximal"]
        if rng <= 0:
            return "equilibrium", None
        pos = (price - nearest_d["proximal"]) / rng
        if pos <= 0.33:
            return "cheap", "LONG"
        elif pos >= 0.67:
            return "expensive", "SHORT"
        return "equilibrium", None
    if nearest_d and (price - nearest_d["proximal"]) / price < 0.02:
        return "cheap", "LONG"
    if nearest_s and (nearest_s["proximal"] - price) / price < 0.02:
        return "expensive", "SHORT"
    return "equilibrium", None


def load_cot_season(symbol, dt):
    cot_bias, seas_bias = "neutral", "neutral"
    dt_naive = pd.Timestamp(dt).tz_localize(None)
    if os.path.exists(COT_FILE):
        cot_df = pd.read_parquet(COT_FILE)
        if "date" in cot_df.columns:
            cot_df["date"] = pd.to_datetime(cot_df["date"]).dt.tz_localize(None)
        cot_row = cot_df[(cot_df["symbol"] == symbol) & (cot_df["date"] <= dt_naive)]
        if not cot_row.empty:
            idx = cot_row.iloc[-1]["cot_index"]
            cot_bias = "bullish" if idx > 20 else "bearish" if idx < -20 else "neutral"
    if os.path.exists(SEASON_FILE):
        sdf = pd.read_parquet(SEASON_FILE)
        month = dt.month
        sr = sdf[(sdf["symbol"] == symbol) & (sdf["month"] == month)]
        if not sr.empty:
            seas_bias = sr.iloc[0]["bias"].lower()
    return cot_bias, seas_bias


def should_trade(location, direction, cot_bias, seas_bias, trend):
    """Simplified Bernd hierarchy."""
    if location == "equilibrium":
        return False, 0

    conform = 1  # location counts
    if (direction == "LONG" and cot_bias == "bullish") or (direction == "SHORT" and cot_bias == "bearish"):
        conform += 1
    if (direction == "LONG" and seas_bias == "bullish") or (direction == "SHORT" and seas_bias == "bearish"):
        conform += 1
    if (direction == "LONG" and trend == "UP") or (direction == "SHORT" and trend == "DOWN"):
        conform += 1

    # Counter-trend check
    counter = (direction == "LONG" and trend == "DOWN") or (direction == "SHORT" and trend == "UP")
    if counter and conform < 3:
        return False, conform

    confidence = conform * 25
    return conform >= 2, confidence


# ═══════════════════════════════════════════════════════════════════════════════
# BACKTEST ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

def backtest_symbol(symbol):
    fname = symbol.replace("/", "_").replace(".", "_") + ".parquet"
    path  = os.path.join(CANDLE_DIR, fname)
    if not os.path.exists(path):
        print(f"  {symbol}: No D1 data")
        return []

    df = pd.read_parquet(path)
    df["time"] = pd.to_datetime(df["time"])
    df = df.set_index("time").sort_index()
    df = df[(df.index >= BACKTEST_START) & (df.index <= BACKTEST_END)]
    if len(df) < 50:
        return []

    df["ma20"] = df["close"].rolling(20).mean()
    df["ma50"] = df["close"].rolling(50).mean()

    trades = []
    active_trade = None

    for i in range(50, len(df)):
        today     = df.index[i]
        price     = float(df.iloc[i]["close"])
        ma20      = float(df.iloc[i]["ma20"])
        ma50      = float(df.iloc[i]["ma50"])
        trend     = "UP" if ma20 > ma50 * 1.005 else "DOWN" if ma20 < ma50 * 0.995 else "neutral"

        # Check if existing trade hits SL or TP
        if active_trade:
            lo = float(df.iloc[i]["low"])
            hi = float(df.iloc[i]["high"])
            sl = active_trade["sl"]
            tp = active_trade["tp"]
            direction = active_trade["direction"]

            hit_sl = (direction == "LONG" and lo <= sl) or (direction == "SHORT" and hi >= sl)
            hit_tp = (direction == "LONG" and hi >= tp) or (direction == "SHORT" and lo <= tp)

            if hit_sl or hit_tp:
                active_trade["exit_date"] = today
                active_trade["exit_price"] = sl if hit_sl else tp
                active_trade["result"] = "WIN" if hit_tp else "LOSS"
                pnl_pct = abs(tp - active_trade["entry"]) / active_trade["entry"] if hit_tp else \
                          -abs(sl - active_trade["entry"]) / active_trade["entry"]
                active_trade["pnl_pct"] = round(pnl_pct * 100, 3)
                trades.append(active_trade)
                active_trade = None

        if active_trade:
            continue  # still in trade

        # Generate new signal
        nd, ns = detect_zones_hist(df.reset_index(), i)
        if nd is None and ns is None:
            continue

        location, direction = get_location(price, nd, ns)
        if direction is None:
            continue

        cot_bias, seas_bias = load_cot_season(symbol, today)
        ok, confidence = should_trade(location, direction, cot_bias, seas_bias, trend)
        if not ok or confidence < MIN_CONFIDENCE:
            continue

        # Entry at next candle open (or current close for simplicity)
        entry = price
        if direction == "LONG":
            sl = nd["distal"] if nd else price * 0.99
            tp = ns["proximal"] if ns else price + (price - sl) * 2
        else:
            sl = ns["distal"] if ns else price * 1.01
            tp = nd["proximal"] if nd else price - (sl - price) * 2

        rr = abs(tp - entry) / abs(sl - entry) if abs(sl - entry) > 0 else 0
        if rr < 1.5:
            continue  # minimum 1:1.5 R:R

        active_trade = {
            "symbol":     symbol,
            "entry_date": today,
            "direction":  direction,
            "entry":      round(entry, 5),
            "sl":         round(sl, 5),
            "tp":         round(tp, 5),
            "rr":         round(rr, 2),
            "confidence": confidence,
            "location":   location,
            "trend":      trend,
            "cot":        cot_bias,
            "seasonality":seas_bias,
        }

    if active_trade:
        active_trade["exit_date"] = df.index[-1]
        active_trade["exit_price"] = float(df.iloc[-1]["close"])
        pnl = (active_trade["exit_price"] - active_trade["entry"]) / active_trade["entry"]
        if active_trade["direction"] == "SHORT":
            pnl = -pnl
        active_trade["result"] = "WIN" if pnl > 0 else "LOSS"
        active_trade["pnl_pct"] = round(pnl * 100, 3)
        trades.append(active_trade)

    return trades


def main():
    print("=" * 60)
    print("Bernd Strategy Backtest")
    print(f"Period: {BACKTEST_START} -> {BACKTEST_END}")
    print(f"Symbols: {len(SYMBOLS)}")
    print("=" * 60 + "\n")

    all_trades = []
    for sym in SYMBOLS:
        print(f"  Testing {sym} ...", end=" ", flush=True)
        trades = backtest_symbol(sym)
        all_trades.extend(trades)
        wins = sum(1 for t in trades if t["result"] == "WIN")
        print(f"{len(trades)} trades | {wins} wins | "
              f"{wins/max(len(trades),1)*100:.0f}% WR" if trades else "no trades")

    if not all_trades:
        print("\nNo trades generated across any symbol.")
        return

    df = pd.DataFrame(all_trades)
    df = df.sort_values("entry_date").reset_index(drop=True)
    df.to_parquet(OUTPUT_FILE, index=False)

    # Performance metrics
    wins   = df[df["result"] == "WIN"]
    losses = df[df["result"] == "LOSS"]
    wr     = len(wins) / len(df) * 100
    avg_win  = wins["pnl_pct"].mean()  if len(wins)  > 0 else 0
    avg_loss = losses["pnl_pct"].mean()if len(losses)> 0 else 0
    profit_factor = abs(avg_win / avg_loss) if avg_loss != 0 else float("inf")
    expectancy = (wr/100 * avg_win + (1-wr/100) * avg_loss)
    total_pnl  = df["pnl_pct"].sum()

    # Drawdown simulation (max 2 concurrent trades)
    daily = df.groupby(df["entry_date"].dt.date)["pnl_pct"].sum()
    cum   = daily.cumsum()
    peak  = cum.cummax()
    dd    = (cum - peak)
    max_dd = dd.min()

    print(f"\n{'=' * 60}")
    print(f"BACKTEST RESULTS")
    print(f"{'=' * 60}")
    print(f"  Total trades:     {len(df)}")
    print(f"  Wins:             {len(wins)} ({wr:.0f}%)")
    print(f"  Losses:           {len(losses)} ({100-wr:.0f}%)")
    print(f"  Avg win:          {avg_win:+.2f}%")
    print(f"  Avg loss:         {avg_loss:+.2f}%")
    print(f"  Profit factor:    {profit_factor:.2f}")
    print(f"  Expectancy:       {expectancy:+.3f}% per trade")
    print(f"  Total P&L:        {total_pnl:+.2f}%")
    print(f"  Max drawdown:     {max_dd:+.2f}%")

    # Monthly breakdown
    df["month"] = df["entry_date"].dt.strftime("%Y-%m")
    monthly = df.groupby("month").agg(
        trades=("result", "count"),
        wins=("result", lambda x: (x=="WIN").sum()),
        wr=("result", lambda x: (x=="WIN").mean()*100),
        pnl=("pnl_pct", "sum"),
    )

    print(f"\n  Monthly Performance:")
    print(f"  {'Month':<8} {'Trades':>6} {'Wins':>5} {'WR':>6} {'P&L%':>8}")
    print(f"  {'-'*35}")
    for _, r in monthly.iterrows():
        print(f"  {r.name:<8} {int(r['trades']):>6} {int(r['wins']):>5} {r['wr']:>5.0f}% {r['pnl']:>+7.2f}%")

    # Per-symbol
    print(f"\n  Per Symbol:")
    for sym in SYMBOLS:
        s = df[df["symbol"] == sym]
        if len(s) == 0: continue
        w = (s["result"] == "WIN").sum()
        print(f"    {sym:<12} {len(s):>3} trades | {w}W/{len(s)-w}L | {w/len(s)*100:.0f}% WR | P&L:{s['pnl_pct'].sum():+.2f}%")

    # Challenge viability
    print(f"\n{'=' * 60}")
    print(f"CHALLENGE VIABILITY")
    print(f"{'=' * 60}")
    if max_dd > -MAX_DRAWDOWN * 100:
        print(f"  ✅ Max drawdown {max_dd:+.1f}% within 10% limit")
    else:
        print(f"  ❌ Max drawdown {max_dd:+.1f}% EXCEEDS 10% limit")
    if total_pnl > 0:
        print(f"  ✅ Total P&L positive ({total_pnl:+.1f}%)")
    else:
        print(f"  ❌ Total P&L negative ({total_pnl:+.1f}%)")

    print(f"\n  Saved -> {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
