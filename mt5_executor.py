"""
MT5 Trade Executor — places and closes trades on a MetaTrader 5 account
via the MetaAPI cloud bridge (https://metaapi.cloud).

Reads credentials from environment variables (GitHub Secrets):
    METAAPI_TOKEN      — your MetaAPI auth token
    METAAPI_ACCOUNT_ID — the account ID assigned by MetaAPI

GOAT 2-Step rules are enforced before every trade:
    - Daily drawdown: 4% of starting balance
    - Max overall drawdown: 10% (static floor)
    - Phase 1 profit target: 8%
    - Phase 2 profit target: 6%
    - Min 3 valid trading days (day P/L >= 0.5% of start)

Set DRY_RUN=true in env to log actions without placing real orders.
"""

import os
import json
import asyncio
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

# ── Config ───────────────────────────────────────────────────────────────
METAAPI_TOKEN = os.environ.get("METAAPI_TOKEN", "")
METAAPI_ACCOUNT_ID = os.environ.get("METAAPI_ACCOUNT_ID", "")
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

LOT_SIZE = 0.01
MAX_OPEN_POSITIONS = 3

# GOAT 2-Step rules
STARTING_BALANCE = 2500.0
DAILY_DRAWDOWN_PCT = 0.04       # 4%
MAX_DRAWDOWN_PCT = 0.10         # 10% static
PHASE1_TARGET_PCT = 0.08        # 8%
PHASE2_TARGET_PCT = 0.06        # 6%
VALID_DAY_PCT = 0.005           # 0.5%
MIN_VALID_DAYS = 3

DAILY_LIMIT = STARTING_BALANCE * DAILY_DRAWDOWN_PCT       # $100
EQUITY_FLOOR = STARTING_BALANCE * (1 - MAX_DRAWDOWN_PCT)  # $2,250
PHASE1_TARGET = STARTING_BALANCE * PHASE1_TARGET_PCT       # $200
PHASE2_TARGET = STARTING_BALANCE * PHASE2_TARGET_PCT       # $150

# State file for tracking daily P/L and valid days
EXECUTOR_STATE_FILE = "executor_state.json"

# Symbol mapping: what the leaderboard shows → what MT5 expects
SYMBOL_MAP = {
    "EUR/USD": "EURUSD", "EURUSD": "EURUSD",
    "GBP/USD": "GBPUSD", "GBPUSD": "GBPUSD",
    "USD/JPY": "USDJPY", "USDJPY": "USDJPY",
    "AUD/USD": "AUDUSD", "AUDUSD": "AUDUSD",
    "NZD/USD": "NZDUSD", "NZDUSD": "NZDUSD",
    "USD/CAD": "USDCAD", "USDCAD": "USDCAD",
    "USD/CHF": "USDCHF", "USDCHF": "USDCHF",
    "GBP/JPY": "GBPJPY", "GBPJPY": "GBPJPY",
    "EUR/JPY": "EURJPY", "EURJPY": "EURJPY",
    "AUD/JPY": "AUDJPY", "AUDJPY": "AUDJPY",
    "CAD/JPY": "CADJPY", "CADJPY": "CADJPY",
    "NZD/JPY": "NZDJPY", "NZDJPY": "NZDJPY",
    "EUR/GBP": "EURGBP", "EURGBP": "EURGBP",
    "EUR/CHF": "EURCHF", "EURCHF": "EURCHF",
    "AUD/CAD": "AUDCAD", "AUDCAD": "AUDCAD",
    "NZD/CAD": "NZDCAD", "NZDCAD": "NZDCAD",
    "AUD/CHF": "AUDCHF", "AUDCHF": "AUDCHF",
    "NZD/CHF": "NZDCHF", "NZDCHF": "NZDCHF",
    "AUD/NZD": "AUDNZD", "AUDNZD": "AUDNZD",
    "EUR/NZD": "EURNZD", "EURNZD": "EURNZD",
    "EUR/CAD": "EURCAD", "EURCAD": "EURCAD",
    "EUR/AUD": "EURAUD", "EURAUD": "EURAUD",
    "GBP/CHF": "GBPCHF", "GBPCHF": "GBPCHF",
    "GBP/AUD": "GBPAUD", "GBPAUD": "GBPAUD",
    "GBP/CAD": "GBPCAD", "GBPCAD": "GBPCAD",
    "GBP/NZD": "GBPNZD", "GBPNZD": "GBPNZD",
    "CAD/CHF": "CADCHF", "CADCHF": "CADCHF",
    "CHF/JPY": "CHFJPY", "CHFJPY": "CHFJPY",
}


