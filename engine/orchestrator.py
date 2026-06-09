"""
Trading System Orchestrator
============================
Master controller: mines setups from historical data, then watches
live markets for matching conditions and generates trade signals.

Run modes:
  python engine/orchestrator.py mine       # Run setup miner (update rules)
  python engine/orchestrator.py scan       # One market scan + signals
  python engine/orchestrator.py watch      # Continuous monitoring (every 15 min)
  python engine/orchestrator.py live       # Continuous + auto-execute trades

Config at top of file: SYMBOLS, CHECK_INTERVAL_MIN, AUTO_EXECUTE, etc.
"""

import os, sys, time
from datetime import datetime, timedelta

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Add parent to path so engine imports work
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from engine.setup_miner import main as mine_setups
from engine.signal_engine import SignalEngine

# ── Configuration ─────────────────────────────────────────────────────────────
SYMBOLS = [
    "XAU/USD", "XAG/USD", "EUR/USD", "GBP/USD", "USD/JPY",
    "AUD/USD", "NZD/USD", "USD/CAD", "USD/CHF",
    "DJ30", "USTEC.v", "US500",
    "BTCUSDT", "ETHUSDT",
]

CHECK_INTERVAL_MIN  = 15     # minutes between market scans
MAX_SIGNALS_PER_SCAN = 5     # only fire top N signals per scan
REBUILD_RULES_EVERY   = 24   # hours between rule regeneration
SLEEP_BETWEEN_SYMBOLS = 1    # seconds between symbol lookups (polite to MT5)
DRY_RUN = True               # False = execute real trades

# Session filter: only scan during active sessions
SCAN_SESSIONS = ["London", "London+NY", "NY"]


def banner(text):
    print(f"\n{'=' * 60}")
    print(f"  {text}")
    print(f"{'=' * 60}")


def run_miner():
    """Rebuild setup rules from latest trader data."""
    banner("MINING SETUPS FROM TOP TRADERS")
    mine_setups()
    print("\nSetup rules updated. Ready for signal generation.")


def run_scan(auto_execute=False, symbols=None):
    """Single market scan."""
    if symbols is None:
        symbols = SYMBOLS

    banner(f"MARKET SCAN — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")

    se = SignalEngine(auto_execute=auto_execute, dry_run=not auto_execute)

    # Filter to active session symbols only
    now = datetime.utcnow()
    hour = now.hour + now.minute / 60
    current_sessions = []
    if 0 <= hour < 9:
        current_sessions = ["Asia"]
    elif 8 <= hour < 13:
        current_sessions = ["London"]
    elif 13 <= hour < 17:
        current_sessions = ["London+NY"]
    elif 17 <= hour < 22:
        current_sessions = ["NY"]
    else:
        current_sessions = ["Off-hours"]

    if not any(s in SCAN_SESSIONS for s in current_sessions):
        print(f"  Current session: {current_sessions} — outside scan window. Skipping.")
        se.shutdown()
        return []

    print(f"  Session: {current_sessions} | Auto-execute: {auto_execute} | "
          f"Symbols: {len(symbols)} | Rules: {len(se.rules)}")

    signals = se.scan(symbols)

    if signals:
        top = signals[:MAX_SIGNALS_PER_SCAN]
        print(f"\n  Top {len(top)} signals:")
        for sig in top:
            emoji = "🟢" if sig["direction"] == "BUY" else "🔴"
            print(f"  {emoji} {sig['direction']:<5} {sig['symbol']:<12} "
                  f"E:{sig['entry_price']:.2f} SL:{sig['stop_loss']:.2f} "
                  f"TP:{sig['take_profit']:.2f} "
                  f"| {sig['match_score']:.0f}% | {sig['rule_summary'][:60]}")
            if sig.get("auto_executed"):
                print(f"     ✅ EXECUTED — Ticket #{sig.get('ticket')}")
    else:
        print("  No signals — no rules matched current market conditions.")

    se.shutdown()
    return signals


def run_watch(auto_execute=False):
    """Continuous monitoring loop."""
    banner(f"WATCH MODE — Scanning every {CHECK_INTERVAL_MIN} minutes")
    print(f"  Auto-execute: {auto_execute} | Dry-run: {DRY_RUN}")
    print(f"  Symbols: {len(SYMBOLS)} | Press Ctrl+C to stop\n")

    last_rebuild = None

    try:
        while True:
            # Rebuild rules periodically
            if (last_rebuild is None or
                datetime.utcnow() - last_rebuild > timedelta(hours=REBUILD_RULES_EVERY)):
                run_miner()
                last_rebuild = datetime.utcnow()

            # Scan
            run_scan(auto_execute=auto_execute)

            # Wait
            next_scan = datetime.utcnow() + timedelta(minutes=CHECK_INTERVAL_MIN)
            print(f"\n  Next scan: {next_scan.strftime('%H:%M:%S UTC')} "
                  f"({CHECK_INTERVAL_MIN} min)")
            time.sleep(CHECK_INTERVAL_MIN * 60)

    except KeyboardInterrupt:
        print("\n\nShutting down...")
    finally:
        print("Trading engine stopped.")


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Rebel Funding Trading Orchestrator")
    ap.add_argument("mode", nargs="?", default="scan",
                    choices=["mine", "scan", "watch", "live"],
                    help="mine=update rules | scan=single check | watch=continuous | live=trade")
    ap.add_argument("--symbols", nargs="*", default=None,
                    help="Symbols to scan (default: configured list)")
    args = ap.parse_args()

    if args.mode == "mine":
        run_miner()
    elif args.mode == "scan":
        run_scan(auto_execute=False, symbols=args.symbols)
    elif args.mode == "watch":
        run_watch(auto_execute=False)
    elif args.mode == "live":
        confirm = input("\n⚠️  LIVE MODE — real trades will be placed. Continue? (yes/no): ")
        if confirm.lower() == "yes":
            run_watch(auto_execute=True)
        else:
            print("Aborted.")
