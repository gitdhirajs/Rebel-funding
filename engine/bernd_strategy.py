"""
Bernd Skorupinski Strategy Engine
==================================
Implements the exact methodology from Bernd's OTC framework:
  1. Zone Detection (Supply/Demand with candle classification)
  2. Valuation (ROC vs DXY, ZB, GC benchmarks)
  3. Location Gate (cheap/expensive based on HTF zone)
  4. COT / Seasonality confirmation
  5. Bias Synthesis (strict hierarchy, NOT equal voting)

Run: python engine/bernd_strategy.py
"""

import yfinance as yf
import pandas as pd
import numpy as np
import os, sys
from datetime import datetime, timedelta

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OUT_DIR = os.path.dirname(__file__)
SEASON_FILE  = os.path.join(OUT_DIR, "seasonality.parquet")
COT_SIM_FILE = os.path.join(OUT_DIR, "cot_simulated.parquet")
RULES_FILE   = os.path.join(OUT_DIR, "setup_rules.json")

# Instruments we trade
SYMBOLS = {
    "XAU/USD": {"ticker": "GC=F",  "class": "commodity", "benchmarks": ["DXY", "ZB", "GC"]},
    "XAG/USD": {"ticker": "SI=F",  "class": "commodity", "benchmarks": ["DXY", "ZB", "GC"]},
    "EUR/USD": {"ticker": "EURUSD=X","class":"forex",     "benchmarks": ["DXY"]},
    "GBP/USD": {"ticker": "GBPUSD=X","class":"forex",     "benchmarks": ["DXY"]},
    "USD/JPY": {"ticker": "JPY=X", "class": "forex",      "benchmarks": ["DXY"]},
    "DJ30":    {"ticker": "YM=F",  "class": "equity",     "benchmarks": ["DXY", "ZB"]},
    "USTEC.v": {"ticker": "NQ=F",  "class": "equity",     "benchmarks": ["DXY", "ZB"]},
    "US500":   {"ticker": "ES=F",  "class": "equity",     "benchmarks": ["DXY", "ZB"]},
}

# Benchmark tickers
BENCHMARKS = {"DXY": "DX-Y.NYB", "ZB": "ZB=F", "GC": "GC=F"}

# ═══════════════════════════════════════════════════════════════════════════════
# 1. ZONE DETECTION (Bernd's candle classification)
# ═══════════════════════════════════════════════════════════════════════════════

class Candle:
    def __init__(self, o, h, l, c, dt=None):
        self.open = o; self.high = h; self.low = l; self.close = c
        self.dt = dt
        rng = h - l
        self.body_pct = abs(c - o) / rng if rng > 0 else 0
        self.direction = "bullish" if c > o else "bearish" if c < o else "neutral"
        if self.body_pct >= 0.70:
            self.candle_type = "explosive"
        elif self.body_pct > 0.50:
            self.candle_type = "decisive"
        else:
            self.candle_type = "indecisive"

    def __repr__(self):
        return f"Candle({self.direction},{self.candle_type},body={self.body_pct:.2f})"


class Zone:
    def __init__(self, zone_type, formation, proximal, distal, base_candles, legout_candles):
        self.zone_type  = zone_type   # "demand" or "supply"
        self.formation  = formation   # "RBR","DBR","RBD","DBD"
        self.proximal   = proximal
        self.distal     = distal
        self.is_original = formation in ("RBR", "DBD")
        self.score       = 10 if self.is_original else 5
        self.retests     = 0
        self.freshness   = 10
        self.base_count  = len(base_candles)
        self.stop = (distal - 0.33 * abs(proximal - distal) if zone_type == "demand"
                     else distal + 0.33 * abs(distal - proximal))

    def __repr__(self):
        return f"Zone({self.zone_type},{self.formation},P:{self.proximal:.2f},D:{self.distal:.2f})"


def classify_candles(df):
    """Convert OHLCV dataframe to Candle objects."""
    return [Candle(r.Open, r.High, r.Low, r.Close, r.name) for _, r in df.iterrows()]