def _today_str():
    return datetime.now(IST).strftime('%Y-%m-%d')


def load_executor_state():
    if os.path.exists(EXECUTOR_STATE_FILE):
        try:
            with open(EXECUTOR_STATE_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "starting_balance": STARTING_BALANCE,
        "phase": 1,
        "daily_pl": {},
        "total_pl": 0.0,
        "valid_days": [],
        "trades_placed": [],
        "halted": False,
        "halt_reason": "",
    }


def save_executor_state(state):
    with open(EXECUTOR_STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


def normalize_symbol(sym):
    """Convert leaderboard symbol to MT5 symbol name."""
    clean = str(sym).strip().upper().replace(".V", "")
    return SYMBOL_MAP.get(clean, clean.replace("/", ""))


# ── Risk Checks ──────────────────────────────────────────────────────────

def check_risk(state, equity):
    """
    Returns (can_trade: bool, reason: str, warnings: list).
    """
    warnings = []

    if state.get("halted"):
        return False, f"Trading halted: {state.get('halt_reason', 'unknown')}", warnings

    # Max drawdown check (static floor)
    if equity <= EQUITY_FLOOR:
        state["halted"] = True
        state["halt_reason"] = f"Equity ${equity:.2f} breached max drawdown floor ${EQUITY_FLOOR:.2f}"
        return False, state["halt_reason"], warnings

    # Daily P/L check
    today = _today_str()
    day_pl = state["daily_pl"].get(today, 0.0)
    if day_pl <= -DAILY_LIMIT:
        return False, f"Daily loss limit reached: ${day_pl:.2f} (limit: -${DAILY_LIMIT:.2f})", warnings

    # Early warning at 3%
    warn_limit = STARTING_BALANCE * 0.03
    if day_pl <= -warn_limit:
        warnings.append(f"⚠️ Daily P/L at ${day_pl:.2f} — approaching 4% limit (-${DAILY_LIMIT:.2f})")

    # Phase target check
    target = PHASE1_TARGET if state.get("phase", 1) == 1 else PHASE2_TARGET
    if state["total_pl"] >= target:
        days_count = len(state.get("valid_days", []))
        if days_count >= MIN_VALID_DAYS:
            state["halted"] = True
            state["halt_reason"] = f"Phase {state['phase']} TARGET REACHED! P/L: +${state['total_pl']:.2f} with {days_count} valid days"
            warnings.append(f"🎉 {state['halt_reason']}")
            return False, state["halt_reason"], warnings
        else:
            warnings.append(f"📊 Profit target reached (+${state['total_pl']:.2f}) but only {days_count}/{MIN_VALID_DAYS} valid trading days")

    return True, "OK", warnings


def update_daily_pl(state, pl_change):
    """Record a P/L change for today."""
    today = _today_str()
    state["daily_pl"].setdefault(today, 0.0)
    state["daily_pl"][today] += pl_change
    state["total_pl"] += pl_change

    # Check if today becomes a valid trading day
    day_threshold = STARTING_BALANCE * VALID_DAY_PCT  # $12.50
    if state["daily_pl"][today] >= day_threshold and today not in state.get("valid_days", []):
        state.setdefault("valid_days", []).append(today)


# ── MetaAPI Connection ───────────────────────────────────────────────────

async def _get_connection():
    """Create and return a MetaAPI RPC connection."""
    from metaapi_cloud_sdk import MetaApi

    api = MetaApi(METAAPI_TOKEN)
    account = await api.metatrader_account_api.get_account(METAAPI_ACCOUNT_ID)

    if account.state != 'DEPLOYED':
        print(f"[MT5] Account state: {account.state}, deploying...")
        await account.deploy()

    await account.wait_connected()
    connection = account.get_rpc_connection()
    await connection.connect()
    await connection.wait_synchronized()
    return connection, api


async def _get_account_info(connection):
    """Get account balance, equity, margin info."""
    info = await connection.get_account_information()
    return {
        "balance": info.get("balance", 0),
        "equity": info.get("equity", 0),
        "margin": info.get("margin", 0),
        "free_margin": info.get("freeMargin", 0),
    }


async def _get_positions(connection):
    """Get all currently open positions."""
    return await connection.get_positions()


async def _open_trade(connection, symbol, direction, lot=LOT_SIZE):
    """Place a market order."""
    mt5_symbol = normalize_symbol(symbol)
    print(f"[MT5] Opening {direction} {lot} lot {mt5_symbol}...")

    if DRY_RUN:
        print(f"[MT5] DRY RUN — would place {direction} {lot} {mt5_symbol}")
        return {"orderId": "DRY_RUN", "symbol": mt5_symbol, "direction": direction}

    if direction.upper() == "BUY":
        result = await connection.create_market_buy_order(mt5_symbol, lot)
    else:
        result = await connection.create_market_sell_order(mt5_symbol, lot)

    print(f"[MT5] Order result: {result}")
    return result


async def _close_trade(connection, symbol, direction):
    """Close all positions matching the symbol and direction."""
    mt5_symbol = normalize_symbol(symbol)
    positions = await _get_positions(connection)

    closed = []
    for pos in positions:
        pos_symbol = pos.get("symbol", "")
        pos_type = pos.get("type", "")

        if pos_symbol != mt5_symbol:
            continue

        expected_type = "POSITION_TYPE_BUY" if direction.upper() == "BUY" else "POSITION_TYPE_SELL"
        if pos_type != expected_type:
            continue

        pos_id = pos.get("id")
        volume = pos.get("volume", LOT_SIZE)
        profit = pos.get("profit", 0)

        print(f"[MT5] Closing position {pos_id}: {pos_symbol} {pos_type} {volume} lot (P/L: {profit})")

        if DRY_RUN:
            print(f"[MT5] DRY RUN — would close position {pos_id}")
            closed.append({"id": pos_id, "profit": profit})
            continue

        result = await connection.close_position(pos_id)
        print(f"[MT5] Close result: {result}")
        closed.append({"id": pos_id, "profit": profit, "result": result})

    return closed


# ── Public Interface (sync wrappers) ─────────────────────────────────────

def is_configured():
    """Check if MetaAPI credentials are set."""
    return bool(METAAPI_TOKEN) and bool(METAAPI_ACCOUNT_ID)


def execute_open(symbol, direction):
    """
    Place a trade if risk checks pass.
    Returns (success: bool, message: str).
    """
    if not is_configured():
        print("[MT5] MetaAPI not configured — skipping trade execution")
        return False, "MetaAPI not configured"

    state = load_executor_state()

    async def _run():
        connection, api = await _get_connection()
        try:
            info = await _get_account_info(connection)
            equity = info["equity"]

            positions = await _get_positions(connection)
            if len(positions) >= MAX_OPEN_POSITIONS:
                return False, f"Max open positions ({MAX_OPEN_POSITIONS}) reached"

            can_trade, reason, warnings = check_risk(state, equity)
            for w in warnings:
                print(f"[MT5] {w}")

            if not can_trade:
                save_executor_state(state)
                return False, reason

            result = await _open_trade(connection, symbol, direction)

            state["trades_placed"].append({
                "action": "OPEN",
                "symbol": normalize_symbol(symbol),
                "direction": direction.upper(),
                "lot": LOT_SIZE,
                "time": datetime.now(IST).isoformat(),
                "result": str(result) if not DRY_RUN else "DRY_RUN",
            })
            save_executor_state(state)

            return True, f"Opened {direction} {LOT_SIZE} lot {normalize_symbol(symbol)}"
        finally:
            pass

    return asyncio.run(_run())


def execute_close(symbol, direction):
    """
    Close all matching positions.
    Returns (success: bool, message: str, total_pl: float).
    """
    if not is_configured():
        print("[MT5] MetaAPI not configured — skipping trade close")
        return False, "MetaAPI not configured", 0.0

    state = load_executor_state()

    async def _run():
        connection, api = await _get_connection()
        try:
            closed = await _close_trade(connection, symbol, direction)

            total_pl = sum(c.get("profit", 0) for c in closed)

            if closed:
                update_daily_pl(state, total_pl)
                state["trades_placed"].append({
                    "action": "CLOSE",
                    "symbol": normalize_symbol(symbol),
                    "direction": direction.upper(),
                    "closed_count": len(closed),
                    "total_pl": total_pl,
                    "time": datetime.now(IST).isoformat(),
                })
                save_executor_state(state)

            return bool(closed), f"Closed {len(closed)} position(s), P/L: ${total_pl:.2f}", total_pl
        finally:
            pass

    return asyncio.run(_run())


def get_dashboard_text():
    """Generate a Discord-formatted dashboard string."""
    state = load_executor_state()
    phase = state.get("phase", 1)
    total_pl = state.get("total_pl", 0.0)
    today = _today_str()
    day_pl = state["daily_pl"].get(today, 0.0)
    valid_days = len(state.get("valid_days", []))

    equity = STARTING_BALANCE + total_pl
    target = PHASE1_TARGET if phase == 1 else PHASE2_TARGET
    target_pct = (total_pl / target * 100) if target > 0 else 0
    daily_pct = (abs(day_pl) / DAILY_LIMIT * 100) if DAILY_LIMIT > 0 else 0
    dd_pct = (abs(min(total_pl, 0)) / (STARTING_BALANCE * MAX_DRAWDOWN_PCT) * 100) if total_pl < 0 else 0

    def bar(pct):
        filled = int(min(pct, 100) / 10)
        return "█" * filled + "░" * (10 - filled)

    halted_str = ""
    if state.get("halted"):
        halted_str = f"\n│  ⛔ HALTED                           │"

    dashboard = f"""```text
┌─────────────────────────────────────┐
│  GOAT FUNDED — PHASE {phase} TRACKER     │
├─────────────────────────────────────┤
│  Starting Balance  :  ${STARTING_BALANCE:>10,.2f}   │
│  Current Equity    :  ${equity:>10,.2f}   │
│  Today's P/L       :  ${day_pl:>+10,.2f}   │
│  Overall P/L       :  ${total_pl:>+10,.2f}   │
├─────────────────────────────────────┤
│  Daily Limit       :  ${-DAILY_LIMIT:>+10,.2f}   │  {bar(daily_pct)} {daily_pct:.0f}%
│  Max Drawdown      :  ${-(STARTING_BALANCE * MAX_DRAWDOWN_PCT):>+10,.2f}   │  {bar(dd_pct)} {dd_pct:.0f}%
│  Phase {phase} Target    :  ${target:>+10,.2f}   │  {bar(max(target_pct,0))} {max(target_pct,0):.0f}%
├─────────────────────────────────────┤
│  Valid Trading Days :     {valid_days} / {MIN_VALID_DAYS} req  │{halted_str}
└─────────────────────────────────────┘
```"""
    return dashboard
