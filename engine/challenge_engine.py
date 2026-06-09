"""
Prop Firm Challenge Engine
===========================
Built for: Standard Funding / FTMO Step 2 challenges
Rules:   5% daily loss limit, 10% max drawdown
Method:  Bernd Skorupinski (120x FTMO winner) multi-bias approach
         + Rebel Funding winner patterns
         + Strict risk management

Core principle: Only trade when MULTIPLE dimensions agree.
Solo signals = skip. Consensus = trade.

Run modes:
  python engine/challenge_engine.py paper    # Paper trade (no real orders)
  python engine/challenge_engine.py live     # Live MT5 execution
"""

import os, sys, json, time
from datetime import datetime, timedelta
from collections import defaultdict

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, BASE_DIR)

from engine.market_state import MarketState
import MetaTrader5 as mt5

# ═══════════════════════════════════════════════════════════════════════════════
# CHALLENGE RULES (adjust per your specific challenge)
# ═══════════════════════════════════════════════════════════════════════════════

ACCOUNT_SIZE        = 100_000       # $100,000 challenge account
MAX_DAILY_LOSS      = 0.05          # 5% daily loss limit
MAX_TOTAL_DRAWDOWN  = 0.10          # 10% max drawdown
RISK_PER_TRADE      = 0.005         # 0.5% risk per trade
MAX_POSITIONS       = 2             # max concurrent trades
DAILY_STOP_BUFFER   = 0.04          # hard-stop day at 4% loss (buffer before 5%)
MIN_BIAS_CONSENSUS  = 2             # need 2+ dimensions agreeing for entry (was 3, lowered since COT unavailable)

# Bias dimensions we compute:
# 1. NEWS direction (from event_rules.json)
# 2. COT positioning (from cot_positions.parquet)
# 3. TECHNICAL (S/R proximity + trend from market_state)
# 4. SEASONALITY (monthly bias from historical D1 data)
# 5. REBEL WINNERS (from setup_rules.json)

SYMBOLS = ["XAU/USD", "EUR/USD", "GBP/USD", "USD/JPY", "XAG/USD"]

EVENT_RULES_FILE = os.path.join(os.path.dirname(__file__), "event_rules.json")
SETUP_RULES_FILE = os.path.join(os.path.dirname(__file__), "setup_rules.json")
COT_FILE         = os.path.join(os.path.dirname(__file__), "cot_simulated.parquet")
SEASON_FILE      = os.path.join(os.path.dirname(__file__), "seasonality.parquet")
CHALLENGE_LOG    = os.path.join(os.path.dirname(__file__), "challenge_log.jsonl")

# ═══════════════════════════════════════════════════════════════════════════════
# RISK MANAGER
# ═══════════════════════════════════════════════════════════════════════════════

