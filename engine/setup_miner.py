"""
Setup Miner
Extracts recurring winning setups from top traders and codifies them as
tradeable rule cards.

Process:
  1. Enrich every winning trade with market context
  2. Cluster by similar context (symbol, direction, session, S/R proximity, trend)
  3. Rank clusters by: occurrences × win_rate × avg_rr
  4. Export as setup_rules.json → consumed by Signal Engine

Usage:
  python engine/setup_miner.py            # mine all setups
  python engine/setup_miner.py --top 50   # only top 50 ranked traders

Output:
  engine/setup_rules.json   — the rule database
  engine/setup_rules.html   — human-readable strategy cards
"""

import pandas as pd
import numpy as np
import json, os, sys
from datetime import datetime, timedelta
from collections import defaultdict

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR     = r"c:\Users\Administrator\Documents\Rebel Funding"
TRADES_FILE  = os.path.join(BASE_DIR, "master_trades.parquet")
NEWS_FILE    = os.path.join(BASE_DIR, "news_calendar.parquet")
CANDLE_DIR   = os.path.join(BASE_DIR, "candle_data")
SCORES_FILE  = os.path.join(BASE_DIR, "trader_scores.parquet")
ENRICHED_FILE = os.path.join(os.path.dirname(__file__), "enriched_trades.parquet")
OUTPUT_RULES = os.path.join(os.path.dirname(__file__), "setup_rules.json")

# ── Parameters ─────────────────────────────────────────────────────────────────
MIN_CLUSTER_SIZE  = 10      # minimum winning trades for a valid setup
MIN_WIN_RATE      = 0.60    # minimum win rate for a cluster to become a rule
TOP_TRADERS       = 100     # analyze only top N ranked traders
MIN_RR            = 1.0     # minimum average R:R

# Session detection
def get_session(hour):
    """Determine session from hour (0-23)."""
    if hour is None:
        return "unknown"
    if 0 <= hour < 9:
        return "Asia"
    elif 8 <= hour < 13:
        return "London"
    elif 13 <= hour < 17:
        return "London+NY"
    elif 17 <= hour < 22:
        return "NY"
    else:
        return "Off-hours"


# ── Load candles for context enrichment ────────────────────────────────────────
_candle_cache = {}

def load_candle(symbol, tf="H1"):
    key = f"{symbol}_{tf}"
    if key in _candle_cache:
        return _candle_cache[key]
    fname = symbol.replace("/", "_").replace(".", "_") + ".parquet"
    path  = os.path.join(CANDLE_DIR, tf, fname)
    if not os.path.exists(path):
        _candle_cache[key] = None
        return None
    df = pd.read_parquet(path)
    df["time"] = pd.to_datetime(df["time"])
    _candle_cache[key] = df.sort_values("time").reset_index(drop=True)
    return _candle_cache[key]


def trend_at(candles, dt, fast=20, slow=50):
    """H4 trend at a specific datetime."""
    if candles is None:
        return "neutral"
    idx = candles["time"].searchsorted(dt, side="right") - 1
    if idx < slow:
        return "neutral"
    window = candles.iloc[max(0, idx - slow):idx + 1]
    ma_f = window["close"].rolling(fast).mean().iloc[-1]
    ma_s = window["close"].rolling(slow).mean().iloc[-1]
    if ma_f > ma_s * 1.005:
        return "UP"
    elif ma_f < ma_s * 0.995:
        return "DOWN"
    return "neutral"


def sr_proximity(symbol, entry_price, dt):
    """Check if entry was near previous day high/low."""
    candles = load_candle(symbol, "H1")
    if candles is None:
        return None, None

    prev_day = (dt - timedelta(days=1)).date()
    prev = candles[candles["time"].dt.date == prev_day]
    if prev.empty:
        return None, None

    pdh = prev["high"].max()
    pdl = prev["low"].min()

    near_pdh = abs(entry_price - pdh) / entry_price < 0.005 if entry_price > 0 else False
    near_pdl = abs(entry_price - pdl) / entry_price < 0.005 if entry_price > 0 else False

    # Round number proximity
    if entry_price > 100:
        step = 50
    elif entry_price > 10:
        step = 5
    elif entry_price > 1:
        step = 0.5
    else:
        step = 0.05
    nearest_round = round(entry_price / step) * step
    near_round = abs(entry_price - nearest_round) / entry_price < 0.003

    if near_pdh:
        return "AT_PDH", pdh
    elif near_pdl:
        return "AT_PDL", pdl
    elif near_round:
        return f"AT_ROUND_{nearest_round}", nearest_round
    return "mid_range", None


