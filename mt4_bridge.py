"""
MT4 Trade Bridge (Local File Version)

Communicates with the RebelGOAT_Bridge.mq4 EA running locally on MT4.
- Writes commands to commands.csv (OPEN/CLOSE)
- Reads account_state.csv to get live equity/balance for GOAT rules

Set MT4_FILES_DIR in your environment to the path of your MT4's MQL4/Files folder.
Example (Wine on Linux): ~/.wine/drive_c/Program Files (x86)/MetaTrader 4/MQL4/Files/
"""

import os
import json
import time
import uuid
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

# ── Config ───────────────────────────────────────────────────────────────
MT4_FILES_DIR = os.environ.get("MT4_FILES_DIR", ".")
COMMAND_FILE = os.path.join(MT4_FILES_DIR, "commands.csv")
STATE_FILE = os.path.join(MT4_FILES_DIR, "account_state.csv")
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

LOT_SIZE = 0.01
MAX_OPEN_POSITIONS = 3

# GOAT 2-Step rules
STARTING_BALANCE = 2500.0
DAILY_DRAWDOWN_PCT = 0.04
MAX_DRAWDOWN_PCT = 0.10
PHASE1_TARGET_PCT = 0.08
PHASE2_TARGET_PCT = 0.06
VALID_DAY_PCT = 0.005
MIN_VALID_DAYS = 3

DAILY_LIMIT = STARTING_BALANCE * DAILY_DRAWDOWN_PCT
EQUITY_FLOOR = STARTING_BALANCE * (1 - MAX_DRAWDOWN_PCT)
PHASE1_TARGET = STARTING_BALANCE * PHASE1_TARGET_PCT
PHASE2_TARGET = STARTING_BALANCE * PHASE2_TARGET_PCT

EXECUTOR_STATE_FILE = "executor_state.json"


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
    """Clean symbol name for MT4 (remove .V, slashes)."""
    return str(sym).strip().upper().replace(".V", "").replace("/", "")


# ── File Bridge Logic ────────────────────────────────────────────────────

def _read_mt4_state():
    """Parse account_state.csv written by the MT4 EA."""
    if not os.path.exists(STATE_FILE):
        return None

    state = {"balance": 0.0, "equity": 0.0, "positions": []}
    try:
        with open(STATE_FILE, 'r') as f:
            lines = [line.strip() for line in f.readlines() if line.strip()]
        
        if len(lines) >= 2 and lines[0].startswith("Balance"):
            parts = lines[1].split(',')
            if len(parts) >= 2:
                state["balance"] = float(parts[0])
                state["equity"] = float(parts[1])

        # Parse positions
        in_pos = False
        for line in lines:
            if line == "---POSITIONS---":
                in_pos = True
                continue
            if in_pos and not line.startswith("Ticket"):
                p = line.split(',')
                if len(p) >= 7:
                    state["positions"].append({
                        "ticket": p[0],
                        "symbol": p[1],
                        "type": "BUY" if p[2] == "0" else "SELL",
                        "lots": float(p[3]),
                        "profit": float(p[6])
                    })
        return state
    except Exception as e:
        print(f"[MT4 Bridge] Error reading state: {e}")
        return None


def _write_command(action, symbol, direction, lots):
    """Write command to commands.csv for MT4 to execute."""
    cmd_id = str(uuid.uuid4())[:8]
    mt4_sym = normalize_symbol(symbol)
    
    if DRY_RUN:
        print(f"[MT4 DRY RUN] Would write command: {cmd_id},{action},{mt4_sym},{direction},{lots}")
        return cmd_id
        
    try:
        with open(COMMAND_FILE, 'w') as f:
            f.write(f"{cmd_id},{action},{mt4_sym},{direction},{lots}")
        return cmd_id
    except Exception as e:
        print(f"[MT4 Bridge] Failed to write command: {e}")
        return None