class RiskManager:
    def __init__(self, account_size=ACCOUNT_SIZE):
        self.initial_balance = account_size
        self.current_balance = account_size
        self.daily_pnl       = 0.0
        self.total_pnl       = 0.0
        self.trade_date      = None
        self.open_positions  = []
        self.trades_today     = 0
        self.total_trades     = 0
        self.wins             = 0
        self.losses           = 0
        self.stopped_out      = False
        self.stop_reason      = ""

    def _reset_daily(self, now=None):
        if now is None:
            now = datetime.utcnow()
        today = now.date()
        if self.trade_date != today:
            self.trade_date = today
            self.daily_pnl  = 0.0
            self.trades_today = 0

    def can_trade(self, now=None):
        """Check if we're allowed to take a new trade."""
        if now is None:
            now = datetime.utcnow()
        self._reset_daily(now)

        # Hard stops
        if self.stopped_out:
            return False, self.stop_reason

        # Daily loss limit approaching
        daily_loss_pct = abs(min(self.daily_pnl, 0)) / self.initial_balance
        if daily_loss_pct >= DAILY_STOP_BUFFER:
            self.stopped_out = True
            self.stop_reason = f"Daily loss buffer hit ({daily_loss_pct:.1%})"
            return False, self.stop_reason

        # Max drawdown
        drawdown_pct = abs(min(self.total_pnl, 0)) / self.initial_balance
        if drawdown_pct >= MAX_TOTAL_DRAWDOWN:
            self.stopped_out = True
            self.stop_reason = f"Max drawdown hit ({drawdown_pct:.1%})"
            return False, self.stop_reason

        # Too many positions
        if len(self.open_positions) >= MAX_POSITIONS:
            return False, f"Max positions ({MAX_POSITIONS}) open"

        # Too many trades today
        if self.trades_today >= 6:
            return False, "Max daily trades (6)"

        return True, "OK"

    def position_size(self, entry_price, stop_loss, symbol):
        """Calculate lots based on 0.5% risk per trade."""
        risk_amount = self.initial_balance * RISK_PER_TRADE  # $500 on $100k
        sl_distance = abs(entry_price - stop_loss)
        if sl_distance <= 0:
            return 0.01

        # Approximate pip values
        if "XAU" in symbol:
            pip_val = 10    # $10 per $1 move per 0.01 lot on gold → $1 per 0.1 lot per $1
            # Actually: 1 lot XAUUSD = $100 per $1 move. 0.01 lot = $1 per $1 move.
            # risk_amount / sl_distance gives lots needed
            lots = risk_amount / (sl_distance * 100)
        elif "XAG" in symbol:
            lots = risk_amount / (sl_distance * 500)
        elif any(x in symbol for x in ("DJ30","US500","USTEC","DE30","UK100","JPN225")):
            lots = risk_amount / (sl_distance * 10)
        elif "USDT" in symbol or "BTC" in symbol or "ETH" in symbol:
            lots = risk_amount / (sl_distance * 1)
        else:  # FX pairs
            lots = risk_amount / (sl_distance * 1000)

        lots = max(0.01, min(lots, 3.0))
        return round(lots, 2)

    def record_trade(self, pnl):
        """Record a closed trade P&L."""
        self.daily_pnl    += pnl
        self.total_pnl    += pnl
        self.current_balance += pnl
        self.trades_today += 1
        self.total_trades += 1
        if pnl > 0:
            self.wins += 1
        else:
            self.losses += 1

    def status(self):
        daily = abs(min(self.daily_pnl, 0)) / self.initial_balance * 100
        total = abs(min(self.total_pnl, 0)) / self.initial_balance * 100
        wr = self.wins / max(self.total_trades, 1) * 100
        return (f"Balance: ${self.current_balance:,.0f} | "
                f"Daily P&L: {self.daily_pnl:+.1f} ({daily:.1f}%) | "
                f"Total P&L: {self.total_pnl:+.1f} ({total:.1f}%) | "
                f"WR: {wr:.0f}% | Trades: {self.total_trades}")


# ═══════════════════════════════════════════════════════════════════════════════
# BIAS COMPUTER (multi-dimensional signal generation)
# ═══════════════════════════════════════════════════════════════════════════════

