"""
Signal Engine
Matches live market state against setup rules mined from top traders.
Generates trade signals: BUY/SELL, entry price, SL, TP, confidence.

Usage:
  from engine.signal_engine import SignalEngine
  se = SignalEngine()
  signals = se.scan(["XAU/USD", "EUR/USD", "GBP/USD"])
  for sig in signals:
      print(sig)

  # With auto-execution:
  se = SignalEngine(auto_execute=True)
  se.scan(["XAU/USD"])

Output:
  engine/signal_log.jsonl   — all generated signals (append-only log)
"""

import json, os, sys
from datetime import datetime, timedelta
from engine.market_state import MarketState
import MetaTrader5 as mt5

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR     = os.path.dirname(os.path.dirname(__file__))
RULES_FILE   = os.path.join(os.path.dirname(__file__), "setup_rules.json")
SIGNAL_LOG   = os.path.join(os.path.dirname(__file__), "signal_log.jsonl")

# ── Trade sizing ──────────────────────────────────────────────────────────────
DEFAULT_RISK_PCT  = 1.0      # risk % of account per trade
DEFAULT_MAX_SL_PCT = 2.0     # max SL distance as % of price
MIN_CONFIDENCE     = 55.0    # minimum rule win_rate % to fire
MIN_OCCURRENCES    = 10      # minimum rule occurrences to fire


