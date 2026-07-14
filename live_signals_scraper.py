import os
import time
import requests
import schedule
from datetime import datetime
from playwright.sync_api import sync_playwright

# --- CONFIGURATION ---
EMAIL = os.environ.get("REBEL_EMAIL")
PASSWORD = os.environ.get("REBEL_PASSWORD")
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK")

if not all([EMAIL, PASSWORD, DISCORD_WEBHOOK]):
    print("Missing environment variables. Please set REBEL_EMAIL, REBEL_PASSWORD, and DISCORD_WEBHOOK.")
    exit(1)

import json

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

def send_discord_signal(trader_name, trade_data):
    """Format and send the signal to Discord."""
    # Create a unique ID for this trade to avoid duplicates
    trade_id = f"{trader_name}_{trade_data.get('Order number', '')}_{trade_data.get('Status', '')}"
    if trade_id in SENT_SIGNALS:
        return
        
    embed = {
        "title": f"🚨 NEW SIGNAL — {trader_name}",
        "color": 0xffaa00 if trade_data.get('Status', '').upper() == 'PENDING' else 0x00ff88,
        "timestamp": datetime.utcnow().isoformat(),
        "fields": [
            {"name": "Symbol", "value": trade_data.get('Symbol', 'N/A'), "inline": True},
            {"name": "Direction", "value": trade_data.get('Direction', 'N/A'), "inline": True},
            {"name": "Status", "value": trade_data.get('Status', 'N/A'), "inline": True},
            {"name": "Open Price", "value": str(trade_data.get('Open price', 'N/A')), "inline": True},
            {"name": "Stop Loss", "value": str(trade_data.get('Stop loss', 'N/A')), "inline": True},
            {"name": "Take Profit", "value": str(trade_data.get('Take profit', 'N/A')), "inline": True}
        ],
        "footer": {"text": "Rebel Funding Top 3 Live Scraper"}
    }

    try:
        r = requests.post(DISCORD_WEBHOOK, json={"embeds": [embed]})
        if r.status_code in [200, 204]:
            print(f"[{datetime.now()}] Sent signal for {trader_name} - {trade_data.get('Symbol')}")
            SENT_SIGNALS.add(trade_id)
            save_sent_signals(SENT_SIGNALS)
        else:
            print(f"Failed to send to Discord: {r.status_code}")
    except Exception as e:
        print(f"Discord error: {e}")

def scrape_trader_trades(page, trader_url, trader_name):
    """Visit the trader's history page and extract OPEN/PENDING trades."""
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
    for row in rows[1:]:
        cells = row.locator("td").all()
        if len(cells) < len(headers):
            continue
            
        trade_data = {}
        for i, cell in enumerate(cells):
            if i < len(headers):
                trade_data[headers[i]] = cell.inner_text().strip()

        status = trade_data.get('Status', '').upper()
        if status in ['OPEN', 'PENDING']:
            send_discord_signal(trader_name, trade_data)

def scrape_leaderboard(page):
    """Navigate to leaderboard and find top 3 traders."""
    print("Navigating to Leaderboard...")
    page.goto("https://rf-zone.rebelsfunding.com/leaderboard")
    page.wait_for_load_state('networkidle')
    time.sleep(3)
    
    # Look for links to trader history
    # Typically links look like /leaderboard/history/1234
    links = page.locator("a[href*='/leaderboard/history/']").all()
    
    traders = []
    for link in links:
        href = link.get_attribute("href")
        name = link.inner_text().strip()
        if not name:
            name = f"Trader_{href.split('/')[-1]}"
            
        full_url = href if href.startswith("http") else f"https://rf-zone.rebelsfunding.com{href}"
        if full_url not in [t['url'] for t in traders]:
            traders.append({"name": name, "url": full_url})
            
        if len(traders) >= 3:
            break
            
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
            page.wait_for_load_state('networkidle')
            
            page.fill('input[type="email"]', EMAIL)
            page.fill('input[type="password"]', PASSWORD)
            
            # Click the submit button
            submit_btn = page.locator('button[type="submit"]')
            if submit_btn.count() > 0:
                submit_btn.first.click()
            else:
                page.locator('button:has-text("Log in"), button:has-text("Sign in")').first.click()
                
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

if __name__ == "__main__":
    # Run once when triggered by GitHub Actions
    run_scraper()
