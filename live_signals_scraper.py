import os
import time
import json
import requests
import pandas as pd
from datetime import datetime, timezone, timedelta
from playwright.sync_api import sync_playwright
import paper_trading as paper
import mt5_executor as mt5

# --- CONFIGURATION ---
EMAIL = os.environ.get("REBEL_EMAIL")
PASSWORD = os.environ.get("REBEL_PASSWORD")
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK")

if not all([EMAIL, PASSWORD, DISCORD_WEBHOOK]):
    print("Missing environment variables. Please set REBEL_EMAIL, REBEL_PASSWORD, and DISCORD_WEBHOOK.")
    exit(1)

# Check the top N traders by %Gain first. Only if NONE of them currently have
# an open/pending trade in an allowed symbol (Safest/Moderate tier - see
# paper.ALLOWED_SYMBOLS) do we fall back to the next M traders. This keeps
# runtime bounded (at most PRIMARY+FALLBACK traders, not the whole leaderboard)
# while still finding a signal when the very top of the leaderboard has
# nothing open in a symbol we actually trade.
PRIMARY_POOL_SIZE = 50
FALLBACK_POOL_SIZE = 0

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
NOTIFIED_THIS_RUN_GROUPS = set()

# We also track which open/pending trades we've already notified about, per
# trader, so we can tell when one of them disappears (i.e. gets closed).
POSITIONS_FILE = "open_positions.json"

def load_open_positions():
    if os.path.exists(POSITIONS_FILE):
        try:
            with open(POSITIONS_FILE, 'r') as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_open_positions(positions):
    with open(POSITIONS_FILE, 'w') as f:
        json.dump(positions, f)

OPEN_POSITIONS = load_open_positions()

PAPER_LEDGER = paper.load_ledger()

def post_discord(msg_text):
    try:
        requests.post(DISCORD_WEBHOOK, json={"content": msg_text})
    except Exception as e:
        print(f"Discord error: {e}")

def extract_visible_trade_tables(page):
    """Read every currently-visible table whose headers include 'Order number'."""
    trades = []
    for t in page.locator("table:visible").all():
        headers = [th.inner_text().strip() for th in t.locator("th").all()]
        if "Order number" not in headers:
            continue
        for tr in t.locator("tbody > tr").all():
            t_cells = tr.locator("td").all()
            if len(t_cells) != len(headers):
                continue
            trade_data = {}
            for col_idx, col_name in enumerate(headers):
                clean_col = col_name.replace('\n', '').replace('i', '').strip() if 'Volume' in col_name else col_name
                trade_data[clean_col] = t_cells[col_idx].inner_text().strip()
            trades.append(trade_data)
    return trades

def group_and_send_signals(signals_list):
    global SIGNALS_SENT_THIS_RUN
    
    groups = {}
    for trader, td in signals_list:
        name = trader['name']
        trade_id = f"{name}_{td.get('Order number', '')}_{td.get('Status', '')}"
        if trade_id in SENT_SIGNALS:
            continue
            
        sym = str(td.get('Symbol', 'N/A')).strip()
        dir = str(td.get('Direction', 'N/A')).upper()
        stat = str(td.get('Status', 'N/A')).upper()
        key = (sym, dir, stat)
        
        if key not in groups:
            groups[key] = []
        groups[key].append((trader, td))

    if not groups:
        return

    def fmt_price(val):
        if pd.isna(val) or str(val).strip() == '': return 'N/A'
        return str(val)

    for (sym, dir, stat), items in groups.items():
        unique_traders = {}
        for trader, td in items:
            name = trader['name']
            if name not in unique_traders:
                unique_traders[name] = (trader, td)
            trade_id = f"{name}_{td.get('Order number', '')}_{td.get('Status', '')}"
            SENT_SIGNALS.add(trade_id)
            
        save_sent_signals(SENT_SIGNALS)
        
        now_str = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime('%d %b %Y   %H:%M IST')
        ping_str = "<@1363959528194052118>\n" if (stat == 'OPEN' and SIGNALS_SENT_THIS_RUN == 0) else ""
        
        msg = f"{ping_str}```text\nAZALYST PROPFIRM SCANNER  —  {sym} {dir} [{stat}]\n{now_str}\n--------------------------------------------------------------\nTRADERS IN THIS POSITION:\n\n"
        
        idx = 1
        for name, (trader, td) in unique_traders.items():
            rank = trader.get('rank', 'N/A')
            wr = trader.get('win_rate', 'N/A')
            entry = fmt_price(td.get('Open price', 'N/A'))
            sl = fmt_price(td.get('Stop loss', 'N/A'))
            tp = fmt_price(td.get('Take profit', 'N/A'))
            msg += f"{idx}. {name.upper()} (Rank #{rank}, WR: {wr})\n"
            msg += f"   Entry: {entry}  |  SL: {sl}  |  TP: {tp}\n\n"
            idx += 1
            
        msg += "--------------------------------------------------------------\n```"
        
        try:
            r = requests.post(DISCORD_WEBHOOK, json={"content": msg})
            if r.status_code in [200, 204]:
                print(f"[{datetime.now()}] Sent grouped signal for {sym} {dir}")
                SIGNALS_SENT_THIS_RUN += 1
            else:
                print(f"Failed to send to Discord: {r.status_code}")
        except Exception as e:
            print(f"Discord error: {e}")

        # Execute trade on MT5 (only for OPEN signals, not PENDING)
        if stat == 'OPEN' and mt5.is_configured():
            success, mt5_msg = mt5.execute_open(sym, dir)
            print(f"[MT5] {mt5_msg}")
            if success:
                post_discord(f"🤖 **AUTO-TRADE**: {mt5_msg}")