def _wait_for_ack(cmd_id, timeout=10):
    """Wait for the EA to acknowledge the command by writing 'ACK'."""
    if DRY_RUN:
        return True
        
    start = time.time()
    while time.time() - start < timeout:
        try:
            with open(COMMAND_FILE, 'r') as f:
                content = f.read().strip().split(',')
                if len(content) >= 2 and content[0] == cmd_id and content[1] == "ACK":
                    return True
        except:
            pass
        time.sleep(0.5)
    return False


# ── Risk Checks ──────────────────────────────────────────────────────────

def check_risk(state, equity):
    warnings = []

    today = _today_str()
    closed_day_pl = state["daily_pl"].get(today, 0.0)
    floating_pl = sum(p.get("profit", 0.0) for p in state.get("mt4_positions_cache", []))
    
    current_day_pl = closed_day_pl + floating_pl

    if equity <= EQUITY_FLOOR:
        warnings.append(f"⚠️ ACCOUNT BREACHED: Equity ${equity:.2f} is below max drawdown floor ${EQUITY_FLOOR:.2f}")

    if current_day_pl <= -DAILY_LIMIT:
        warnings.append(f"⚠️ ACCOUNT BREACHED: Daily loss limit reached: ${current_day_pl:.2f}")

    warn_limit = STARTING_BALANCE * 0.03
    if current_day_pl <= -warn_limit and current_day_pl > -DAILY_LIMIT:
        warnings.append(f"⚠️ Daily P/L at ${current_day_pl:.2f} — approaching 4% limit (-${DAILY_LIMIT:.2f})")

    target = PHASE1_TARGET if state.get("phase", 1) == 1 else PHASE2_TARGET
    if state["total_pl"] >= target:
        days_count = len(state.get("valid_days", []))
        if days_count >= MIN_VALID_DAYS:
            warnings.append(f"🎉 Phase {state['phase']} TARGET REACHED! P/L: +${state['total_pl']:.2f} with {days_count} valid days")
        else:
            warnings.append(f"📊 Profit target reached (+${state['total_pl']:.2f}) but only {days_count}/{MIN_VALID_DAYS} valid trading days")

    return True, "OK", warnings


def update_daily_pl(state, pl_change):
    today = _today_str()
    state["daily_pl"].setdefault(today, 0.0)
    state["daily_pl"][today] += pl_change
    state["total_pl"] += pl_change

    day_threshold = STARTING_BALANCE * VALID_DAY_PCT
    if state["daily_pl"][today] >= day_threshold and today not in state.get("valid_days", []):
        state.setdefault("valid_days", []).append(today)


# ── Public Interface ─────────────────────────────────────────────────────

def is_configured():
    # If we can read the state file, or at least the directory exists, we're good
    return os.path.exists(MT4_FILES_DIR)


def execute_open(symbol, direction):
    if not is_configured():
        return False, "MT4 files directory not found"

    state = load_executor_state()
    mt4_state = _read_mt4_state()
    
    if not mt4_state:
        return False, "Could not read MT4 account state"

    equity = mt4_state["equity"]
    
    mt4_sym = normalize_symbol(symbol)
    for p in mt4_state["positions"]:
        if p["symbol"] == mt4_sym:
            return False, f"Position already open for {mt4_sym} (ignoring new alert to avoid hedging/pyramiding)"
    
    # Optional check for max open positions
    if len(mt4_state["positions"]) >= MAX_OPEN_POSITIONS:
        return False, f"Max open positions ({MAX_OPEN_POSITIONS}) reached"

    can_trade, reason, warnings = check_risk(state, equity)
    for w in warnings:
        print(f"[MT4] {w}")

    if not can_trade:
        save_executor_state(state)
        return False, reason

    cmd_id = _write_command("OPEN", symbol, direction, LOT_SIZE)
    if cmd_id:
        acked = _wait_for_ack(cmd_id)
        msg = f"Opened {direction} {LOT_SIZE} lot {normalize_symbol(symbol)}" + (" (Acked)" if acked else " (Timeout)")
        
        state["trades_placed"].append({
            "action": "OPEN",
            "symbol": normalize_symbol(symbol),
            "direction": direction.upper(),
            "time": datetime.now(IST).isoformat(),
        })
        save_executor_state(state)
        return True, msg
        
    return False, "Failed to write command"


