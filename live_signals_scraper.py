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
    trader_winrate = trader['win_rate']
    trader_profit = f"{trader['total_return']:+.2f}"
    
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


def scrape_trader_trades(page, trader_url, trader_name):
    """Visit the trader's history page and extract OPEN/PENDING trades and stats."""
    print(f"Visiting {trader_name}'s profile: {trader_url}")
    page.goto(trader_url)
    page.wait_for_load_state('networkidle')
    time.sleep(3) # Extra wait for dynamic tables

    # Find all table rows
    rows = page.locator("table tr").all()
    if not rows:
        print(f"No tables found for {trader_name}")
        return

    # Extract headers
    headers = []
    for th in rows[0].locator("th, td").all():
        headers.append(th.inner_text().strip())
    
    # If standard headers aren't found, use defaults based on known Excel format
    if not headers or 'Status' not in headers:
        headers = ['Symbol', 'Order number', 'Status', 'Direction', 'P/L', 'P/L % %', 'Open price', 'Close price', 'Stop loss', 'Take profit', 'Volume', 'Opened', 'Closed', 'Duration', 'Commission', 'Swap']

    # Parse rows
    all_trades = []
    for row in rows[1:]:
        cells = row.locator("td").all()
        if len(cells) < len(headers):
            continue
            
        trade_data = {}
        for i, cell in enumerate(cells):
            if i < len(headers):
                trade_data[headers[i]] = cell.inner_text().strip()
        all_trades.append(trade_data)

    # Calculate Win Rate and Total Profit from the visible trades
    total_profit = 0.0
    wins = 0
    total_closed = 0
    
    for td in all_trades:
        pl_val = td.get('P/L')
        if pl_val and str(pl_val).strip() and str(pl_val).strip() != '-':
            try:
                val_str = str(pl_val).replace(',', '').replace('$', '').replace(' ', '').strip()
                val = float(val_str)
                total_profit += val
                total_closed += 1
                if val > 0:
                    wins += 1
            except ValueError:
                pass
                
    win_rate_str = "N/A"
    if total_closed > 0:
        win_rate = (wins / total_closed) * 100
        win_rate_str = f"{win_rate:.1f}%"
        
    trader_info = {
        'name': trader_name,
        'win_rate': win_rate_str,
        'total_return': total_profit
    }

    # Send signals for OPEN or PENDING trades
    for td in all_trades:
        status = td.get('Status', '').upper()
        if status in ['OPEN', 'PENDING']:
            send_discord_signal(trader_info, td)


def scrape_leaderboard(page):
    """Navigate to leaderboard and find top 3 traders."""
    print("Navigating to Leaderboard...")
    page.goto("https://rf-zone.rebelsfunding.com/leaderboard")
    page.wait_for_load_state('networkidle')
    time.sleep(3)
    
    try:
        page.wait_for_selector("table", timeout=20000)
    except:
        pass
    
    time.sleep(5)
    
    links = page.locator("a").all()
    traders = []
    for link in links:
        href = link.get_attribute("href")
        if not href or "/leaderboard/history" not in href:
            continue
            
        name = link.inner_text().strip()
        if not name:
            name = f"Trader_{href.split('/')[-1]}"
            
        full_url = href if href.startswith("http") else f"https://rf-zone.rebelsfunding.com{href}"
        if full_url not in [t['url'] for t in traders]:
            traders.append({"name": name, "url": full_url})
            
        if len(traders) >= 3:
            break
            
    if not traders:
        traders = [
            {"name": "Emanuel C", "url": "https://rf-zone.rebelsfunding.com/leaderboard/history/1330"},
            {"name": "Mathews T", "url": "https://rf-zone.rebelsfunding.com/leaderboard/history/1331"},
            {"name": "Kevin B", "url": "https://rf-zone.rebelsfunding.com/leaderboard/history/1332"}
        ]
            
    print(f"Found top {len(traders)} traders: {[t['name'] for t in traders]}")
    return traders

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
            top_traders = scrape_leaderboard(page)
            
            # Scrape trades for each
            for trader in top_traders:
                scrape_trader_trades(page, trader['url'], trader['name'])
                
        except Exception as e:
            print(f"An error occurred during scraping: {e}")
        finally:
            browser.close()
            
    print(f"[{datetime.now()}] Scraping cycle complete.")
    
    if SIGNALS_SENT_THIS_RUN == 0:
        now_str = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime('%d %b %Y   %H:%M IST')
        heartbeat_msg = f"✅ AZALYST PROPFIRM SCANNER: Checked Leaderboard at {now_str}. No new open signals found."
        try:
            requests.post(DISCORD_WEBHOOK, json={"content": heartbeat_msg})
        except:
            pass

if __name__ == "__main__":
    run_scraper()
    
    if not os.path.exists(STATE_FILE):
        save_sent_signals(SENT_SIGNALS)
