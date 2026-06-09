"""
Event-Based Setup Miner
========================
Uses the news calendar + event reactions + crowd-vs-smart data
to answer: "When [Event] happens, what did the market do and what
did the smart traders do?"

Output: engine/event_rules.json — one rule per event type × symbol
"""

import pandas as pd, json, os, sys
from datetime import datetime

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR    = r"c:\Users\Administrator\Documents\Rebel Funding"
EVENT_FILE  = os.path.join(BASE_DIR, "event_playbook.parquet")
CLUSTER_FILE= os.path.join(BASE_DIR, "trader_clusters.parquet")
REACT_FILE  = os.path.join(BASE_DIR, "event_reactions.parquet")
NEWS_FILE   = os.path.join(BASE_DIR, "news_calendar.parquet")
OUTPUT_FILE = os.path.join(os.path.dirname(__file__), "event_rules.json")

MIN_OCCURRENCES = 3     # events must happen at least this many times
MIN_CONFIDENCE  = 60    # % consistency in price direction

def _parse_dur(d):
    try: p = str(d).split(":"); return int(p[0])*60+int(p[1])
    except: return None


def main():
    print("=" * 60)
    print("Event-Based Setup Miner")
    print("Using: news calendar + crowd-vs-smart + price reactions")
    print("=" * 60)

    # ── Load data ─────────────────────────────────────────────────────────────
    ep = pd.read_parquet(EVENT_FILE) if os.path.exists(EVENT_FILE) else pd.DataFrame()
    cl = pd.read_parquet(CLUSTER_FILE) if os.path.exists(CLUSTER_FILE) else pd.DataFrame()
    er = pd.read_parquet(REACT_FILE) if os.path.exists(REACT_FILE) else pd.DataFrame()
    nw = pd.read_parquet(NEWS_FILE) if os.path.exists(NEWS_FILE) else pd.DataFrame()

    for df, name in [(ep,"event_playbook"),(er,"event_reactions"),(nw,"news")]:
        if not df.empty:
            for c in df.columns:
                if "date" in c or "time" in c:
                    try: df[c] = pd.to_datetime(df[c])
                    except: pass

    print(f"  event_playbook:   {len(ep)} rows")
    print(f"  trader_clusters:  {len(cl)} trades labelled")
    print(f"  event_reactions:  {len(er)} event-instrument pairs")
    print(f"  news_calendar:    {len(nw)} events")

    if ep.empty:
        print("\nERROR: event_playbook.parquet is empty.")
        print("Run the analysis pipeline first: run_all.bat")
        return

    # ── Enrich event_playbook with surprise from news ─────────────────────────
    if not nw.empty and "surprise" in nw.columns and "event" in nw.columns:
        nw_small = nw[["datetime","event","currency","surprise"]].copy()
        nw_small.columns = ["nw_dt","nw_event","nw_ccy","surprise"]
        # Match by date + event name prefix
        ep["event_date"] = pd.to_datetime(ep.get("event_date", pd.NaT))
        ep["surprise"] = ""

        for i, row in ep.iterrows():
            ed = row.get("event_date")
            ev = str(row.get("event",""))[:20]
            if pd.isna(ed): continue
            match = nw_small[
                (nw_small["nw_dt"].dt.date == ed.date()) &
                (nw_small["nw_event"].str[:20] == ev)
            ]
            if not match.empty:
                ep.at[i, "surprise"] = str(match.iloc[0]["surprise"])

    # ── Group by event type + symbol + surprise ──────────────────────────────
    ep["event_key"] = (
        ep["event"].fillna("?") + " | " +
        ep["symbol"].fillna("?") + " | " +
        ep["surprise"].fillna("?")
    )

    rules = []
    for key, grp in ep.groupby("event_key"):
        n = len(grp)
        if n < MIN_OCCURRENCES:
            continue

        # Price direction consistency
        valid = grp[grp["correct_dir"].notna()]
        if len(valid) < MIN_OCCURRENCES:
            continue
        up   = (valid["correct_dir"] == "UP").sum()
        down = (valid["correct_dir"] == "DOWN").sum()
        total = up + down
        dominant = "UP" if up >= down else "DOWN"
        conf = max(up/total*100, down/total*100) if total > 0 else 0
        if conf < MIN_CONFIDENCE:
            continue

        # Crowd stats
        crowd_wrong = grp["crowd_wrong_pct"].mean()
        correct_wr  = grp["correct_win_rate"].dropna().mean()
        correct_rr  = grp["correct_avg_rr"].dropna().mean()
        correct_n   = int(grp["correct_n"].sum())

        # From trader_clusters: analyze what correct traders did
        correct_trades = pd.DataFrame()
        if not cl.empty and "event" in cl.columns:
            parts = key.split(" | ")
            ev_match = cl[cl["event"].fillna("").str[:30] == parts[0][:30]]
            sym_match = ev_match[ev_match["symbol"] == parts[1]] if len(parts) > 1 else ev_match
            correct_trades = sym_match[sym_match["cluster"] == "CORRECT"]

        avg_dur = None
        early_pct = None
        if not correct_trades.empty and "duration" in correct_trades.columns:
            durs = correct_trades["duration"].apply(_parse_dur).dropna()
            if not durs.empty:
                avg_dur = round(float(durs.mean()), 1)
                early_pct = round(float((durs < durs.quantile(0.25)).mean()), 2)

        parts = key.split(" | ")
        event_name = parts[0].strip()
        symbol     = parts[1].strip() if len(parts) > 1 else "?"
        surprise   = parts[2].strip() if len(parts) > 2 else "?"

        rules.append({
            "id":               len(rules) + 1,
            "event":            event_name,
            "event_category":   _categorize(event_name),
            "symbol":           symbol,
            "surprise_filter":  surprise if surprise != "?" else "any",
            "occurrences":      int(n),
            "price_dir":        dominant,
            "confidence":       round(conf, 1),
            "up_pct":           round(up/total*100, 1) if total > 0 else 0,
            "down_pct":         round(down/total*100, 1) if total > 0 else 0,
            "crowd_wrong_pct":  round(float(crowd_wrong), 1) if pd.notna(crowd_wrong) else None,
            "correct_traders":  correct_n,
            "correct_win_rate": round(float(correct_wr), 3) if pd.notna(correct_wr) else None,
            "correct_avg_rr":   round(float(correct_rr), 2) if pd.notna(correct_rr) else None,
            "correct_avg_dur":  avg_dur,
            "correct_early_pct": early_pct,
        })

    rules.sort(key=lambda r: (r["confidence"] * r["occurrences"]), reverse=True)
    for i, r in enumerate(rules):
        r["id"] = i + 1

    # ── Save ──────────────────────────────────────────────────────────────────
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump(rules, f, indent=2, default=str)

    # ── Print ─────────────────────────────────────────────────────────────────
    print(f"\n{'=' * 80}")
    print(f"EVENT RULES ({len(rules)} total)")
    print(f"Each rule = what happens when a specific event occurs")
    print(f"{'=' * 80}")
    print(f"{'#':>3}  {'Event':<25} {'Symbol':<12} {'Surprise':<8} {'N':>3}  {'Dir':<5} {'Conf%':>6}  {'CrowdWrong%':>11}  {'SmartWR':>7}  {'SmartR:R':>8}")
    print("-" * 105)
    for r in rules[:30]:
        cw = f"{r['crowd_wrong_pct']:.0f}%" if r.get("crowd_wrong_pct") else "n/a"
        sw = f"{r['correct_win_rate']:.0%}" if r.get("correct_win_rate") else "n/a"
        sr = f"{r['correct_avg_rr']:.2f}" if r.get("correct_avg_rr") else "n/a"
        print(f"{r['id']:>3}  {r['event'][:24]:<25} {r['symbol']:<12} {r['surprise_filter']:<8} "
              f"{r['occurrences']:>3}  {r['price_dir']:<5} {r['confidence']:>5.1f}%  "
              f"{cw:>11}  {sw:>7}  {sr:>8}")

    print(f"\nSaved -> {OUTPUT_FILE}")
    print("=" * 60)

    # ── Insight summary ───────────────────────────────────────────────────────
    high_conf = [r for r in rules if r["confidence"] >= 75 and r["occurrences"] >= 4]
    if high_conf:
        print(f"\nHIGH-CONFIDENCE EVENT SETUPS ({len(high_conf)} with >=75% consistency):")
        for r in high_conf:
            note = ""
            if r.get("crowd_wrong_pct") and r["crowd_wrong_pct"] > 60:
                note = f" — CROWD WAS {r['crowd_wrong_pct']:.0f}% WRONG (fade the crowd)"
            print(f"  {r['event']} -> {r['symbol']}: {r['price_dir']} {r['confidence']:.0f}% "
                  f"({r['occurrences']}x){note}")


def _categorize(event_name):
    e = event_name.lower()
    if "nfp" in e or "payroll" in e: return "Employment"
    if "cpi" in e or "inflation" in e or "pce" in e or "ppi" in e: return "Inflation"
    if "fomc" in e or "fed" in e or "interest rate" in e: return "Central Bank"
    if "gdp" in e: return "GDP"
    if "pmi" in e or "ism" in e: return "PMI"
    if "retail" in e: return "Consumption"
    if "claims" in e: return "Employment"
    if "boe" in e or "boj" in e or "rba" in e or "boc" in e or "rbnz" in e or "snb" in e:
        return "Central Bank"
    return "Other"


if __name__ == "__main__":
    main()
