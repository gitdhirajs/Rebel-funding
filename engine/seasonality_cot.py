"""
Seasonality + COT Data via yfinance
====================================
Uses yfinance to fetch 10-year D1 data for seasonality patterns
and futures data for COT simulation.

COT Simulation (when CFTC API unavailable):
  Approximates commercial positioning from price/volume patterns.
  - Strong uptrend + increasing volume → commercials accumulating (bullish)
  - Strong downtrend + high volume → commercials distributing (bearish)
  - Ranging → neutral

Seasonality:
  Computes monthly bias from 10 years of daily data.
  "In January, gold is up 65% of the time with avg return of +2.1%"

Run: python engine/seasonality_cot.py
Output: engine/seasonality.parquet + engine/cot_simulated.parquet
"""

import yfinance as yf
import pandas as pd
import numpy as np
import os, sys
from datetime import datetime, timedelta

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OUT_DIR   = os.path.dirname(__file__)
SEASON_FILE = os.path.join(OUT_DIR, "seasonality.parquet")
COT_SIM_FILE = os.path.join(OUT_DIR, "cot_simulated.parquet")

# yfinance tickers → our symbols
TICKER_MAP = {
    "XAU/USD":  "GC=F",      # Gold futures
    "XAG/USD":  "SI=F",      # Silver futures
    "EUR/USD":  "EURUSD=X",
    "GBP/USD":  "GBPUSD=X",
    "USD/JPY":  "JPY=X",
    "AUD/USD":  "AUDUSD=X",
    "NZD/USD":  "NZDUSD=X",
    "USD/CAD":  "CAD=X",
    "USD/CHF":  "CHF=X",
    "DJ30":     "YM=F",      # Dow futures
    "USTEC.v":  "NQ=F",      # Nasdaq futures
    "US500":    "ES=F",      # S&P futures
    "BTCUSDT":  "BTC-USD",
    "ETHUSDT":  "ETH-USD",
    "1USO":     "CL=F",      # Crude oil futures
    "1NGAS":    "NG=F",      # Natural gas futures
}

SYMBOLS = ["XAU/USD", "XAG/USD", "EUR/USD", "GBP/USD", "USD/JPY",
           "AUD/USD", "USD/CAD", "DJ30", "USTEC.v", "US500"]


def fetch_d1(symbol, years=10):
    """Download D1 data via yfinance."""
    ticker = TICKER_MAP.get(symbol, symbol)
    try:
        end = datetime.now()
        start = end - timedelta(days=years * 365)
        df = yf.Ticker(ticker).history(start=start, end=end, interval="1d", auto_adjust=True)
        if df.empty:
            return None
        df = df.reset_index()
        df = df.rename(columns={"Date": "date", "Open": "open", "High": "high",
                                "Low": "low", "Close": "close", "Volume": "volume"})
        df["date"] = pd.to_datetime(df["date"])
        return df[["date", "open", "high", "low", "close", "volume"]]
    except Exception as e:
        print(f"    {symbol}: {e}")
        return None


def compute_seasonality(df):
    """
    Compute monthly seasonality stats.
    Returns: month, win_pct, avg_return, median_return, n_years
    """
    if df is None or len(df) < 252:
        return None

    df = df.copy()
    df["month"] = df["date"].dt.month
    df["year"]  = df["date"].dt.year
    df["return"] = df["close"].pct_change()

    # Monthly stats
    monthly = df.groupby("month").agg(
        avg_return=("return", "mean"),
        median_return=("return", "median"),
        up_pct=("return", lambda x: (x > 0).mean() * 100),
        n_days=("return", "count"),
        n_years=("year", "nunique"),
    ).reset_index()

    monthly["bias"] = monthly.apply(
        lambda r: "BULLISH" if r["up_pct"] > 55 and r["avg_return"] > 0
        else "BEARISH" if r["up_pct"] < 45 and r["avg_return"] < 0
        else "NEUTRAL", axis=1
    )

    monthly["avg_return_pct"] = (monthly["avg_return"] * 100).round(3)
    monthly["up_pct"] = monthly["up_pct"].round(1)
    return monthly


