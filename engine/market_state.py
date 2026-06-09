"""
Market State Engine
Reads live MT5 candles and computes the current market picture:
  - S/R levels (PDH/PDL, weekly H/L, round numbers, recent swing points)
  - HTF trend (H4 and D1 direction)
  - Current session (Asia/London/NY)
  - News proximity (any high-impact events nearby?)
  - Recent price action (last few candles)

Usage:
  from engine.market_state import MarketState
  ms = MarketState()
  state = ms.get_state("XAU/USD")
  # state = {symbol, price, pdh, pdl, weekly_high, weekly_low, round_levels,
  #          h4_trend, d1_trend, session, near_news, recent_candles, ...}
"""

import MetaTrader5 as mt5
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import os, sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CANDLE_DIR = r"c:\Users\Administrator\Documents\Rebel Funding\candle_data"
NEWS_FILE  = r"c:\Users\Administrator\Documents\Rebel Funding\news_calendar.parquet"

# Session hours (UTC)
SESSIONS = {
    "Asia":    (0, 9),
    "London":  (8, 17),
    "NY":      (13, 22),
}

TIMEFRAMES = {
    "M15": mt5.TIMEFRAME_M15,
    "H1":  mt5.TIMEFRAME_H1,
    "H4":  mt5.TIMEFRAME_H4,
    "D1":  mt5.TIMEFRAME_D1,
}