def detect_zones(candles, min_legin=3):
    """Scan candles for valid supply/demand zones."""
    zones = []
    avg_range = pd.Series([c.high - c.low for c in candles]).rolling(20).mean()

    for i in range(len(candles)):
        # Find base: 1-6 consecutive indecisive candles
        base_start = i
        base_count = 0
        while i + base_count < len(candles) and candles[i + base_count].candle_type == "indecisive":
            base_count += 1
        if base_count < 1 or base_count > 6:
            continue
        base_end = i + base_count - 1
        base_candles = candles[base_start:base_end + 1]

        # Find leg-in before base
        if base_start < min_legin:
            continue
        legin_dir = candles[base_start - 1].direction
        if legin_dir == "neutral":
            continue
        legin_count = 0
        j = base_start - 1
        while j >= 0 and candles[j].candle_type in ("decisive", "explosive") and candles[j].direction == legin_dir:
            legin_count += 1
            j -= 1
        if legin_count < min_legin:
            continue

        # Find leg-out after base (MUST have explosive candle)
        lo_start = base_end + 1
        if lo_start >= len(candles):
            continue
        lo_candle = candles[lo_start]
        if lo_candle.candle_type != "explosive":
            continue
        # Check leg-out size (1.2x avg range — relaxed from 1.5x for smaller datasets)
        rng = lo_candle.high - lo_candle.low
        if pd.notna(avg_range.iloc[lo_start]) and rng < 1.2 * avg_range.iloc[lo_start]:
            continue

        lo_dir = lo_candle.direction
        lo_candles = [lo_candle]

        # Formation type
        if legin_dir == "bearish" and lo_dir == "bullish":
            formation = "DBR"
        elif legin_dir == "bullish" and lo_dir == "bullish":
            formation = "RBR"
        elif legin_dir == "bullish" and lo_dir == "bearish":
            formation = "RBD"
        elif legin_dir == "bearish" and lo_dir == "bearish":
            formation = "DBD"
        else:
            continue

        zone_type = "demand" if formation in ("DBR", "RBR") else "supply"
        combined = base_candles + lo_candles

        if zone_type == "demand":
            proximal = max(max(c.open, c.close) for c in base_candles)
            distal   = min(c.low for c in combined)
        else:
            proximal = min(min(c.open, c.close) for c in base_candles)
            distal   = max(c.high for c in combined)

        zones.append(Zone(zone_type, formation, proximal, distal, base_candles, lo_candles))

    return zones


def active_zone(zones, price):
    """Find the nearest active zone relative to current price."""
    # Nearest demand below price, nearest supply above
    demand_zones = [z for z in zones if z.zone_type == "demand" and z.proximal < price * 1.02]
    supply_zones = [z for z in zones if z.zone_type == "supply" and z.proximal > price * 0.98]

    nearest_demand = min(demand_zones, key=lambda z: price - z.proximal) if demand_zones else None
    nearest_supply = max(supply_zones, key=lambda z: z.proximal - price) if supply_zones else None

    return nearest_demand, nearest_supply


def location_score(price, nearest_demand, nearest_supply):
    """
    Location gate from Bernd's methodology.
    Returns: (location_label, proposed_direction)
      "cheap" = near demand = BULLISH
      "expensive" = near supply = BEARISH
      "equilibrium" = middle = NEUTRAL (no trade unless consensus override)
    """
    if nearest_demand and nearest_supply:
        zone_range = nearest_supply.proximal - nearest_demand.proximal
        if zone_range <= 0:
            return "equilibrium", None
        position = (price - nearest_demand.proximal) / zone_range
        if position <= 0.33:
            return "cheap", "LONG"
        elif position >= 0.67:
            return "expensive", "SHORT"
        else:
            return "equilibrium", None

    if nearest_demand:
        dist_pct = abs(price - nearest_demand.proximal) / price
        return ("cheap", "LONG") if dist_pct < 0.01 else ("equilibrium", None)

    if nearest_supply:
        dist_pct = abs(price - nearest_supply.proximal) / price
        return ("expensive", "SHORT") if dist_pct < 0.01 else ("equilibrium", None)

    return "equilibrium", None


# ═══════════════════════════════════════════════════════════════════════════════
# 2. VALUATION (ROC vs benchmarks)
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_benchmark(ticker):
    try:
        df = yf.Ticker(ticker).history(period="6mo", interval="1d", auto_adjust=True)
        return df["Close"] if not df.empty else None
    except:
        return None


