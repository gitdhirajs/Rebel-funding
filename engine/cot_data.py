"""
COT (Commitment of Traders) Data Fetcher
========================================
Downloads COT reports from CFTC.gov (free, no key, decades of history).
Extracts smart-money positioning for Rebel Funding instruments.

COT shows positioning of:
  - Commercials (smart money / hedgers)
  - Non-Commercials (large speculators)
  - Non-Reportable (small retail)

Methodology (from Azalyst-Bernd framework):
  COT Index = 140 * (net - lowest) / (highest - lowest) - 20
  Scale: -20 (extreme short) to +120 (extreme long)
  > 80 = overbought, < 20 = oversold

Run: python engine/cot_data.py
Output: engine/cot_positions.parquet
"""

import pandas as pd, numpy as np, requests, io, zipfile, os, sys
from datetime import datetime, timedelta

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OUTPUT_FILE = os.path.join(os.path.dirname(__file__), "cot_positions.parquet")

# CFTC commodity codes → our symbol names
CFTC_MAP = {
    "088691": "XAU/USD",   # COMEX Gold
    "084691": "XAG/USD",   # COMEX Silver
    "099741": "EUR/USD",   # CME Euro FX
    "096742": "GBP/USD",   # CME British Pound
    "097741": "USD/JPY",   # CME Japanese Yen  (inverted: JPY/USD in COT)
    "232741": "AUD/USD",   # CME Australian Dollar
    "090741": "USD/CAD",   # CME Canadian Dollar
    "092741": "USD/CHF",   # CME Swiss Franc (inverted)
    "095741": "USD/MXN",   # CME Mexican Peso (inverted)
    "112741": "NZD/USD",   # CME New Zealand Dollar
    "124603": "DJ30",      # CBOT Dow Jones
    "13874A": "US500",     # CME S&P 500
    "209742": "USTEC.v",   # CME NASDAQ-100
    "076651": "USO",       # NYMEX WTI Crude Oil (1USO)
    "023391": "NGAS",      # NYMEX Natural Gas (1NGAS)
}

# How far back to fetch
START_YEAR = 2020


def download_year(year):
    """Download COT data for a specific year from CFTC."""
    url = f"https://www.cftc.gov/files/dea/history/fut_fin_txt_{year}.zip"
    try:
        r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            print(f"    {year}: {r.status_code} (not available)")
            return None
        z = zipfile.ZipFile(io.BytesIO(r.content))
        # Find the annual file
        for name in z.namelist():
            if name.endswith(".txt"):
                return z.read(name).decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"    {year}: error ({e})")
    return None


def parse_cot(text, target_codes):
    """Parse the fixed-width COT report, extracting net positions."""
    rows = []
    for line in text.split("\n"):
        if len(line) < 120:
            continue
        code = line[0:6].strip()
        if code not in target_codes:
            continue

        try:
            date_str  = line[7:13].strip()   # YYMMDD
            name      = line[13:60].strip()
            noncomm_long  = int(line[60:67].strip() or 0)
            noncomm_short = int(line[67:74].strip() or 0)
            comm_long     = int(line[74:81].strip() or 0)
            comm_short    = int(line[81:88].strip() or 0)
            nonrep_long   = int(line[88:95].strip() or 0)
            nonrep_short  = int(line[95:102].strip() or 0)

            # Parse date (YYMMDD)
            yy = int(date_str[:2])
            mm = int(date_str[2:4])
            dd = int(date_str[4:6])
            year = 2000 + yy if yy < 70 else 1900 + yy
            dt = datetime(year, mm, dd)

            # Net positions
            commercial_net = comm_long - comm_short
            speculator_net = noncomm_long - noncomm_short
            retail_net     = nonrep_long - nonrep_short

            # Total OI
            total_oi = (noncomm_long + noncomm_short +
                       comm_long + comm_short +
                       nonrep_long + nonrep_short)

            rows.append({
                "date": dt,
                "code": code,
                "name": name,
                "commercial_net":   commercial_net,
                "speculator_net":   speculator_net,
                "retail_net":       retail_net,
                "total_oi":         total_oi,
                "comm_long":  comm_long,   "comm_short":  comm_short,
                "spec_long":  noncomm_long,"spec_short":  noncomm_short,
                "retail_long":nonrep_long, "retail_short":nonrep_short,
            })
        except (ValueError, IndexError):
            continue

    return rows


def compute_cot_index(df, window=52):
    """
    COT Index V2 from Azalyst-Bernd:
    140 * (net_position - lowest_N) / (highest_N - lowest_N) - 20
    Range: -20 to +120
    """
    df = df.sort_values("date").copy()
    for col in ["commercial_net", "speculator_net"]:
        net = df[col]
        lowest  = net.rolling(window, min_periods=10).min()
        highest = net.rolling(window, min_periods=10).max()
        denom   = highest - lowest
        idx = np.where(denom > 0, 140 * (net - lowest) / denom - 20, 0)
        df[f"{col}_index"] = np.round(idx, 1)
    return df


def main():
    print("=" * 60)
    print("COT Data Fetcher — CFTC.gov (free, no key)")
    print(f"Period: {START_YEAR} → {datetime.now().year}")
    print(f"Instruments: {len(CFTC_MAP)}")
    print("=" * 60 + "\n")

    codes = set(CFTC_MAP.keys())
    all_rows = []

    for year in range(START_YEAR, datetime.now().year + 1):
        print(f"  {year} ... ", end="", flush=True)
        text = download_year(year)
        if text is None:
            continue
        rows = parse_cot(text, codes)
        all_rows.extend(rows)
        print(f"{len(rows)} records")

    if not all_rows:
        print("\nNo COT data downloaded.")
        return

    df = pd.DataFrame(all_rows)
    df["date"] = pd.to_datetime(df["date"])
    df["symbol"] = df["code"].map(CFTC_MAP)

    # Compute COT Index per instrument
    print(f"\n  Computing COT Index...")
    frames = []
    for symbol, grp in df.groupby("symbol"):
        indexed = compute_cot_index(grp)
        frames.append(indexed)

    df = pd.concat(frames).sort_values(["symbol", "date"]).reset_index(drop=True)

    # Latest positioning
    latest = df.groupby("symbol").last().reset_index()

    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    df.to_parquet(OUTPUT_FILE, index=False)

    print(f"\n{'=' * 60}")
    print("COT POSITIONING (latest available)")
    print(f"{'=' * 60}")
    print(f"{'Symbol':<12} {'Date':<12} {'Commercial Net':>14} {'Commercial Index':>16}")
    print("-" * 56)
    for _, r in latest.iterrows():
        ci = r.get("commercial_net_index", 0)
        bias = "🟢 BULLISH" if ci > 60 else "🔴 BEARISH" if ci < 20 else "⚪ NEUTRAL"
        print(f"{r['symbol']:<12} {str(r['date'].date()):<12} {r['commercial_net']:>14,}  "
              f"{ci:>5.0f} ({bias})")

    print(f"\nSaved -> {OUTPUT_FILE}")
    print(f"Total: {len(df):,} weekly COT records for {df['symbol'].nunique()} instruments")
    print("=" * 60)


if __name__ == "__main__":
    main()