class MarketState:
    def __init__(self, connect_mt5=True):
        self._candle_cache = {}
        self._news = None
        if connect_mt5:
            self._connect()

    def _connect(self):
        if not mt5.initialize():
            print(f"[MarketState] MT5 init failed: {mt5.last_error()}")
        else:
            info = mt5.account_info()
            if info:
                print(f"[MarketState] MT5 connected: {info.server}")

    def _load_news(self):
        if self._news is None and os.path.exists(NEWS_FILE):
            df = pd.read_parquet(NEWS_FILE)
            for col in ["datetime", "date"]:
                if col in df.columns:
                    df[col] = pd.to_datetime(df[col])
            self._news = df[df["impact"] == "High"].copy()
        return self._news

    def _get_candles(self, symbol, tf="H1", lookback=200):
        """Get recent candles from MT5 (live) or fall back to cached parquet."""
        tf_code = TIMEFRAMES.get(tf)
        if tf_code is None:
            return None

        # Try live MT5 first
        try:
            rates = mt5.copy_rates_from_pos(symbol, tf_code, 0, lookback)
            if rates is not None and len(rates) > 0:
                df = pd.DataFrame(rates)
                df["time"] = pd.to_datetime(df["time"], unit="s")
                df = df[["time", "open", "high", "low", "close", "tick_volume"]]
                df.columns = ["time", "open", "high", "low", "close", "volume"]
                return df.sort_values("time").reset_index(drop=True)
        except Exception:
            pass

        # Fall back to cached parquet
        key = f"{symbol}_{tf}"
        if key in self._candle_cache:
            return self._candle_cache[key].tail(lookback)

        fname = symbol.replace("/", "_").replace(".", "_") + ".parquet"
        path = os.path.join(CANDLE_DIR, tf, fname)
        if os.path.exists(path):
            df = pd.read_parquet(path)
            df["time"] = pd.to_datetime(df["time"])
            self._candle_cache[key] = df
            return df.tail(lookback)
        return None

    def _calc_swing_points(self, candles, n=5):
        """Simple swing high/low detection using n-candle windows."""
        if candles is None or len(candles) < n * 2:
            return [], []
        highs = candles["high"].values
        lows = candles["low"].values
        swing_highs = []
        swing_lows = []
        for i in range(n, len(highs) - n):
            if highs[i] == max(highs[i-n:i+n+1]):
                swing_highs.append(float(highs[i]))
            if lows[i] == min(lows[i-n:i+n+1]):
                swing_lows.append(float(lows[i]))
        return swing_highs, swing_lows

    def _round_levels(self, price, n=4):
        """Generates nearest round numbers (psychological levels)."""
        if price <= 0:
            return []
        # Determine scale
        if price > 100:
            step = 50   # indices, gold
        elif price > 10:
            step = 5
        elif price > 1:
            step = 0.5  # most FX
        else:
            step = 0.05  # sub-dollar FX
        base = round(price / step) * step
        levels = []
        for i in range(-n, n + 1):
            levels.append(round(base + i * step, 5))
        return sorted(levels)

    def _current_session(self, dt=None):
        """Determine which session(s) we're in."""
        if dt is None:
            dt = datetime.utcnow()
        hour = dt.hour + dt.minute / 60
        active = []
        for name, (start, end) in SESSIONS.items():
            if start <= hour < end:
                active.append(name)
        return active if active else ["Off-hours"]

    def _near_news(self, symbol, now=None, window_hours=2):
        """Check for high-impact news near now for the given symbol."""
        news_df = self._load_news()
        if news_df is None or news_df.empty:
            return []
        if now is None:
            now = datetime.utcnow()

        window_start = now - timedelta(hours=window_hours)
        window_end = now + timedelta(hours=window_hours)

        nearby = news_df[
            (news_df["datetime"] >= window_start) &
            (news_df["datetime"] <= window_end)
        ]
        if nearby.empty:
            return []

        # Filter to events affecting this symbol
        relevant = []
        for _, ev in nearby.iterrows():
            instruments = str(ev.get("instruments", "")).split(",")
            if symbol in instruments or not instruments[0]:
                relevant.append({
                    "time": ev["datetime"],
                    "event": ev["event"],
                    "currency": ev["currency"],
                    "actual": ev.get("actual", ""),
                    "forecast": ev.get("forecast", ""),
                    "surprise": ev.get("surprise", ""),
                })
        return relevant

    def get_state(self, symbol):
        """
        Returns a complete market state dict for a given symbol.
        Call this whenever you need to evaluate trading conditions.
        """
        now = datetime.utcnow()

        # ── Candles ───────────────────────────────────────────────────────────
        h1 = self._get_candles(symbol, "H1", 200)
        h4 = self._get_candles(symbol, "H4", 100)
        d1 = self._get_candles(symbol, "D1", 50)

        current_price = float(h1["close"].iloc[-1]) if h1 is not None and len(h1) > 0 else None
        if current_price is None:
            return {"symbol": symbol, "error": "No candle data"}

        # ── Key levels ────────────────────────────────────────────────────────
        # Previous day high/low
        today = now.date()
        prev_day = today - timedelta(days=1)
        prev_candles = h1[h1["time"].dt.date == prev_day] if h1 is not None else None
        pdh = float(prev_candles["high"].max()) if prev_candles is not None and not prev_candles.empty else None
        pdl = float(prev_candles["low"].min())  if prev_candles is not None and not prev_candles.empty else None

        # Weekly high/low
        week_start = today - timedelta(days=today.weekday())
        week_candles = h1[h1["time"].dt.date >= week_start] if h1 is not None else None
        weekly_high = float(week_candles["high"].max()) if week_candles is not None and not week_candles.empty else None
        weekly_low  = float(week_candles["low"].min())  if week_candles is not None and not week_candles.empty else None

        # Round numbers
        rounds = self._round_levels(current_price)

        # Swing points from H4
        sw_highs, sw_lows = self._calc_swing_points(h4) if h4 is not None else ([], [])

        # ── Trend ──────────────────────────────────────────────────────────────
        def trend(candles, fast=20, slow=50):
            if candles is None or len(candles) < slow:
                return "neutral"
            ma_fast = candles["close"].rolling(fast).mean().iloc[-1]
            ma_slow = candles["close"].rolling(slow).mean().iloc[-1]
            if ma_fast > ma_slow * 1.005:
                return "UP"
            elif ma_fast < ma_slow * 0.995:
                return "DOWN"
            return "neutral"

        h4_trend = trend(h4)
        d1_trend = trend(d1)

        # ── Session ────────────────────────────────────────────────────────────
        session = self._current_session(now)

        # ── News proximity ─────────────────────────────────────────────────────
        news = self._near_news(symbol, now)

        # ── Distance to nearest key levels (as % of price) ─────────────────────
        def dist_pct(level):
            if level is None or current_price == 0:
                return None
            return round(abs(current_price - level) / current_price * 100, 3)

        state = {
            "symbol":        symbol,
            "timestamp":     now.isoformat(),
            "price":         current_price,

            # Key levels
            "pdh":           pdh,
            "pdl":           pdl,
            "pdh_dist_pct":  dist_pct(pdh),
            "pdl_dist_pct":  dist_pct(pdl),
            "weekly_high":   weekly_high,
            "weekly_low":    weekly_low,
            "round_levels":  rounds,
            "swing_highs":   sorted(sw_highs, reverse=True)[:5],
            "swing_lows":    sorted(sw_lows)[:5],

            # Nearest S/R
            "nearest_resistance": self._nearest_level(current_price, (sw_highs + [pdh, weekly_high] + rounds), above=True),
            "nearest_support":    self._nearest_level(current_price, (sw_lows  + [pdl, weekly_low]  + rounds), above=False),

            # Trend
            "h4_trend":      h4_trend,
            "d1_trend":      d1_trend,
            "trend_aligned": h4_trend == d1_trend and h4_trend != "neutral",

            # Session
            "session":       session,
            "is_asia":       "Asia" in session,
            "is_london":     "London" in session,
            "is_ny":         "NY" in session,

            # News
            "near_news":     news,
            "has_near_news": len(news) > 0,
        }

        return state

    def _nearest_level(self, price, levels, above=True):
        """Find the nearest level above or below current price."""
        valid = [lvl for lvl in levels if lvl is not None and lvl > 0]
        if not valid:
            return None
        if above:
            candidates = [lvl for lvl in valid if lvl > price]
            return min(candidates) if candidates else None
        else:
            candidates = [lvl for lvl in valid if lvl < price]
            return max(candidates) if candidates else None

    def get_multi_state(self, symbols):
        """Get state for multiple symbols at once."""
        return {s: self.get_state(s) for s in symbols}

    def shutdown(self):
        mt5.shutdown()


# ── CLI test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    ms = MarketState()
    state = ms.get_state("XAU/USD")
    for k, v in state.items():
        print(f"  {k:<20s}: {v}")
    ms.shutdown()
