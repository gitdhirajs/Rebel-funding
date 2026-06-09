"""Signal Quality Report — Does the system predict direction correctly?"""
import pandas as pd, os

df = pd.read_parquet(os.path.join(os.path.dirname(__file__), "backtest_results.parquet"))
df["month"] = pd.to_datetime(df["entry_date"]).dt.strftime("%Y-%m")

print("=" * 70)
print("SIGNAL QUALITY REPORT")
print("=" * 70)

# Every trade
print("\n--- EVERY TRADE ---")
for _, t in df.iterrows():
    d = str(t["entry_date"])[:10]
    print(f"  {d} {t['symbol']:<10} {t['direction']:<6} E:{t['entry']:>8.2f} X:{t['exit_price']:>8.2f} "
          f"{t['result']:<6} {t['pnl_pct']:>+7.2f}% | loc:{str(t.get('location','')):<10} "
          f"trend:{str(t.get('trend','')):<6} cot:{str(t.get('cot','')):<8} seas:{str(t.get('seasonality','')):<8}")

# By symbol
print("\n--- BY SYMBOL ---")
for sym in sorted(df["symbol"].unique()):
    s = df[df["symbol"] == sym]
    w = (s["result"] == "WIN").sum()
    print(f"  {sym:<12} {len(s):>3} signals | {w}/{len(s)} wins | {w/len(s)*100:.0f}% accuracy")

# By direction
print("\n--- BY DIRECTION ---")
for d in ["LONG", "SHORT"]:
    s = df[df["direction"] == d]
    if len(s) == 0: continue
    w = (s["result"] == "WIN").sum()
    print(f"  {d:<6} {len(s):>3} signals | {w}/{len(s)} wins | {w/len(s)*100:.0f}% accuracy")

# By location
print("\n--- BY LOCATION (entry quality) ---")
for loc in df["location"].unique():
    s = df[df["location"] == loc]
    w = (s["result"] == "WIN").sum()
    print(f"  {str(loc):<12} {len(s):>3} trades | {w}/{len(s)} wins | {w/len(s)*100:.0f}% accuracy")

# Trend alignment
print("\n--- TREND ALIGNMENT ---")
aligned = df[((df["direction"] == "LONG") & (df["trend"] == "UP")) |
             ((df["direction"] == "SHORT") & (df["trend"] == "DOWN"))]
counter = df[((df["direction"] == "LONG") & (df["trend"] == "DOWN")) |
             ((df["direction"] == "SHORT") & (df["trend"] == "UP"))]
for label, s in [("With trend", aligned), ("Counter-trend", counter)]:
    if len(s) > 0:
        w = (s["result"] == "WIN").sum()
        print(f"  {label:<15} {len(s):>3} trades | {w}/{len(s)} wins | {w/len(s)*100:.0f}% accuracy")

# COT confirmation
print("\n--- COT CONFIRMATION ---")
cot_ok = df[((df["direction"] == "LONG") & (df["cot"] == "bullish")) |
            ((df["direction"] == "SHORT") & (df["cot"] == "bearish"))]
cot_no = df[~df.index.isin(cot_ok.index)]
for label, s in [("COT confirmed", cot_ok), ("COT neutral/against", cot_no)]:
    if len(s) > 0:
        w = (s["result"] == "WIN").sum()
        print(f"  {label:<20} {len(s):>3} trades | {w}/{len(s)} wins | {w/len(s)*100:.0f}% accuracy")

# Seasonality
print("\n--- SEASONALITY CONFIRMATION ---")
seas_ok = df[((df["direction"] == "LONG") & (df["seasonality"] == "bullish")) |
             ((df["direction"] == "SHORT") & (df["seasonality"] == "bearish"))]
seas_no = df[~df.index.isin(seas_ok.index)]
for label, s in [("Season confirmed", seas_ok), ("Season neutral/against", seas_no)]:
    if len(s) > 0:
        w = (s["result"] == "WIN").sum()
        print(f"  {label:<20} {len(s):>3} trades | {w}/{len(s)} wins | {w/len(s)*100:.0f}% accuracy")

# Consensus strength
print("\n--- CONSENSUS STRENGTH ---")
df["consensus"] = 1  # location
df.loc[((df["direction"] == "LONG") & (df["cot"] == "bullish")) |
       ((df["direction"] == "SHORT") & (df["cot"] == "bearish")), "consensus"] += 1
df.loc[((df["direction"] == "LONG") & (df["seasonality"] == "bullish")) |
       ((df["direction"] == "SHORT") & (df["seasonality"] == "bearish")), "consensus"] += 1
df.loc[((df["direction"] == "LONG") & (df["trend"] == "UP")) |
       ((df["direction"] == "SHORT") & (df["trend"] == "DOWN")), "consensus"] += 1
for n in sorted(df["consensus"].unique()):
    s = df[df["consensus"] == n]
    w = (s["result"] == "WIN").sum()
    print(f"  {n}/4 dims agree: {len(s):>3} trades | {w}/{len(s)} wins | {w/len(s)*100:.0f}% accuracy")

print("\n" + "=" * 70)
print("SUMMARY")
wr = (df["result"] == "WIN").mean() * 100
avg_w = df[df["result"] == "WIN"]["pnl_pct"].mean()
avg_l = df[df["result"] == "LOSS"]["pnl_pct"].mean()
print(f"  Overall accuracy: {wr:.0f}%")
print(f"  Avg win: +{avg_w:.1f}% | Avg loss: {avg_l:.1f}%")
print(f"  Expectancy: {wr/100*avg_w + (1-wr/100)*avg_l:+.2f}% per trade")
print(f"  Total: {df['pnl_pct'].sum():+.1f}% from {len(df)} trades")
print("=" * 70)