def news_proximity(symbol, dt, window_hours=2):
    """Check if there was high-impact news near the trade time."""
    if not os.path.exists(NEWS_FILE):
        return None
    news = pd.read_parquet(NEWS_FILE)
    if "datetime" not in news.columns:
        return None
    news["datetime"] = pd.to_datetime(news["datetime"])
    news = news[news["impact"].str.upper() == "HIGH"]

    window_start = dt - timedelta(hours=window_hours)
    window_end = dt + timedelta(hours=window_hours)
    nearby = news[(news["datetime"] >= window_start) & (news["datetime"] <= window_end)]

    if nearby.empty:
        return None

    for _, ev in nearby.iterrows():
        instruments = str(ev.get("instruments", "")).split(",")
        if symbol in instruments or not instruments[0]:
            return {
                "event": ev["event"],
                "surprise": ev.get("surprise", ""),
                "time": str(ev["datetime"]),
            }
    return None


def parse_duration(dur_str):
    try:
        parts = str(dur_str).split(":")
        return int(parts[0]) * 60 + int(parts[1])
    except Exception:
        return None


def calc_rr(row):
    try:
        entry = float(row.get("open_price", 0))
        sl = float(row.get("stop_loss", 0))
        tp = float(row.get("take_profit", 0))
        sl_d = abs(entry - sl)
        tp_d = abs(entry - tp)
        return round(tp_d / sl_d, 2) if sl_d > 0 else None
    except Exception:
        return None