class BiasComputer:
    def __init__(self, market):
        self.market = market
        self.event_rules = self._load_json(EVENT_RULES_FILE)
        self.setup_rules = self._load_json(SETUP_RULES_FILE)
        self.cot = self._load_parquet(COT_FILE)
        self.season = self._load_parquet(SEASON_FILE)

    def _load_json(self, path):
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
        return []

    def _load_parquet(self, path):
        if os.path.exists(path):
            import pandas as pd
            return pd.read_parquet(path)
        return None

    def compute_bias(self, symbol, state):
        """
        Returns a dict with bias scores for each dimension.
        Each dimension: direction ("BUY"/"SELL"/None) + confidence (0-100)
        """
        biases = {}

        # ── 1. TECHNICAL bias (S/R + trend) ──────────────────────────────────
        tech_dir = None
        tech_conf = 0

        trend = state.get("h4_trend", "neutral")
        pdh_dist = state.get("pdh_dist_pct")
        pdl_dist = state.get("pdl_dist_pct")

        # Support + uptrend = bullish
        if pdl_dist is not None and pdl_dist < 0.5 and trend == "UP":
            tech_dir = "BUY"
            tech_conf = 70
        elif pdl_dist is not None and pdl_dist < 0.5:
            tech_dir = "BUY"
            tech_conf = 55
        # Resistance + downtrend = bearish
        elif pdh_dist is not None and pdh_dist < 0.5 and trend == "DOWN":
            tech_dir = "SELL"
            tech_conf = 70
        elif pdh_dist is not None and pdh_dist < 0.5:
            tech_dir = "SELL"
            tech_conf = 55
        # Trend-only
        elif trend == "UP":
            tech_dir = "BUY"
            tech_conf = 40
        elif trend == "DOWN":
            tech_dir = "SELL"
            tech_conf = 40

        biases["technical"] = {"direction": tech_dir, "confidence": tech_conf}

        # ── 2. COT bias (smart money positioning) ─────────────────────────────
        cot_dir = None
        cot_conf = 0
        if self.cot is not None:
            cot_row = self.cot[self.cot["symbol"] == symbol]
            if not cot_row.empty:
                latest = cot_row.iloc[-1]
                idx = latest.get("cot_index", 0)   # simulated COT index (-100 to +100)
                if idx > 20:
                    cot_dir = "BUY"
                    cot_conf = min(abs(idx) * 0.8, 90)
                elif idx < -20:
                    cot_dir = "SELL"
                    cot_conf = min(abs(idx) * 0.8, 90)
        biases["cot"] = {"direction": cot_dir, "confidence": round(cot_conf, 1)}

        # ── 3. NEWS bias (pending/active events) ──────────────────────────────
        news_dir = None
        news_conf = 0
        if state.get("has_near_news"):
            for ev in state.get("near_news", []):
                ev_name = ev.get("event", "")
                surprise = ev.get("surprise", "")
                # Check event rules for this event+symbol
                for rule in self.event_rules:
                    if (rule["symbol"] == symbol and
                        rule["event"][:20] in ev_name and
                        rule["occurrences"] >= 3):
                        news_dir = "BUY" if rule["price_dir"] == "UP" else "SELL"
                        news_conf = rule["confidence"]
                        break
        biases["news"] = {"direction": news_dir, "confidence": news_conf}

        # ── 4. SEASONALITY bias (monthly pattern from 10yr history) ────────────
        season_dir = None
        season_conf = 0
        current_month = datetime.now().month
        if self.season is not None:
            sm = self.season[
                (self.season["symbol"] == symbol) &
                (self.season["month"] == current_month)
            ]
            if not sm.empty:
                r = sm.iloc[0]
                if r["bias"] == "BULLISH":
                    season_dir = "BUY"
                    season_conf = r["up_pct"] * 0.8
                elif r["bias"] == "BEARISH":
                    season_dir = "SELL"
                    season_conf = (100 - r["up_pct"]) * 0.8
        biases["seasonality"] = {"direction": season_dir, "confidence": round(season_conf, 1)}

        # ── 5. REBEL WINNERS bias ─────────────────────────────────────────────
        rebel_dir = None
        rebel_conf = 0
        # Get current session
        current_hour = datetime.now().hour
        session = ""
        if 0 <= current_hour < 9: session = "Asia"
        elif 8 <= current_hour < 13: session = "London"
        elif 13 <= current_hour < 17: session = "London+NY"
        elif 17 <= current_hour < 22: session = "NY"

        # Check if there's a high-confidence setup rule matching current state
        for rule in self.setup_rules:
            if (rule["symbol"] == symbol
                and rule["occurrences"] >= 20
                and rule["win_rate"] >= 0.70
                and rule["session"] == session
                and rule["h4_trend"] == trend):
                rebel_dir = rule["direction"]
                rebel_conf = rule["win_rate"] * 80
                break
        biases["rebel_winners"] = {"direction": rebel_dir, "confidence": rebel_conf}

        return biases

    def consensus(self, biases):
        """
        Count how many dimensions agree on direction.
        Returns (direction, vote_count, total_confidence)
        """
        votes = {"BUY": 0, "SELL": 0}
        conf  = {"BUY": 0, "SELL": 0}
        for dim, b in biases.items():
            d = b["direction"]
            if d in votes:
                votes[d] += 1
                conf[d] += b["confidence"]

        dominant = "BUY" if votes["BUY"] > votes["SELL"] else "SELL"
        count = votes[dominant]
        total_conf = conf[dominant]
        return dominant, count, total_conf


# ═══════════════════════════════════════════════════════════════════════════════
# CHALLENGE ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════════════════════