class SignalEngine:
    def __init__(self, auto_execute=False, risk_pct=DEFAULT_RISK_PCT, dry_run=True):
        self.auto_execute = auto_execute
        self.risk_pct     = risk_pct
        self.dry_run      = dry_run
        self.market       = MarketState(connect_mt5=auto_execute)
        self.rules        = self._load_rules()
        self.signals_today = []

    def _load_rules(self):
        if not os.path.exists(RULES_FILE):
            print(f"[SignalEngine] No rules file at {RULES_FILE}")
            print("  Run engine/setup_miner.py first to generate rules.")
            return []
        with open(RULES_FILE) as f:
            rules = json.load(f)
        # Filter to quality rules only
        rules = [r for r in rules
                 if r.get("win_rate", 0) >= MIN_CONFIDENCE / 100
                 and r.get("occurrences", 0) >= MIN_OCCURRENCES]
        print(f"[SignalEngine] Loaded {len(rules)} active rules "
              f"(min {MIN_CONFIDENCE:.0f}% WR, {MIN_OCCURRENCES} occurrences)")
        return rules

    def _match_rule(self, state, rule):
        """
        Check if current market state matches a setup rule.
        Returns a match score 0-100, or 0 for no match.
        """
        score = 0

        # ── Symbol must match ─────────────────────────────────────────────────
        if state.get("symbol") != rule.get("symbol"):
            return 0

        # ── Session ────────────────────────────────────────────────────────────
        rule_session = rule.get("session", "")
        current_session = "|".join(state.get("session", []))
        if rule_session and rule_session not in current_session:
            return 0
        score += 20

        # ── Trend ──────────────────────────────────────────────────────────────
        rule_trend = rule.get("h4_trend", "")
        if rule_trend and rule_trend != "neutral":
            if state.get("h4_trend") != rule_trend:
                return 0
        score += 20

        # ── S/R zone proximity ─────────────────────────────────────────────────
        rule_zone = rule.get("sr_zone", "mid_range")
        if rule_zone.startswith("AT_PDH"):
            dist = state.get("pdh_dist_pct")
            if dist is not None and dist < 0.5:
                score += 25  # bonus for being right at the level
            elif dist is not None and dist < 1.0:
                score += 15  # near the level
            else:
                return 0  # too far from PDH
        elif rule_zone.startswith("AT_PDL"):
            dist = state.get("pdl_dist_pct")
            if dist is not None and dist < 0.5:
                score += 25
            elif dist is not None and dist < 1.0:
                score += 15
            else:
                return 0
        elif rule_zone.startswith("AT_ROUND"):
            # Check if near any round level
            if state.get("price") and state.get("round_levels"):
                price = state["price"]
                for rl in state["round_levels"]:
                    if abs(price - rl) / price < 0.003:
                        score += 25
                        break
                else:
                    score += 5  # not near a round level but OK
        else:  # mid_range
            score += 10

        # ── News proximity ─────────────────────────────────────────────────────
        # If rule was triggered by news, current state should match
        if rule.get("top_news_triggers"):
            if state.get("has_near_news"):
                score += 10  # news nearby = matches pattern
            # No penalty if no news — the setup can still work without news

        # ── Trend alignment bonus ──────────────────────────────────────────────
        if state.get("trend_aligned"):
            score += 10

        # ── Rule historical quality bonus ──────────────────────────────────────
        score += rule.get("win_rate", 0.5) * 15  # up to 15 bonus for high WR

        return min(score, 100)

    def _generate_sl_tp(self, state, rule):
        """Generate SL and TP levels based on rule context and current levels."""
        price = state.get("price", 0)
        if price <= 0:
            return None, None

        direction = rule.get("direction", "BUY")
        avg_rr = rule.get("avg_rr") or 1.5

        # SL: nearest support (for BUY) or resistance (for SELL)
        if direction == "BUY":
            sl = state.get("nearest_support") or state.get("pdl") or (price * 0.99)
        else:
            sl = state.get("nearest_resistance") or state.get("pdh") or (price * 1.01)

        # Validate SL distance
        sl_dist_pct = abs(price - sl) / price * 100
        if sl_dist_pct > DEFAULT_MAX_SL_PCT:
            sl = price * (0.995 if direction == "BUY" else 1.005)
        elif sl_dist_pct < 0.1:
            sl = price * (0.997 if direction == "BUY" else 1.003)

        # TP based on rule's historical R:R
        sl_dist = abs(price - sl)
        tp_dist = sl_dist * avg_rr
        tp = price + tp_dist if direction == "BUY" else price - tp_dist

        return round(float(sl), 5), round(float(tp), 5)

    def _calc_volume(self, state, sl):
        """Calculate position size based on risk %."""
        if sl is None or state.get("price", 0) <= 0:
            return 0.01
        try:
            account = mt5.account_info()
            if account is None:
                return 0.01
            balance = account.balance
            risk_amount = balance * self.risk_pct / 100
            sl_distance = abs(state["price"] - sl)
            if sl_distance <= 0:
                return 0.01
            # Approximate: 1 lot = $10 per pip for most FX pairs
            symbol = state["symbol"]
            if "XAU" in symbol:
                pip_value = 10  # gold: 1 lot = $10 per $1 move
                volume = risk_amount / (sl_distance * pip_value)
            elif "XAG" in symbol:
                volume = risk_amount / (sl_distance * 50)
            elif any(x in symbol for x in ("DJ30", "US500", "USTEC", "DE30", "UK100", "JPN225", "F40")):
                volume = risk_amount / (sl_distance * 100)
            elif "USDT" in symbol or "BTC" in symbol or "ETH" in symbol:
                volume = risk_amount / (sl_distance * 1)
            else:
                volume = risk_amount / (sl_distance * 1000)  # FX pairs

            volume = max(0.01, min(volume, 5.0))
            return round(volume, 2)
        except Exception:
            return 0.01

    def scan(self, symbols=None):
        """
        Scan all (or specified) symbols for trade signals.
        Returns list of signal dicts.
        """
        if not symbols:
            # Default: scan the most commonly traded symbols
            symbols = ["XAU/USD", "XAG/USD", "EUR/USD", "GBP/USD", "USD/JPY",
                        "DJ30", "USTEC.v", "US500", "BTCUSDT"]

        if not self.rules:
            print("[SignalEngine] No rules loaded. Run setup_miner.py first.")
            return []

        signals = []
        for symbol in symbols:
            state = self.market.get_state(symbol)
            if state.get("error"):
                continue

            for rule in self.rules:
                match_score = self._match_rule(state, rule)
                if match_score <= 0:
                    continue

                sl, tp = self._generate_sl_tp(state, rule)
                volume = self._calc_volume(state, sl)

                signal = {
                    "timestamp":     datetime.utcnow().isoformat(),
                    "symbol":        symbol,
                    "direction":     rule["direction"],
                    "entry_price":   state["price"],
                    "stop_loss":     sl,
                    "take_profit":   tp,
                    "volume":        volume,
                    "rule_id":       rule["id"],
                    "match_score":   round(match_score, 1),
                    "confidence":    round(rule["win_rate"] * 100, 1),
                    "rule_summary":  f"{rule['symbol']} {rule['direction']} | "
                                     f"{rule['session']} | {rule['h4_trend']} | "
                                     f"{rule['sr_zone']} | WR:{rule['win_rate']:.0%}",
                    "reason": {
                        "session_match": rule["session"],
                        "trend_match":   rule["h4_trend"],
                        "zone_match":    rule["sr_zone"],
                        "near_pdh":      state.get("pdh_dist_pct"),
                        "near_pdl":      state.get("pdl_dist_pct"),
                        "h4_trend":      state.get("h4_trend"),
                        "d1_trend":      state.get("d1_trend"),
                        "near_news":     state.get("near_news"),
                    },
                    "auto_executed": False,
                }

                signals.append(signal)

                # Auto-execute if enabled
                if self.auto_execute and not self.dry_run:
                    self._execute(signal)

        # Sort by match_score desc
        signals.sort(key=lambda s: s["match_score"], reverse=True)

        # Log all signals
        self._log_signals(signals)

        return signals

    def _execute(self, signal):
        """Place a trade via MT5."""
        if self.dry_run:
            signal["auto_executed"] = False
            signal["execution_note"] = "DRY RUN — not placed"
            return

        symbol = signal["symbol"]
        direction = signal["direction"]
        volume = signal["volume"]
        sl = signal["stop_loss"]
        tp = signal["take_profit"]

        # Ensure symbol is in Market Watch
        mt5.symbol_select(symbol, True)

        order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
        price = mt5.symbol_info_tick(symbol).ask if direction == "BUY" else mt5.symbol_info_tick(symbol).bid

        request = {
            "action":    mt5.TRADE_ACTION_DEAL,
            "symbol":    symbol,
            "volume":    volume,
            "type":      order_type,
            "price":     price,
            "sl":        sl,
            "tp":        tp,
            "deviation": 20,
            "magic":     773300,  # unique ID for this EA
            "comment":   f"RF_SETUP_{signal['rule_id']}",
        }

        result = mt5.order_send(request)
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            signal["auto_executed"] = True
            signal["ticket"] = result.order
            signal["execution_note"] = f"Executed: ticket #{result.order}"
            print(f"  ✅ EXECUTED: {signal['symbol']} {signal['direction']} "
                  f"@{signal['entry_price']:.2f} SL:{sl} TP:{tp}")
        else:
            signal["auto_executed"] = False
            signal["execution_note"] = f"Failed: {result.comment}"
            print(f"  ❌ FAILED: {signal['symbol']} — {result.comment}")

    def _log_signals(self, signals):
        """Append signals to the signal log."""
        if not signals:
            return
        os.makedirs(os.path.dirname(SIGNAL_LOG), exist_ok=True)
        with open(SIGNAL_LOG, "a") as f:
            for sig in signals:
                f.write(json.dumps(sig, default=str) + "\n")

    def get_daily_summary(self):
        """Return summary of today's signals."""
        today = datetime.utcnow().date()
        if not os.path.exists(SIGNAL_LOG):
            return []
        today_signals = []
        with open(SIGNAL_LOG) as f:
            for line in f:
                try:
                    sig = json.loads(line)
                    sig_dt = datetime.fromisoformat(sig["timestamp"]).date()
                    if sig_dt == today:
                        today_signals.append(sig)
                except Exception:
                    pass
        return today_signals

    def shutdown(self):
        self.market.shutdown()


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Signal Engine")
    parser.add_argument("--symbols", nargs="*", default=None, help="Symbols to scan")
    parser.add_argument("--execute", action="store_true", help="Auto-execute trades (requires MT5)")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Dry run (default)")
    parser.add_argument("--live", action="store_true", help="Live trading (no dry run)")
    args = parser.parse_args()

    se = SignalEngine(auto_execute=args.execute or args.live, dry_run=not args.live)
    signals = se.scan(args.symbols)

    if signals:
        print(f"\n{'=' * 80}")
        print(f"SIGNALS GENERATED: {len(signals)}")
        print(f"{'=' * 80}")
        for sig in signals[:10]:
            print(f"  {sig['direction']:<5} {sig['symbol']:<12} "
                  f"Entry:{sig['entry_price']:.2f} SL:{sig['stop_loss']:.2f} "
                  f"TP:{sig['take_profit']:.2f} | Score:{sig['match_score']:.0f}% "
                  f"| {sig['rule_summary'][:50]}")
    else:
        print("\nNo signals generated. No rules matched current market conditions.")

    se.shutdown()
