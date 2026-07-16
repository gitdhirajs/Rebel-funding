import os
import glob
import json
import time
import requests
import pandas as pd
from datetime import datetime

# --- CONFIGURATION ---
DATA_FOLDER = "trader_trades"
STATE_FILE = "sent_signals.json"
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK")

if not DISCORD_WEBHOOK:
    print("Missing DISCORD_WEBHOOK environment variable.")
    exit(1)

# Load sent signals to avoid spam
def load_sent_signals():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                return set(json.load(f))
        except:
            return set()
    return set()

def save_sent_signals(signals):
    with open(STATE_FILE, 'w') as f:
        json.dump(list(signals), f)

SENT_SIGNALS = load_sent_signals()

# Re-using the parse logic from competitor monitor
def parse_trader_file(filepath):
    try:
        df = pd.read_excel(filepath, engine='openpyxl')
    except Exception as e:
        return None

    status_col = 'Status'
    profit_col = 'P/L'

    if status_col not in df.columns:
        return None

    total_return = 0.0
    if profit_col in df.columns:
        total_return = pd.to_numeric(df[profit_col], errors='coerce').sum()

    name = os.path.basename(filepath).replace('.xlsx', '').replace('.xls', '')
    if name.startswith('Competition-'):
        parts = name.split('_', 2)
        if len(parts) >= 3:
            name = parts[2]

    return {
        'name': name,
        'filepath': filepath,
        'total_return': total_return,
        'df': df
    }

def get_top_competitors(n=3):
    files = glob.glob(os.path.join(DATA_FOLDER, "*.xlsx"))
    traders = []
    for f in files:
        data = parse_trader_file(f)
        if data is not None:
            traders.append(data)
    
    traders.sort(key=lambda x: x['total_return'], reverse=True)
    return traders[:n]

def send_discord_signal(trader_name, trade_data):
    # Unique ID based on trader and order number
    trade_id = f"{trader_name}_{trade_data.get('Order number', '')}_{trade_data.get('Status', '')}"
    if trade_id in SENT_SIGNALS:
        return
        
    symbol = str(trade_data.get('Symbol', 'N/A')).ljust(15)
    direction = str(trade_data.get('Direction', 'N/A')).upper()
    status = str(trade_data.get('Status', 'N/A')).upper()
    
    def fmt_price(val):
        if pd.isna(val) or str(val).strip() == '': return 'N/A'
        return str(val)

    entry = fmt_price(trade_data.get('Open price', 'N/A')).rjust(12)
    sl = fmt_price(trade_data.get('Stop loss', 'N/A')).rjust(12)
    tp = fmt_price(trade_data.get('Take profit', 'N/A')).rjust(12)
    
    now_str = datetime.utcnow().strftime('%d %b %Y   %H:%M UTC')
    
    msg_text = f"""```text
AZALYST PROPFIRM SCANNER  —  NEW SIGNALS (TRADER: {trader_name.upper()})
{now_str}
--------------------------------------------------------------
{symbol}  {direction}
  >> VERDICT     : [SIGNAL]
  Location       : {status}
  Entry          : {entry}
  Stop Loss      : {sl}
  Take Profit    : {tp}
```"""

    try:
        r = requests.post(DISCORD_WEBHOOK, json={"content": msg_text})
        if r.status_code in [200, 204]:
            print(f"[{datetime.now()}] Sent signal for {trader_name} - {symbol}")
            SENT_SIGNALS.add(trade_id)
            save_sent_signals(SENT_SIGNALS)
        else:
            print(f"Failed to send to Discord: {r.status_code}")
    except Exception as e:
        print(f"Discord error: {e}")

def run_signals_scanner():
    print(f"[{datetime.now()}] Scanning files for live signals...")
    top3 = get_top_competitors(3)
    
    if not top3:
        print("No trader files found in trader_trades folder.")
        return

    for trader in top3:
        df = trader['df']
        name = trader['name']
        status_col = 'Status'
        
        # Filter for OPEN or PENDING trades
        open_trades = df[df[status_col].astype(str).str.upper().isin(['OPEN', 'PENDING'])]
        
        for _, row in open_trades.iterrows():
            trade_data = row.to_dict()
            send_discord_signal(name, trade_data)
            
    print(f"[{datetime.now()}] Signal scan complete.")
    
    # Ensure state file exists so cache action doesn't warn on first run
    if not os.path.exists(STATE_FILE):
        save_sent_signals(SENT_SIGNALS)

if __name__ == "__main__":
    run_signals_scanner()
