import os
import time
import json
import requests
import pandas as pd
from datetime import datetime, timezone, timedelta
from playwright.sync_api import sync_playwright

# --- CONFIGURATION ---
EMAIL = os.environ.get("REBEL_EMAIL")
PASSWORD = os.environ.get("REBEL_PASSWORD")
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK")

if not all([EMAIL, PASSWORD, DISCORD_WEBHOOK]):
    print("Missing environment variables. Please set REBEL_EMAIL, REBEL_PASSWORD, and DISCORD_WEBHOOK.")
    exit(1)

# We will track already sent signals so we don't spam the same open trade
STATE_FILE = "sent_signals.json"

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
SIGNALS_SENT_THIS_RUN = 0

def send_discord_signal(trader, trade_data):
    trader_name = trader['name']
    trader_winrate = trader.get('win_rate', 'N/A')
    
    profit_val = trader.get('total_return', 0)
    try:
        trader_profit = f"{float(profit_val):+.2f}"
    except (ValueError, TypeError):
        trader_profit = str(profit_val)
    
    global SIGNALS_SENT_THIS_RUN
    
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
    
    now_str = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime('%d %b %Y   %H:%M IST')
    
    ping_str = "<@1363959528194052118>\n" if status == 'OPEN' else ""
    
    msg_text = f"""{ping_str}```text
AZALYST PROPFIRM SCANNER  —  NEW SIGNALS (TRADER: {trader_name.upper()})
{now_str}
--------------------------------------------------------------
{symbol}  {direction}
  >> VERDICT     : [SIGNAL]
  Location       : {status}
  Entry          : {entry}
  Stop Loss      : {sl}
  Take Profit    : {tp}

[Trader Stats]
Win Rate         : {trader_winrate}
Total Profit     : {trader_profit}
```"""

    try:
        r = requests.post(DISCORD_WEBHOOK, json={"content": msg_text})
        if r.status_code in [200, 204]:
            print(f"[{datetime.now()}] Sent signal for {trader_name} - {trade_data.get('Symbol')}")
            SIGNALS_SENT_THIS_RUN += 1
            SENT_SIGNALS.add(trade_id)
            save_sent_signals(SENT_SIGNALS)
        else:
            print(f"Failed to send to Discord: {r.status_code}")
    except Exception as e:
        print(f"Discord error: {e}")


def run_scraper():
    print(f"[{datetime.now()}] Starting live signal scraper...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        
        try:
            # Login
            print("Logging in to Rebel Funding...")
            page.goto("https://rf-zone.rebelsfunding.com/login")
            
            page.get_by_label("E-mail").wait_for(state="visible", timeout=30000)
            page.get_by_label("E-mail").fill(EMAIL)
            page.get_by_label("Password").fill(PASSWORD)
            page.get_by_role("button", name="Sign in", exact=True).click()
                
            page.wait_for_load_state('networkidle')
            time.sleep(5) # Wait for login redirect
            
            # Scrape top 3
            print("Navigating to Leaderboard...")
            page.goto("https://rf-zone.rebelsfunding.com/leaderboard")
            page.wait_for_load_state('networkidle')
            time.sleep(5)
            
            rows = page.locator("tbody.p-datatable-tbody > tr").all()
            if not rows:
                print("No rows found on the leaderboard!")
                return
                
            for i in range(min(10, len(rows))):
                try:
                    # Re-query rows in case DOM changed
                    rows = page.locator("tbody.p-datatable-tbody > tr").all()
                    if i >= len(rows): break
                    
                    row = rows[i]
                    cells = row.locator("td").all()
                    if len(cells) < 4: continue
                    
                    name = cells[1].inner_text().strip()
                    profit_text = cells[5].inner_text().strip()  # Closed Profit
                    
                    print(f"Clicking trader row {i}: {name}...")
                    row.click()
                    
                    # Wait for the modal and its tables to appear
                    try:
                        page.wait_for_function("() => document.querySelectorAll('table').length > 1", timeout=15000)
                    except:
                        print(f"Timeout waiting for modal tables for {name}")
                        page.keyboard.press("Escape")
                        time.sleep(2)
                        continue
                        
                    time.sleep(2) # Extra time for data to populate
                    
                    tables = page.locator("table").all()
                    
                    all_trades = []
                    
                    # Find the trades table inside the modal
                    for t in tables:
                        headers = [th.inner_text().strip() for th in t.locator("th").all()]
                        if "Order number" in headers:
                            trade_rows = t.locator("tbody > tr").all()
                            for tr in trade_rows:
                                t_cells = tr.locator("td").all()
                                if len(t_cells) == len(headers):
                                    trade_data = {}
                                    for col_idx, col_name in enumerate(headers):
                                        clean_col = col_name.replace('\n', '').replace('i', '').strip() if 'Volume' in col_name else col_name
                                        trade_data[clean_col] = t_cells[col_idx].inner_text().strip()
                                    all_trades.append(trade_data)
                                    
                    # Calculate win rate based on trades
                    total_closed = 0
                    wins = 0
                    for td in all_trades:
                        pl_val = td.get('P/L')
                        if pl_val and str(pl_val).strip() and str(pl_val).strip() != '-':
                            try:
                                val_str = str(pl_val).replace(',', '').replace('$', '').replace(' ', '').strip()
                                val = float(val_str)
                                total_closed += 1
                                if val > 0:
                                    wins += 1
                            except ValueError:
                                pass
                                
                    win_rate_str = "N/A"
                    if total_closed > 0:
                        win_rate_str = f"{(wins / total_closed) * 100:.1f}%"
                        
                    profit_val = profit_text.replace('$', '').replace(',', '').strip()
                        
                    trader_info = {
                        'name': name,
                        'win_rate': win_rate_str,
                        'total_return': profit_val
                    }
                    
                    # Process signals
                    for td in all_trades:
                        status = td.get('Status', '').upper()
                        if status in ['OPEN', 'PENDING']:
                            send_discord_signal(trader_info, td)
                            
                    # Close modal
                    page.keyboard.press("Escape")
                    time.sleep(2)
                    
                except Exception as e:
                    print(f"Error extracting data for trader {i}: {e}")
                    page.keyboard.press("Escape")
                    time.sleep(2)
                
        except Exception as e:
            print(f"An error occurred during scraping: {e}")
        finally:
            browser.close()
            
    print(f"[{datetime.now()}] Scraping cycle complete.")
    
    if SIGNALS_SENT_THIS_RUN == 0:
        now_str = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime('%d %b %Y   %H:%M IST')
        heartbeat_msg = f"REBEL FUNDING: Checked Leaderboard at {now_str}. No new open signals found."
        try:
            requests.post(DISCORD_WEBHOOK, json={"content": heartbeat_msg})
        except:
            pass

if __name__ == "__main__":
    run_scraper()
    
    if not os.path.exists(STATE_FILE):
        save_sent_signals(SENT_SIGNALS)