class ChallengeEngine:
    def __init__(self, dry_run=True):
        self.dry_run = dry_run
        self.risk    = RiskManager()
        self.market  = MarketState(connect_mt5=not dry_run)
        self.bias    = BiasComputer(self.market)
        self.signals_generated = 0
        self.trades_taken      = 0

    def scan(self):
        """One scan cycle: check all symbols, generate signals if consensus."""
        signals = []

        for symbol in SYMBOLS:
            state = self.market.get_state(symbol)
            if state.get("error"):
                continue

            # Compute multi-dimensional bias
            biases = self.bias.compute_bias(symbol, state)
            direction, votes, conf = self.bias.consensus(biases)

            # Only fire if minimum consensus met
            if votes < MIN_BIAS_CONSENSUS:
                continue

            # Risk check
            can, reason = self.risk.can_trade()
            if not can:
                continue

            # Generate SL/TP
            price = state["price"]
            if direction == "BUY":
                sl = state.get("nearest_support") or state.get("pdl") or (price * 0.997)
                tp = price + (price - sl) * 2.0  # 1:2 R:R minimum
            else:
                sl = state.get("nearest_resistance") or state.get("pdh") or (price * 1.003)
                tp = price - (sl - price) * 2.0

            volume = self.risk.position_size(price, sl, symbol)

            # Bias breakdown for logging
            bias_summary = " | ".join(
                f"{k}:{v['direction'] or '-'}({v['confidence']:.0f})"
                for k, v in biases.items()
            )

            signal = {
                "timestamp":   datetime.utcnow().isoformat(),
                "symbol":      symbol,
                "direction":   direction,
                "entry_price": round(price, 5),
                "stop_loss":   round(sl, 5),
                "take_profit": round(tp, 5),
                "volume":      volume,
                "votes":       votes,
                "confidence":  round(conf / votes, 1) if votes > 0 else 0,  # avg conf
                "biases":      bias_summary,
                "ticket":      None,
            }

            signals.append(signal)
            self.signals_generated += 1

        # Sort by confidence, take top
        signals.sort(key=lambda s: s["votes"] * s["confidence"], reverse=True)
        return signals[:2]  # max 2 signals per scan

    def run_once(self):
        """Run one scan cycle, print results."""
        print(f"\n{'=' * 70}")
        print(f"SCAN — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
        print(f"{'=' * 70}")
        print(f"  {self.risk.status()}")

        signals = self.scan()

        if not signals:
            status = "STOPPED" if self.risk.stopped_out else "NO SIGNALS"
            if self.risk.stopped_out:
                print(f"  ⛔ {status} — {self.risk.stop_reason}")
            else:
                print(f"  ⚪ {status} — no consensus signals ({MIN_BIAS_CONSENSUS}+ dimensions needed)")
            return []

        for sig in signals:
            emoji = "🟢" if sig["direction"] == "BUY" else "🔴"
            print(f"\n  {emoji} {sig['direction']} {sig['symbol']}")
            print(f"     Entry: {sig['entry_price']:.2f} | SL: {sig['stop_loss']:.2f} "
                  f"| TP: {sig['take_profit']:.2f} | Lots: {sig['volume']}")
            print(f"     Votes: {sig['votes']}/5 dims agree | Avg conf: {sig['confidence']:.0f}%")
            print(f"     Biases: {sig['biases']}")

            if not self.dry_run:
                self._execute(sig)

        self._log(signals)
        return signals

    def _execute(self, signal):
        """Send order to MT5."""
        symbol = signal["symbol"]
        mt5.symbol_select(symbol, True)
        order_type = mt5.ORDER_TYPE_BUY if signal["direction"] == "BUY" else mt5.ORDER_TYPE_SELL
        tick = mt5.symbol_info_tick(symbol)
        price = tick.ask if signal["direction"] == "BUY" else tick.bid

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": signal["volume"],
            "type": order_type,
            "price": price,
            "sl": signal["stop_loss"],
            "tp": signal["take_profit"],
            "deviation": 20,
            "magic": 882200,
            "comment": f"RF_CHALLENGE_V{signal['votes']}",
        }
        result = mt5.order_send(request)
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            signal["ticket"] = result.order
            self.trades_taken += 1
            print(f"     ✅ EXECUTED — Ticket #{result.order}")
        else:
            print(f"     ❌ FAILED — {result.comment}")

    def _log(self, signals):
        os.makedirs(os.path.dirname(CHALLENGE_LOG), exist_ok=True)
        with open(CHALLENGE_LOG, "a") as f:
            for s in signals:
                f.write(json.dumps(s, default=str) + "\n")

    def shutdown(self):
        self.market.shutdown()

    def summary(self):
        print(f"\n{'=' * 70}")
        print(f"CHALLENGE STATUS")
        print(f"{'=' * 70}")
        print(f"  {self.risk.status()}")
        print(f"  Signals generated: {self.signals_generated}")
        print(f"  Trades taken:      {self.trades_taken}")
        if self.risk.stopped_out:
            print(f"  ⛔ STOPPED: {self.risk.stop_reason}")
        print(f"{'=' * 70}")


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Prop Firm Challenge Engine")
    ap.add_argument("mode", nargs="?", default="paper",
                    choices=["paper", "live", "watch"],
                    help="paper=no execution | live=execute via MT5 | watch=continuous")
    args = ap.parse_args()

    engine = ChallengeEngine(dry_run=(args.mode != "live"))

    if args.mode == "watch":
        print("=" * 60)
        print("CHALLENGE WATCH MODE — scanning every 15 min")
        print("Press Ctrl+C to stop")
        print("=" * 60)
        try:
            while True:
                engine.run_once()
                engine.summary()
                if engine.risk.stopped_out:
                    break
                time.sleep(900)  # 15 minutes
        except KeyboardInterrupt:
            print("\nShutting down...")
        finally:
            engine.shutdown()
    else:
        engine.run_once()
        engine.summary()
        engine.shutdown()