def send_discord_close(trader_name, saved_position, closed_match, order_num='N/A'):
    """Alert that a previously-notified open/pending trade is no longer open."""
    global SIGNALS_SENT_THIS_RUN

    symbol = str(saved_position.get('symbol', 'N/A')).ljust(15)
    direction = str(saved_position.get('direction', 'N/A')).upper()
    now_str = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime('%d %b %Y   %H:%M IST')

    if closed_match:
        result = str(closed_match.get('Status', 'N/A')).upper()
        close_price = closed_match.get('Close price', 'N/A')
        pl = closed_match.get('P/L', 'N/A')
        detail = f"""  Result         : {result}
  Close Price    : {close_price}
  P/L            : {pl}"""
    else:
        detail = "  Result         : UNKNOWN (could not confirm outcome, check manually)"

    ping_str = "<@1363959528194052118>\n" if SIGNALS_SENT_THIS_RUN == 0 else ""
    msg_text = f"""{ping_str}```text
AZALYST PROPFIRM SCANNER  —  POSITION CLOSED (TRADER: {trader_name.upper()})
{now_str}
--------------------------------------------------------------
{symbol}  {direction}
  >> VERDICT     : [CLOSED]
  Order #        : {order_num}
{detail}
```"""

    try:
        r = requests.post(DISCORD_WEBHOOK, json={"content": msg_text})
        if r.status_code in [200, 204]:
            print(f"[{datetime.now()}] Sent close alert for {trader_name} - {saved_position.get('symbol')}")
            SIGNALS_SENT_THIS_RUN += 1
        else:
            print(f"Failed to send close alert to Discord: {r.status_code}")
    except Exception as e:
        print(f"Discord error: {e}")

    # Close matching MT5 position
    if mt5.is_configured():
        success, mt5_msg, pl = mt5.execute_close(
            saved_position.get('symbol', 'N/A'),
            saved_position.get('direction', 'N/A')
        )
        print(f"[MT5] {mt5_msg}")
        if success:
            post_discord(f"🤖 **AUTO-CLOSE**: {mt5_msg}")