def compute_valuation(symbol_df, benchmarks_dict, asset_class):
    """
    Valuation = ROC comparison against macro benchmarks.
    Returns: score (-100 to +100), label (undervalued/fair/overvalued)
    """
    if symbol_df is None or len(symbol_df) < 20:
        return 0, "neutral"

    # 10-period ROC for the symbol
    asset_roc = (symbol_df["Close"].iloc[-1] / symbol_df["Close"].iloc[-11] - 1) * 100 if len(symbol_df) >= 11 else 0

    ref_rocs = []
    for bm_name, bm_series in benchmarks_dict.items():
        if bm_series is not None and len(bm_series) >= 11:
            roc = (bm_series.iloc[-1] / bm_series.iloc[-11] - 1) * 100
            ref_rocs.append(roc)

    if not ref_rocs:
        return 0, "neutral"

    # Score: how much asset is outperforming/underperforming benchmarks
    avg_ref = np.mean(ref_rocs)
    diff = asset_roc - avg_ref
    score = np.clip(diff * 20, -100, 100)  # scale to -100/+100

    if score < -30:
        label = "undervalued"
    elif score > 30:
        label = "overvalued"
    else:
        label = "neutral"

    return round(score, 1), label


# ═══════════════════════════════════════════════════════════════════════════════
# 3. COT & SEASONALITY
# ═══════════════════════════════════════════════════════════════════════════════

def load_cot_season(symbol):
    cot_bias = "neutral"
    seas_bias = "neutral"
    seas_up_pct = 50

    if os.path.exists(COT_SIM_FILE):
        cot_df = pd.read_parquet(COT_SIM_FILE)
        cot_row = cot_df[cot_df["symbol"] == symbol]
        if not cot_row.empty:
            idx = cot_row.iloc[-1]["cot_index"]
            cot_bias = "bullish" if idx > 20 else "bearish" if idx < -20 else "neutral"

    if os.path.exists(SEASON_FILE):
        seas_df = pd.read_parquet(SEASON_FILE)
        month = datetime.now().month
        sr = seas_df[(seas_df["symbol"] == symbol) & (seas_df["month"] == month)]
        if not sr.empty:
            r = sr.iloc[0]
            seas_bias = r["bias"].lower()  # BULLISH/BEARISH/NEUTRAL -> lowercase
            seas_up_pct = r["up_pct"]

    return cot_bias, seas_bias, seas_up_pct


# ═══════════════════════════════════════════════════════════════════════════════
# 4. BIAS SYNTHESIS (Bernd's strict hierarchy)
# ═══════════════════════════════════════════════════════════════════════════════