def enrich_trades(trades_df):
    """Vectorized enrichment: pre-load candles, process by symbol in batches."""
    print(f"  Enriching {len(trades_df):,} trades with context...")

    # ── Pre-load all candles once ─────────────────────────────────────────────
    symbols = trades_df["symbol"].unique()
    print(f"  Loading H4 candles for {len(symbols)} symbols...")
    h4_map = {}
    h1_map = {}
    for sym in symbols:
        h4_map[sym] = load_candle(sym, "H4")
        h1_map[sym] = load_candle(sym, "H1")

    # ── Vectorized: session from hour ────────────────────────────────────────
    trades_df = trades_df.copy()
    hours = pd.to_datetime(trades_df["opened"]).dt.hour
    trades_df["session"] = hours.apply(get_session)

    # ── Vectorized: is_win, direction, duration, rr ──────────────────────────
    trades_df["is_win"]    = trades_df["status"].str.upper() == "WIN"
    trades_df["direction"] = trades_df["direction"].str.upper()
    trades_df["duration_min"] = trades_df["duration"].apply(parse_duration)
    trades_df["rr"] = trades_df.apply(calc_rr, axis=1)
    trades_df["entry_price"] = pd.to_numeric(trades_df["open_price"], errors="coerce")

    # ── Per-symbol: trend at time ─────────────────────────────────────────────
    trends = {}
    print("  Computing H4 trends...")
    for i, sym in enumerate(symbols):
        if (i + 1) % 10 == 0:
            print(f"    {i+1}/{len(symbols)} symbols...")
        candles = h4_map.get(sym)
        if candles is None:
            trends[sym] = {}
            continue
        # Compute MA cross for all times efficiently
        ma_f = candles["close"].rolling(20).mean()
        ma_s = candles["close"].rolling(50).mean()
        trend_map = {}
        for idx in range(50, len(candles)):
            t = candles.iloc[idx]["time"]
            if ma_f.iloc[idx] > ma_s.iloc[idx] * 1.005:
                trend_map[t] = "UP"
            elif ma_f.iloc[idx] < ma_s.iloc[idx] * 0.995:
                trend_map[t] = "DOWN"
            else:
                trend_map[t] = "neutral"
        trends[sym] = trend_map

    def get_trend(sym, dt):
        tmap = trends.get(sym, {})
        if not tmap:
            return "neutral"
        # Find nearest candle time <= dt
        times = sorted(tmap.keys())
        idx = pd.Series(times).searchsorted(dt, side="right") - 1
        if idx < 0:
            return "neutral"
        return tmap.get(times[idx], "neutral")

    print("  Applying trends...")
    trades_df["h4_trend"] = trades_df.apply(
        lambda r: get_trend(r["symbol"], r["opened"]), axis=1
    )

    # ── Per-symbol: S/R proximity ────────────────────────────────────────────
    print("  Computing S/R proximity...")
    sr_results = []
    for sym in symbols:
        h1 = h1_map.get(sym)
        if h1 is None:
            continue
        sym_trades = trades_df[trades_df["symbol"] == sym]
        for _, t in sym_trades.iterrows():
            dt = t["opened"]
            price = t["entry_price"]
            if pd.isna(dt) or pd.isna(price) or price <= 0:
                sr_results.append({"index": t.name, "sr_zone": "unknown", "sr_level": None})
                continue
            prev_day = (dt - pd.Timedelta(days=1)).date()
            prev = h1[h1["time"].dt.date == prev_day]
            if prev.empty:
                sr_results.append({"index": t.name, "sr_zone": "mid_range", "sr_level": None})
                continue
            pdh = float(prev["high"].max())
            pdl = float(prev["low"].min())
            near_pdh = abs(price - pdh) / price < 0.005
            near_pdl = abs(price - pdl) / price < 0.005
            if near_pdh:
                sr_results.append({"index": t.name, "sr_zone": "AT_PDH", "sr_level": pdh})
            elif near_pdl:
                sr_results.append({"index": t.name, "sr_zone": "AT_PDL", "sr_level": pdl})
            else:
                sr_results.append({"index": t.name, "sr_zone": "mid_range", "sr_level": None})

    sr_df = pd.DataFrame(sr_results).set_index("index")
    trades_df["sr_zone"]  = sr_df["sr_zone"]
    trades_df["sr_level"] = sr_df["sr_level"]

    # ── News proximity ────────────────────────────────────────────────────────
    print("  Computing news proximity...")
    news_cache = {}
    if os.path.exists(NEWS_FILE):
        all_news = pd.read_parquet(NEWS_FILE)
        if "datetime" in all_news.columns:
            all_news["datetime"] = pd.to_datetime(all_news["datetime"])
            all_news = all_news[all_news["impact"].str.upper() == "HIGH"]
    else:
        all_news = pd.DataFrame()

    def get_news(sym, dt):
        if all_news.empty:
            return None, None
        ws = dt - pd.Timedelta(hours=2)
        we = dt + pd.Timedelta(hours=2)
        near = all_news[(all_news["datetime"] >= ws) & (all_news["datetime"] <= we)]
        if near.empty:
            return None, None
        for _, ev in near.iterrows():
            instruments = str(ev.get("instruments", "")).split(",")
            if sym in instruments or not instruments[0]:
                return ev["event"], ev.get("surprise", "")
        return None, None

    news_results = []
    for _, t in trades_df.iterrows():
        ev, surprise = get_news(t["symbol"], t["opened"])
        news_results.append({"index": t.name, "near_news": ev, "near_news_surprise": surprise})

    news_df = pd.DataFrame(news_results).set_index("index")
    trades_df["near_news"]           = news_df["near_news"]
    trades_df["near_news_surprise"]  = news_df["near_news_surprise"]

    # ── Keep only enriched columns ────────────────────────────────────────────
    cols = ["symbol", "direction", "session", "h4_trend", "sr_zone",
            "sr_level", "near_news", "near_news_surprise",
            "duration_min", "rr", "p_l", "is_win", "trader",
            "competition", "entry_price"]
    available = [c for c in cols if c in trades_df.columns]
    enriched = trades_df[available].copy()
    # Ensure p_l is float
    enriched["p_l"] = pd.to_numeric(trades_df.get("p_l", 0), errors="coerce")
    print(f"  Enrichment complete: {len(enriched):,} rows.")
    return enriched