def run_scraper():
    print(f"[{datetime.now()}] Starting scraping cycle...")
    
    NEW_SIGNALS_TO_SEND = []

    # Update worst/best excursion for every still-open paper position using a
    # live market price, independent of anything the scraper finds this run.
    paper.poll_open_positions(PAPER_LEDGER)
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
            
            # Scrape every trader on the leaderboard - filtered below by track record
            print("Navigating to Leaderboard...")
            page.goto("https://rf-zone.rebelsfunding.com/leaderboard")
            page.wait_for_load_state('networkidle')
            time.sleep(5)

            rows = page.locator("tbody.p-datatable-tbody > tr").all()
            if not rows:
                print("No rows found on the leaderboard!")
                return

            # Rank every trader by %Gain.
            def parse_gain(text):
                try:
                    return float(text.replace('%', '').replace(',', '').strip())
                except (ValueError, AttributeError):
                    return float('-inf')

            candidates = []
            for i in range(len(rows)):
                cells = rows[i].locator("td").all()
                if len(cells) < 4:
                    continue
                name = cells[1].inner_text().strip()
                gain_text = cells[2].inner_text().strip()  # %Gain
                candidates.append({'index': i, 'name': name, 'gain_text': gain_text,
                                    'gain_val': parse_gain(gain_text)})

            ranked = sorted(candidates, key=lambda c: c['gain_val'], reverse=True)
            primary_traders = ranked[:PRIMARY_POOL_SIZE]
            fallback_traders = ranked[PRIMARY_POOL_SIZE:PRIMARY_POOL_SIZE + FALLBACK_POOL_SIZE]
            primary_indices = {t['index'] for t in primary_traders}
            fallback_indices = {t['index'] for t in fallback_traders}
            rank_by_index = {t['index']: rank for rank, t in enumerate(ranked, start=1)}
            print(f"Leaderboard has {len(rows)} traders - checking top {len(primary_traders)} by %Gain first: "
                  + ", ".join(f"#{rank} {t['name']} ({t['gain_text']})"
                              for rank, t in enumerate(primary_traders, start=1)))

            def scan_pool(indices, pool_name):
                """Scrapes the given trader row indices. Returns True if any
                currently open/pending trade among them is in an allowed
                symbol (Safest/Moderate tier)."""
                nonlocal rows
                found_allowed_signal = False
                for i in range(len(rows)):
                    if i not in indices:
                        continue
                    try:
                        # Re-query rows in case DOM changed
                        rows = page.locator("tbody.p-datatable-tbody > tr").all()
                        if i >= len(rows): break

                        row = rows[i]
                        cells = row.locator("td").all()
                        if len(cells) < 4: continue

                        name = cells[1].inner_text().strip()
                        gain_text = cells[2].inner_text().strip()  # %Gain
                        profit_text = cells[5].inner_text().strip()  # Closed Profit

                        print(f"Clicking trader row {i} ({pool_name}): {name}...")
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

                        # Default tab is "Closed Trades" - use it for win-rate/profit stats
                        closed_trades = extract_visible_trade_tables(page)

                        # Calculate win rate based on closed trades
                        total_closed = 0
                        wins = 0
                        for td in closed_trades:
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

                        win_rate_pct = (wins / total_closed) * 100 if total_closed > 0 else 0.0
                        win_rate_str = f"{win_rate_pct:.1f}%" if total_closed > 0 else "N/A"

                        profit_val = profit_text.replace('$', '').replace(',', '').strip()

                        trader_info = {
                            'name': name,
                            'rank': rank_by_index.get(i, 'N/A'),
                            'win_rate': win_rate_str,
                            'total_return': profit_val,
                            'gain': gain_text
                        }

                        # Open Trades and Pending Orders are separate tabs, not columns in
                        # the default (Closed Trades) table - each needs its own tab click.
                        current_position_keys = set()
                        for tab_name, status_label in [("Open Trades", "OPEN"), ("Pending Orders", "PENDING")]:
                            tab = page.get_by_role("tab", name=tab_name)
                            if tab.count() == 0:
                                continue
                            tab.first.click()
                            time.sleep(1.5)
                            for td in extract_visible_trade_tables(page):
                                td['Status'] = status_label
                                order_num = td.get('Order number', '')
                                pos_key = f"{name}_{order_num}"
                                current_position_keys.add(pos_key)
                                # A pool "has a signal" if it currently holds any open/pending
                                # trade in an allowed symbol - whether brand new or already
                                # known - since that's what decides whether we need to fall
                                # back to the next pool at all.
                                if paper.is_symbol_allowed(td.get('Symbol', 'N/A')):
                                    found_allowed_signal = True
                                if pos_key not in OPEN_POSITIONS:
                                    OPEN_POSITIONS[pos_key] = {
                                        'symbol': td.get('Symbol', 'N/A'),
                                        'direction': td.get('Direction', 'N/A'),
                                        'rank': trader_info.get('rank', 'N/A'),
                                        'win_rate': trader_info.get('win_rate', 'N/A')
                                    }
                                    if status_label == 'OPEN':
                                        paper.record_open(
                                            PAPER_LEDGER, pos_key,
                                            td.get('Symbol', 'N/A'),
                                            td.get('Direction', 'N/A'),
                                            td.get('Open price', 'N/A'),
                                        )
                                        paper.save_ledger(PAPER_LEDGER)
                                # Only alert on symbols this account actually trades
                                # (metals/crypto filtered out - see paper.ALLOWED_SYMBOLS).
                                if paper.is_symbol_allowed(td.get('Symbol', 'N/A')):
                                    NEW_SIGNALS_TO_SEND.append((trader_info, td))

                        # Anything we'd previously flagged as open/pending for this trader
                        # that isn't in this run's tabs anymore has been closed (or cancelled).
                        trader_prefix = f"{name}_"
                        known_keys = [k for k in OPEN_POSITIONS if k.startswith(trader_prefix)]
                        for pos_key in known_keys:
                            if pos_key in current_position_keys:
                                continue
                            order_num = pos_key[len(trader_prefix):]
                            closed_match = next((td for td in closed_trades if td.get('Order number') == order_num), None)
                            if paper.is_symbol_allowed(OPEN_POSITIONS[pos_key].get('symbol', 'N/A')):
                                send_discord_close(name, OPEN_POSITIONS[pos_key], closed_match, order_num)
                            if closed_match:
                                paper_msg = paper.record_close(PAPER_LEDGER, pos_key, closed_match.get('Close price', 'N/A'))
                                if paper_msg:
                                    post_discord(paper_msg)
                            del OPEN_POSITIONS[pos_key]

                        save_open_positions(OPEN_POSITIONS)

                        # Close modal
                        page.keyboard.press("Escape")
                        time.sleep(2)

                    except Exception as e:
                        print(f"Error extracting data for trader {i}: {e}")
                        page.keyboard.press("Escape")
                        time.sleep(2)

                return found_allowed_signal

            found_in_primary = scan_pool(primary_indices, f"top {len(primary_traders)}")
            if not found_in_primary and fallback_traders:
                print(f"No allowed-symbol open/pending trades among the top {len(primary_traders)} - "
                      f"checking next {len(fallback_traders)} (#{len(primary_traders) + 1}-#{len(primary_traders) + len(fallback_traders)})...")
                scan_pool(fallback_indices, f"next {len(fallback_traders)}")
            elif found_in_primary:
                print(f"Found allowed-symbol open/pending trade(s) in the top {len(primary_traders)} - skipping fallback pool.")
                
            if NEW_SIGNALS_TO_SEND:
                group_and_send_signals(NEW_SIGNALS_TO_SEND)
        except Exception as e:
            print(f"An error occurred during scraping: {e}")
        finally:
            browser.close()
            
    print(f"[{datetime.now()}] Scraping cycle complete.")
    
    if SIGNALS_SENT_THIS_RUN == 0:
        now_str = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime('%d %b %Y   %H:%M IST')
        heartbeat_msg = (f"REBEL FUNDING: Checked Leaderboard at {now_str}. No new open signals found.\n"
                          f"{paper.status_line(PAPER_LEDGER)}")
        try:
            requests.post(DISCORD_WEBHOOK, json={"content": heartbeat_msg})
        except:
            pass

    summary_msg = paper.week_summary_if_due(PAPER_LEDGER)
    if summary_msg:
        post_discord(summary_msg)

    # Post MT5 dashboard if executor is configured
    if mt5.is_configured():
        try:
            dashboard = mt5.get_dashboard_text()
            post_discord(dashboard)
        except Exception as e:
            print(f"[MT5] Dashboard error: {e}")

if __name__ == "__main__":
    run_scraper()

    if not os.path.exists(STATE_FILE):
        save_sent_signals(SENT_SIGNALS)
    if not os.path.exists(POSITIONS_FILE):
        save_open_positions(OPEN_POSITIONS)
    if not os.path.exists(paper.LEDGER_FILE):
        paper.save_ledger(PAPER_LEDGER)