def synthesize_bias(location, direction, valuation_label, valuation_score,
                    cot_bias, seas_bias, trend, asset_class):
    """
    Bernd's exact hierarchy:
      1. Valuation hard gate — STRONG opposition = VETO
      2. Location gate — cheap/expensive determines direction
      3. Minimum threshold — what confirms are needed
      4. Counter-trend safety — don't fight the trend without consensus

    Returns: (signal_direction, confidence, reasoning)
      signal_direction: "LONG", "SHORT", or None (no trade)
    """
    reasons = []

    # ── 1. VALUATION HARD GATE ────────────────────────────────────────────────
    if valuation_label == "overvalued" and direction == "LONG":
        # Could be mild or strong opposition
        if valuation_score > 50:
            return None, 0, [f"VETO: Valuation strongly overvalued ({valuation_score}), cannot LONG"]
        else:
            reasons.append(f"WARNING: Valuation mildly overvalued ({valuation_score})")

    if valuation_label == "undervalued" and direction == "SHORT":
        if valuation_score < -50:
            return None, 0, [f"VETO: Valuation strongly undervalued ({valuation_score}), cannot SHORT"]
        else:
            reasons.append(f"WARNING: Valuation mildly undervalued ({valuation_score})")

    # ── 2. LOCATION GATE ──────────────────────────────────────────────────────
    if location == "equilibrium":
        # Need ALL 3 fundamentals to override
        fund_count = 0
        if valuation_label != "neutral" and not (
            (valuation_label == "overvalued" and direction == "LONG") or
            (valuation_label == "undervalued" and direction == "SHORT")
        ):
            fund_count += 1
        if cot_bias == ("bullish" if direction == "LONG" else "bearish"):
            fund_count += 1
        if seas_bias == ("bullish" if direction == "LONG" else "bearish"):
            fund_count += 1

        if fund_count < 3:
            return None, 0, [f"Location equilibrium — need 3/3 fundamentals, got {fund_count}/3"]
        reasons.append(f"Location equilibrium but all 3 fundamentals override")

    # ── 3. MINIMUM THRESHOLD ──────────────────────────────────────────────────
    confirmations = 0

    # Valuation aligned?
    val_aligned = False
    if (valuation_label == "undervalued" and direction == "LONG") or \
       (valuation_label == "overvalued" and direction == "SHORT"):
        val_aligned = True
        confirmations += 1
        reasons.append(f"Valuation aligned ({valuation_label})")
    elif valuation_label == "neutral":
        reasons.append("Valuation neutral")

    # COT aligned?
    if (cot_bias == "bullish" and direction == "LONG") or \
       (cot_bias == "bearish" and direction == "SHORT"):
        confirmations += 1
        reasons.append(f"COT aligned ({cot_bias})")
    else:
        reasons.append(f"COT {cot_bias}")

    # Seasonality aligned?
    if (seas_bias == "bullish" and direction == "LONG") or \
       (seas_bias == "bearish" and direction == "SHORT"):
        confirmations += 1
        reasons.append(f"Seasonality aligned ({seas_bias})")
    else:
        reasons.append(f"Seasonality {seas_bias}")

    # Location already counted (it determined the direction)
    confirmations += 1

    # Need: Loc+Val aligned, or Loc+Val_neutral+at_least_1_other
    if not val_aligned and confirmations < 3:
        return None, 0, [f"Insufficient confirmations: {confirmations}/4 (need Val_aligned or 3+ total)"]

    # ── 4. COUNTER-TREND SAFETY ───────────────────────────────────────────────
    trend_dir = "bullish" if trend == "UP" else "bearish" if trend == "DOWN" else "neutral"
    counter_trend = (direction == "LONG" and trend_dir == "bearish") or \
                    (direction == "SHORT" and trend_dir == "bullish")

    if counter_trend:
        non_trend_confirmations = confirmations - 1  # exclude location
        opposing = 1 if (
            (direction == "LONG" and cot_bias == "bearish") or
            (direction == "SHORT" and cot_bias == "bullish")
        ) else 0
        if non_trend_confirmations < 2 or opposing > 0:
            return None, 0, [f"Counter-trend blocked: {non_trend_confirmations} non-trend confirms, {opposing} opposing"]
        reasons.append("Counter-trend trade (approved by non-trend consensus)")

    # ── CONFIDENCE ────────────────────────────────────────────────────────────
    confidence = min(confirmations * 25, 90)
    if valuation_label in ("undervalued", "overvalued") and val_aligned:
        confidence += 10

    return direction, confidence, reasons


# ═══════════════════════════════════════════════════════════════════════════════
# 5. MAIN ANALYZER
# ═══════════════════════════════════════════════════════════════════════════════