def mine_setups(enriched, top_traders_only=TOP_TRADERS):
    """Cluster ALL trades from top traders, compute real win rate per cluster."""
    # Filter to top traders only (all their trades, wins AND losses)
    if os.path.exists(SCORES_FILE):
        scores = pd.read_parquet(SCORES_FILE)
        top = scores.head(top_traders_only)["trader"].tolist()
        subset = enriched[enriched["trader"].isin(top)]
        print(f"  Filtered to {len(subset):,} trades from top {top_traders_only} traders")
    else:
        subset = enriched
        print(f"  Using {len(subset):,} trades from all traders")

    # Cluster keys
    subset = subset.copy()
    subset["cluster_key"] = (
        subset["symbol"] + "|" +
        subset["direction"] + "|" +
        subset["session"] + "|" +
        subset["h4_trend"] + "|" +
        subset["sr_zone"].fillna("unknown")
    )

    clusters = subset.groupby("cluster_key")
    rules = []

    for key, grp in clusters:
        n = len(grp)
        if n < MIN_CLUSTER_SIZE:
            continue

        wr = grp["is_win"].mean()
        if wr < MIN_WIN_RATE:
            continue

        avg_rr = grp["rr"].dropna().mean()
        if avg_rr and avg_rr < MIN_RR:
            continue

        avg_dur = grp["duration_min"].dropna().mean()
        avg_pl  = grp["p_l"].mean()
        n_wins  = grp["is_win"].sum()
        n_loss  = n - n_wins

        parts = key.split("|")
        symbol, direction, session, trend, sr_zone = parts

        dur_sorted = grp["duration_min"].dropna().sort_values()
        fast_pct = (dur_sorted < dur_sorted.quantile(0.25)).mean() if not dur_sorted.empty else 0

        news_events = grp["near_news"].dropna()
        top_news = news_events.value_counts().head(3).to_dict() if not news_events.empty else {}

        # Quality = occurrences × (wr - 0.5) × avg_rr — penalizes 50/50 coinflips
        quality = n * max(wr - 0.5, 0) * (avg_rr if avg_rr and not np.isnan(avg_rr) else 1.0)

        rules.append({
            "id":               len(rules) + 1,
            "symbol":           symbol,
            "direction":        direction,
            "session":          session,
            "h4_trend":         trend,
            "sr_zone":          sr_zone,
            "occurrences":      int(n),
            "n_wins":           int(n_wins),
            "n_losses":         int(n_loss),
            "win_rate":         round(float(wr), 3),
            "avg_rr":           round(float(avg_rr), 2) if avg_rr and not np.isnan(avg_rr) else None,
            "avg_pl":           round(float(avg_pl), 2),
            "avg_dur_min":      round(float(avg_dur), 1) if avg_dur and not np.isnan(avg_dur) else None,
            "fast_entries_pct": round(float(fast_pct), 2),
            "top_news_triggers": top_news,
            "quality_score":    round(float(quality), 1),
        })

    rules.sort(key=lambda r: r["quality_score"], reverse=True)
    for i, r in enumerate(rules):
        r["id"] = i + 1

    return rules


def render_html(rules):
    """Generate a standalone strategy cards HTML."""
    cards = ""
    for r in rules:
        dir_color = "#3fb950" if r["direction"] == "BUY" else "#f85149"
        cards += f"""
        <div class="card">
          <div class="header">
            <span class="id">#{r['id']:03d}</span>
            <span class="symbol">{r['symbol']}</span>
            <span class="dir" style="color:{dir_color}">{r['direction']}</span>
            <span class="quality">{r['quality_score']:.0f}</span>
          </div>
          <div class="body">
            <div class="kpi"><b>{r['occurrences']}</b> trades</div>
            <div class="kpi"><b>{r['win_rate']:.0%}</b> WR</div>
            <div class="kpi"><b>R:R {r['avg_rr']}</b></div>
            <div class="kpi"><b>{r['avg_dur_min']:.0f}m</b> avg</div>
          </div>
          <div class="context">
            Session: {r['session']} | Trend: {r['h4_trend']} | Zone: {r['sr_zone']}
          </div>
          {"<div class='news'>News trigger: " + ", ".join(f"{k} ({v}x)" for k,v in r.get('top_news_triggers',{}).items()) + "</div>" if r.get('top_news_triggers') else ""}
        </div>"""

    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><title>Setup Rules</title>