def execute_close(symbol, direction):
    if not is_configured():
        return False, "MT4 files directory not found", 0.0

    mt4_state = _read_mt4_state()
    if not mt4_state:
        return False, "Could not read MT4 account state", 0.0

    # See if there's actually a position to close
    mt4_sym = normalize_symbol(symbol)
    pos_to_close = [p for p in mt4_state["positions"] if p["symbol"] == mt4_sym and p["type"] == direction.upper()]
    
    if not pos_to_close:
        return False, f"No matching {direction} position for {mt4_sym} found in MT4", 0.0

    cmd_id = _write_command("CLOSE", symbol, direction, LOT_SIZE)
    total_pl = sum(p["profit"] for p in pos_to_close)
    
    if cmd_id:
        acked = _wait_for_ack(cmd_id)
        msg = f"Closed {len(pos_to_close)} position(s), P/L: ${total_pl:.2f}" + (" (Acked)" if acked else " (Timeout)")
        
        state = load_executor_state()
        update_daily_pl(state, total_pl)
        state["trades_placed"].append({
            "action": "CLOSE",
            "symbol": mt4_sym,
            "direction": direction.upper(),
            "total_pl": total_pl,
            "time": datetime.now(IST).isoformat(),
        })
        save_executor_state(state)
        return True, msg, total_pl
        
    return False, "Failed to write command", 0.0


def get_dashboard_text():
    state = load_executor_state()
    mt4_state = _read_mt4_state()
    
    phase = state.get("phase", 1)
    total_pl_closed = state.get("total_pl", 0.0)
    today = _today_str()
    day_pl_closed = state["daily_pl"].get(today, 0.0)
    valid_days = len(state.get("valid_days", []))
    
    floating_pl = sum(p["profit"] for p in mt4_state["positions"]) if mt4_state else 0.0
    current_day_pl = day_pl_closed + floating_pl
    current_total_pl = total_pl_closed + floating_pl
    
    equity = mt4_state["equity"] if mt4_state else STARTING_BALANCE + current_total_pl
    open_pos = len(mt4_state["positions"]) if mt4_state else 0
    
    target = PHASE1_TARGET if phase == 1 else PHASE2_TARGET
    target_pct = (current_total_pl / target * 100) if target > 0 else 0
    
    # Calculate drawdown percentages based on the live floating P/L
    daily_pct = (abs(min(current_day_pl, 0)) / DAILY_LIMIT * 100) if DAILY_LIMIT > 0 else 0
    dd_pct = (abs(min(current_total_pl, 0)) / (STARTING_BALANCE * MAX_DRAWDOWN_PCT) * 100) if current_total_pl < 0 else 0

    def bar(pct):
        filled = int(min(pct, 100) / 10)
        return "█" * filled + "░" * (10 - filled)

    dashboard = f"""```text
┌─────────────────────────────────────┐
│  GOAT FUNDED — PHASE {phase} TRACKER     │
├─────────────────────────────────────┤
│  Starting Balance  :  ${STARTING_BALANCE:>10,.2f}   │
│  Current Equity    :  ${equity:>10,.2f}   │
│  Today's P/L       :  ${current_day_pl:>+10,.2f}   │
│  Overall P/L       :  ${current_total_pl:>+10,.2f}   │
├─────────────────────────────────────┤
│  Daily Limit       :  ${-DAILY_LIMIT:>+10,.2f}   │  {bar(daily_pct)} {daily_pct:.0f}%
│  Max Drawdown      :  ${-(STARTING_BALANCE * MAX_DRAWDOWN_PCT):>+10,.2f}   │  {bar(dd_pct)} {dd_pct:.0f}%
│  Phase {phase} Target    :  ${target:>+10,.2f}   │  {bar(max(target_pct,0))} {max(target_pct,0):.0f}%
├─────────────────────────────────────┤
│  Open Positions     :     {open_pos} / {MAX_OPEN_POSITIONS} max  │
│  Valid Trading Days :     {valid_days} / {MIN_VALID_DAYS} req  │
└─────────────────────────────────────┘
```"""
    return dashboard