def analyze(symbol, lookback_days=365):
    """Run Bernd's full analysis on one symbol. Returns signal or None."""
    info = SYMBOLS.get(symbol)
    if not info:
        return None

    ticker  = info["ticker"]
    aclass  = info["class"]
    benchmarks_list = info.get("benchmarks", [])

    print(f"\n{'=' * 60}")
    print(f"  {symbol} — Bernd Strategy Analysis")
    print(f"{'=' * 60}")

    # ── Fetch data ────────────────────────────────────────────────────────────
    end = datetime.now()
    start = end - timedelta(days=lookback_days)
    df = yf.Ticker(ticker).history(start=start, end=end, interval="1d", auto_adjust=True)
    if df.empty:
        print(f"  No data for {ticker}")
        return None
    print(f"  {len(df)} daily candles loaded")

    # ── Zone Detection ────────────────────────────────────────────────────────
    candles = classify_candles(df)
    zones = detect_zones(candles, min_legin=2)
    price = float(df["Close"].iloc[-1])

    nearest_d, nearest_s = active_zone(zones, price)
    location, direction = location_score(price, nearest_d, nearest_s)

    # Determine H4 trend from price vs MA
    trend = "neutral"
    if len(df) >= 50:
        ma20 = df["Close"].rolling(20).mean().iloc[-1]
        ma50 = df["Close"].rolling(50).mean().iloc[-1]
        if ma20 > ma50 * 1.005:
            trend = "UP"
        elif ma20 < ma50 * 0.995:
            trend = "DOWN"

    print(f"  Price: {price:.2f} | Trend: {trend}")
    print(f"  Zones found: {len(zones)} (demand: {sum(1 for z in zones if z.zone_type=='demand')}, supply: {sum(1 for z in zones if z.zone_type=='supply')})")
    if nearest_d:
        print(f"  Nearest demand: {nearest_d.proximal:.2f} (formation: {nearest_d.formation}, score: {nearest_d.score}/10)")
    if nearest_s:
        print(f"  Nearest supply: {nearest_s.proximal:.2f} (formation: {nearest_s.formation}, score: {nearest_s.score}/10)")
    print(f"  Location: {location} -> proposed {direction}")

    # ── Valuation ─────────────────────────────────────────────────────────────
    bm_data = {}
    for bm_name in benchmarks_list:
        bm_ticker = BENCHMARKS.get(bm_name, bm_name)
        bm_data[bm_name] = fetch_benchmark(bm_ticker)

    val_score, val_label = compute_valuation(df, bm_data, aclass)
    print(f"  Valuation: {val_label} (score: {val_score})")

    # ── COT & Seasonality ─────────────────────────────────────────────────────
    cot_bias, seas_bias, seas_up = load_cot_season(symbol)
    print(f"  COT: {cot_bias} | Seasonality: {seas_bias} (up {seas_up:.0f}% of time)")

    # ── Bias Synthesis ────────────────────────────────────────────────────────
    if direction is None:
        print(f"\n  RESULT: NO DIRECTION — {location}")
        return None

    signal, confidence, reasons = synthesize_bias(
        location, direction, val_label, val_score,
        cot_bias, seas_bias, trend, aclass
    )

    print(f"\n  BIAS SYNTHESIS:")
    for r in reasons:
        print(f"    • {r}")

    if signal:
        # Generate trade parameters from zone
        if signal == "LONG" and nearest_d:
            entry = price
            sl = nearest_d.distal
            tp = nearest_s.proximal if nearest_s else price + (price - sl) * 2
        elif signal == "SHORT" and nearest_s:
            entry = price
            sl = nearest_s.distal
            tp = nearest_d.proximal if nearest_d else price - (sl - price) * 2
        else:
            entry = price
            sl = price * (0.997 if signal == "LONG" else 1.003)
            tp = price * (1.006 if signal == "LONG" else 0.994)

        rr = abs(tp - entry) / abs(sl - entry) if abs(sl - entry) > 0 else 0

        print(f"\n  {'🟢' if signal == 'LONG' else '🔴'} SIGNAL: {signal} {symbol}")
        print(f"     Entry: {entry:.2f} | SL: {sl:.2f} | TP: {tp:.2f} | R:R: {rr:.2f}")
        print(f"     Confidence: {confidence}%")
        return {"symbol": symbol, "direction": "BUY" if signal == "LONG" else "SELL",
                "entry": round(entry, 5), "sl": round(sl, 5), "tp": round(tp, 5),
                "confidence": confidence, "reasons": reasons}
    else:
        print(f"\n  RESULT: NO TRADE")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    signals = []
    for sym in list(SYMBOLS.keys())[:5]:  # Analyze top 5
        result = analyze(sym)
        if result:
            signals.append(result)

    if signals:
        print(f"\n{'=' * 60}")
        print(f"ACTIVE SIGNALS: {len(signals)}")
        for s in signals:
            print(f"  {s['direction']} {s['symbol']} @ {s['entry']}, "
                  f"SL:{s['sl']}, TP:{s['tp']}, Conf:{s['confidence']}%")
    else:
        print(f"\n{'=' * 60}")
        print("NO SIGNALS — no instruments pass the hierarchy")
