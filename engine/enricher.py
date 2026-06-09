"""
One-time enrichment: adds market context to every trade and saves to parquet.
Runs once, then setup_miner reads the cached result instantly.

Run: python engine/enricher.py
Output: engine/enriched_trades.parquet (~5 minutes for 118K trades)
"""

import pandas as pd, numpy as np, os, sys
from datetime import timedelta

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR    = r"c:\Users\Administrator\Documents\Rebel Funding"
TRADES_FILE = os.path.join(BASE_DIR, "master_trades.parquet")
NEWS_FILE   = os.path.join(BASE_DIR, "news_calendar.parquet")
CANDLE_DIR  = os.path.join(BASE_DIR, "candle_data")
OUTPUT_FILE = os.path.join(os.path.dirname(__file__), "enriched_trades.parquet")

def get_session(hour):
    if hour is None: return "unknown"
    if 0 <= hour < 9:  return "Asia"
    if 8 <= hour < 13: return "London"
    if 13 <= hour < 17: return "London+NY"
    if 17 <= hour < 22: return "NY"
    return "Off-hours"

def load_candle(symbol, tf="H1"):
    fname = symbol.replace("/", "_").replace(".", "_") + ".parquet"
    path = os.path.join(CANDLE_DIR, tf, fname)
    if not os.path.exists(path): return None
    df = pd.read_parquet(path)
    df["time"] = pd.to_datetime(df["time"])
    return df.sort_values("time").reset_index(drop=True)

def parse_dur(d): 
    try: p = str(d).split(":"); return int(p[0])*60+int(p[1])
    except: return None

def calc_rr(row):
    try:
        e = float(row.get("open_price", 0)); s = float(row.get("stop_loss", 0))
        t = float(row.get("take_profit", 0))
        sd = abs(e-s); td = abs(e-t)
        return round(td/sd, 2) if sd > 0 else None
    except: return None

print("=" * 60)
print("Trade Enricher — one-time context enrichment")
print("=" * 60)

# Load
t = pd.read_parquet(TRADES_FILE)
t["opened"] = pd.to_datetime(t["opened"])
print(f"Trades: {len(t):,}")

# Vectorized columns
t["session"] = t["opened"].dt.hour.apply(get_session)
t["is_win"]  = t["status"].str.upper() == "WIN"
t["dir"]     = t["direction"].str.upper()
t["dur_min"] = t["duration"].apply(parse_dur)
t["rr"]      = t.apply(calc_rr, axis=1)
t["ep"]      = pd.to_numeric(t["open_price"], errors="coerce")

# Load candles
symbols = t["symbol"].unique()
print(f"Symbols: {len(symbols)}")
print("Loading candles...")
h4 = {}; h1 = {}
for i, s in enumerate(symbols):
    if (i+1) % 15 == 0: print(f"  {i+1}/{len(symbols)}...")
    h4[s] = load_candle(s, "H4")
    h1[s] = load_candle(s, "H1")

# H4 trends
print("H4 trends...")
trends = {}
for s in symbols:
    c = h4.get(s)
    if c is None or len(c) < 50: trends[s] = {}; continue
    mf = c["close"].rolling(20).mean(); ms = c["close"].rolling(50).mean()
    tm = {}
    for i in range(50, len(c)):
        ct = c.iloc[i]["time"]
        if mf.iloc[i] > ms.iloc[i]*1.005: tm[ct] = "UP"
        elif mf.iloc[i] < ms.iloc[i]*0.995: tm[ct] = "DOWN"
        else: tm[ct] = "neutral"
    trends[s] = tm

def get_trend(s, dt):
    tm = trends.get(s, {})
    if not tm: return "neutral"
    times = sorted(tm.keys())
    idx = pd.Series(times).searchsorted(dt, side="right") - 1
    return tm.get(times[idx], "neutral") if idx >= 0 else "neutral"

t["h4_trend"] = t.apply(lambda r: get_trend(r["symbol"], r["opened"]), axis=1)

# S/R proximity
print("S/R proximity...")
sr_map = {}
for s in symbols:
    c = h1.get(s)
    if c is None: continue
    st = t[t["symbol"] == s]
    for idx, row in st.iterrows():
        dt = row["opened"]; pr = row["ep"]
        if pd.isna(dt) or pd.isna(pr) or pr <= 0: sr_map[idx] = ("unknown", None); continue
        prev = c[c["time"].dt.date == (dt - timedelta(days=1)).date()]
        if prev.empty: sr_map[idx] = ("mid_range", None); continue
        ph = float(prev["high"].max()); pl = float(prev["low"].min())
        if abs(pr-ph)/pr < 0.005: sr_map[idx] = ("AT_PDH", ph)
        elif abs(pr-pl)/pr < 0.005: sr_map[idx] = ("AT_PDL", pl)
        else: sr_map[idx] = ("mid_range", None)

sr_df = pd.DataFrame([(k, v[0], v[1]) for k,v in sr_map.items()],
                     columns=["idx","sr_zone","sr_level"]).set_index("idx")
t["sr_zone"] = sr_df["sr_zone"]; t["sr_level"] = sr_df["sr_level"]

# News proximity
print("News proximity...")
news = pd.read_parquet(NEWS_FILE) if os.path.exists(NEWS_FILE) else pd.DataFrame()
if not news.empty and "datetime" in news.columns:
    news["datetime"] = pd.to_datetime(news["datetime"])
    news = news[news["impact"].str.upper() == "HIGH"]
def get_news(s, dt):
    if news.empty: return None, None
    n = news[(news["datetime"] >= dt-timedelta(hours=2)) & (news["datetime"] <= dt+timedelta(hours=2))]
    if n.empty: return None, None
    for _, e in n.iterrows():
        ins = str(e.get("instruments","")).split(",")
        if s in ins or not ins[0]: return e["event"], e.get("surprise","")
    return None, None

nmap = {}
for idx, row in t.iterrows():
    ev, sp = get_news(row["symbol"], row["opened"])
    nmap[idx] = (ev, sp)

ndf = pd.DataFrame([(k,v[0],v[1]) for k,v in nmap.items()],
                   columns=["idx","near_news","near_news_surprise"]).set_index("idx")
t["near_news"] = ndf["near_news"]; t["near_news_surprise"] = ndf["near_news_surprise"]

# Save
cols = ["symbol","dir","session","h4_trend","sr_zone","sr_level",
        "near_news","near_news_surprise","dur_min","rr","p_l","is_win",
        "trader","competition","ep"]
out = t[[c for c in cols if c in t.columns]].copy()
out.columns = ["symbol","direction","session","h4_trend","sr_zone","sr_level",
               "near_news","near_news_surprise","duration_min","rr","p_l","is_win",
               "trader","competition","entry_price"]
out["p_l"] = pd.to_numeric(t.get("p_l",0), errors="coerce")

os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
out.to_parquet(OUTPUT_FILE, index=False)
print(f"\nSaved {len(out):,} enriched rows -> {OUTPUT_FILE}")
print("Done. Run setup_miner.py to generate rules (instant now).")