def simulate_cot(df):
    """
    Simulate COT commercial positioning from price/volume.
    Not real COT, but directionally useful when CFTC is blocked.

    Logic:
      - Compute 20-day price trend strength + volume trend
      - Strong uptrend + rising volume → commercial net long (bullish)
      - Strong downtrend + high volume → commercial net short (bearish)
      - Ranging → neutral
    """
    if df is None or len(df) < 50:
        return None

    df = df.copy()
    df["price_roc"] = df["close"].pct_change(20) * 100    # 20-day % change
    df["vol_ma"]    = df["volume"].rolling(20).mean()
    df["vol_ratio"] = df["volume"] / df["vol_ma"].replace(0, 1)

    # Composite score: price trend (-100 to +100) × volume confirmation
    df["trend_score"] = df["price_roc"].clip(-5, 5) * 20  # scale to -100/+100
    df["vol_score"]   = (df["vol_ratio"] - 1).clip(-1, 1) * 50
    df["cot_index"]   = (df["trend_score"] * 0.7 + df["vol_score"] * 0.3).clip(-100, 100)

    # Weekly aggregation (COT is weekly)
    df["week"] = df["date"].dt.isocalendar().year.astype(str) + "-W" + \
                 df["date"].dt.isocalendar().week.astype(str).str.zfill(2)
    weekly = df.groupby("week").agg(
        date=("date", "last"),
        cot_index=("cot_index", "mean"),
        close=("close", "last"),
    ).reset_index(drop=True)

    weekly["cot_index"] = weekly["cot_index"].round(1)

    # Bias interpretation
    weekly["bias"] = weekly["cot_index"].apply(
        lambda x: "BULLISH" if x > 20 else "BEARISH" if x < -20 else "NEUTRAL"
    )

    return weekly[["date", "cot_index", "bias", "close"]]


def main():
    print("=" * 60)
    print("Seasonality + COT Builder (yfinance)")
    print(f"Symbols: {len(SYMBOLS)} | History: 10 years")
    print("=" * 60 + "\n")

    all_season = []
    all_cot    = []

    for i, sym in enumerate(SYMBOLS):
        print(f"  [{i+1}/{len(SYMBOLS)}] {sym} ...", end=" ", flush=True)
        df = fetch_d1(sym, years=10)
        if df is None:
            print("no data")
            continue

        # Seasonality
        s = compute_seasonality(df)
        if s is not None:
            s["symbol"] = sym
            all_season.append(s)
            months = len(s)
        else:
            months = 0

        # COT simulation
        c = simulate_cot(df)
        if c is not None:
            c["symbol"] = sym
            all_cot.append(c)
            weeks = len(c)
        else:
            weeks = 0

        print(f"{len(df)} candles | {months} season months | {weeks} COT weeks")

    # ── Save seasonality ──────────────────────────────────────────────────────
    if all_season:
        season_df = pd.concat(all_season).reset_index(drop=True)
        os.makedirs(OUT_DIR, exist_ok=True)
        season_df.to_parquet(SEASON_FILE, index=False)
        print(f"\nSeasonality saved -> {SEASON_FILE}")
        print(f"  {len(season_df)} month-symbol patterns")

        # Print current month bias
        current_month = datetime.now().month
        print(f"\n  Current month (month {current_month}) bias:")
        cm = season_df[season_df["month"] == current_month]
        for _, r in cm.iterrows():
            arrow = "🟢" if r["bias"] == "BULLISH" else "🔴" if r["bias"] == "BEARISH" else "⚪"
            print(f"    {arrow} {r['symbol']:<12} {r['bias']:<8}  "
                  f"Up:{r['up_pct']:.0f}%  Avg:{r['avg_return_pct']:+.2f}%  "
                  f"({int(r['n_years'])}yr history)")

    # ── Save COT ──────────────────────────────────────────────────────────────
    if all_cot:
        cot_df = pd.concat(all_cot).reset_index(drop=True)
        cot_df.to_parquet(COT_SIM_FILE, index=False)
        print(f"\nCOT (simulated) saved -> {COT_SIM_FILE}")
        print(f"  {len(cot_df)} weekly records")

        # Latest positioning
        latest = cot_df.groupby("symbol").last().reset_index()
        print(f"\n  Latest simulated COT bias:")
        for _, r in latest.iterrows():
            idx = r["cot_index"]
            arrow = "🟢" if idx > 20 else "🔴" if idx < -20 else "⚪"
            print(f"    {arrow} {r['symbol']:<12}  Index:{idx:>+6.0f}  {r['bias']}")

    print(f"\n{'=' * 60}")
    print("Done. Ready for challenge engine.")
    print("=" * 60)


if __name__ == "__main__":
    main()