<style>
  *{{margin:0;padding:0;box-sizing:border-box}}
  body{{font-family:Segoe UI,sans-serif;background:#0d1117;color:#c9d1d9;padding:20px}}
  h1{{color:#58a6ff;margin-bottom:6px}}
  .sub{{color:#8b949e;font-size:12px;margin-bottom:20px}}
  .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(380px,1fr));gap:14px}}
  .card{{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:16px}}
  .header{{display:flex;gap:10px;align-items:center;margin-bottom:10px}}
  .id{{color:#8b949e;font-size:11px}}
  .symbol{{font-weight:700;font-size:15px;color:#e6edf3}}
  .dir{{font-weight:700;font-size:14px}}
  .quality{{margin-left:auto;background:#1c2a3a;color:#79c0ff;padding:2px 10px;border-radius:12px;font-size:12px;font-weight:600}}
  .body{{display:flex;gap:16px;margin-bottom:10px}}
  .kpi{{font-size:13px;color:#8b949e}}
  .kpi b{{color:#e6edf3}}
  .context{{font-size:12px;color:#8b949e;background:#21262d;padding:6px 10px;border-radius:6px;margin-bottom:6px}}
  .news{{font-size:11px;color:#3fb950;background:#1a2a1a;padding:4px 10px;border-radius:6px}}
</style></head><body>
<h1>Setup Rules Database</h1>
<div class="sub">{len(rules)} rules mined from top trader winning setups</div>
<div class="grid">{cards}</div>
</body></html>"""


def main():
    print("=" * 60)
    print("Setup Miner — Extracting winning patterns from top traders")
    print("=" * 60 + "\n")

    # Load or compute enrichment
    if os.path.exists(ENRICHED_FILE):
        print(f"Loading cached enrichment from {ENRICHED_FILE}")
        enriched = pd.read_parquet(ENRICHED_FILE)
        print(f"Loaded {len(enriched):,} enriched trades")
    else:
        trades = pd.read_parquet(TRADES_FILE)
        trades["opened"] = pd.to_datetime(trades["opened"])
        print(f"Loaded {len(trades):,} trades")
        enriched = enrich_trades(trades)
        print(f"(Run engine/enricher.py separately to cache this for faster reloads)")

    # Mine setups
    print(f"\n  Mining setups (min {MIN_CLUSTER_SIZE} trades, {MIN_WIN_RATE:.0%} WR)...")
    rules = mine_setups(enriched)

    if not rules:
        print("  No rules found. Increase data or lower thresholds.")
        return

    # Save
    os.makedirs(os.path.dirname(OUTPUT_RULES), exist_ok=True)
    with open(OUTPUT_RULES, "w") as f:
        json.dump(rules, f, indent=2, default=str)

    html_path = OUTPUT_RULES.replace(".json", ".html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(render_html(rules))

    # Print top rules
    print(f"\n{'=' * 80}")
    print(f"TOP SETUP RULES ({len(rules)} total)")
    print(f"{'=' * 80}")
    print(f"{'#':>3}  {'Symbol':<12} {'Dir':<5} {'Session':<10} {'Trend':<6} {'Zone':<15} {'N':>4}  {'WR':>6}  {'R:R':>6}  {'Score':>7}")
    print("-" * 80)
    for r in rules[:25]:
        print(f"{r['id']:>3}  {r['symbol']:<12} {r['direction']:<5} {r['session']:<10} "
              f"{r['h4_trend']:<6} {r['sr_zone']:<15} {r['occurrences']:>4}  "
              f"{r['win_rate']:>5.0%}  {r['avg_rr'] or 'n/a':>6}  {r['quality_score']:>7.0f}")

    print(f"\nSaved → {OUTPUT_RULES}")
    print(f"Saved → {html_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
