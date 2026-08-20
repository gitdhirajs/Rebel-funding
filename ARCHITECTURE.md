# Rebel Funding Scraper & MT4 Auto-Trader

## Overview
This system scrapes the Rebel Funding leaderboard, groups trades by symbol to find the majority consensus (Buy vs Sell), and automatically copies them to a local MetaTrader 4 (MT4) terminal. It is designed to run 24/7 on an Oracle Linux Cloud Server.

## Core Components

### 1. `live_signals_scraper.py`
The brain of the operation. It uses Playwright (headless Chromium) to scrape the leaderboard.
- **Daemon Mode**: Runs continuously in a `while True` loop, sleeping 15 minutes between cycles.
- **Top 50 Filter**: Scrapes the top 50 traders by %Gain (`PRIMARY_POOL_SIZE = 50`).
- **Majority Voting**: Groups open trades by symbol. If a symbol has multiple conflicting signals (e.g. 3 Buys and 1 Sell), it discards the minority direction and only trades the majority.
- **Anchor Logic**: When trading a symbol, it finds the **Highest Ranked Trader** in the majority group and "anchors" the MT4 trade to their specific `trade_id` (`MT4_ANCHORS`). It ignores when other lower-ranked traders close, and only closes the MT4 trade when that specific anchor trader closes theirs.

### 2. `mt4_bridge.py` (Local File Bridge)
Handles the communication between the Python scraper and the MT4 Terminal *without* using MetaAPI or webhooks.
- **Writing Commands**: Python writes `OPEN` or `CLOSE` actions to `commands.csv`.
- **Reading State**: Python reads `account_state.csv` (written by MT4) to get live floating equity, balance, and currently open positions.
- **Risk Reporting**: Calculates current Daily Drawdown (4% limit) and Max Drawdown (10% limit) using floating equity. **It does NOT halt trading if breached** (by user request for demo testing), but it accurately reports the breach percentage on the Discord dashboard.
- **Anti-Hedging Rule**: Prevents opening a trade if ANY position is already open for that symbol.
- **Unlimited Positions**: There is no limit on the maximum number of concurrent open positions (removed `MAX_OPEN_POSITIONS`).

### 3. `RebelGOAT_Bridge.mq4` (MT4 Expert Advisor)
Runs inside the MT4 terminal on the Oracle server.
- Triggers every 1 second via `OnTimer()`.
- Writes live account data and open trades to `account_state.csv` in the `MQL4/Files` directory.
- Reads `commands.csv`. If it finds a new command, it executes the trade (always `0.01` lots) and acknowledges it by replacing the command file with `ACK`.

### 4. `setup_oracle_mt4.sh`
A bash script for setting up the Oracle Ubuntu Cloud server. 
- Installs `wine` (Windows emulator for Linux) and `tightvncserver` (lightweight graphical desktop).
- Allows the user to connect via VNC Viewer to visually open MT4 and attach the EA, while Python runs in the background.

## Running the Bot on Oracle
1. Run `setup_oracle_mt4.sh` on the Oracle server to install Wine/VNC.
2. Connect via VNC Viewer (port 5901) and install MT4 (`wine mt4setup.exe`).
3. Place `RebelGOAT_Bridge.mq4` into the `MQL4/Experts` folder and drag it onto a chart.
4. Set environment variables in the SSH terminal:
   ```bash
   export MT4_FILES_DIR="$HOME/.wine/drive_c/Program Files (x86)/MetaTrader 4/MQL4/Files"
   export REBEL_EMAIL="..."
   export REBEL_PASSWORD="..."
   export DISCORD_WEBHOOK="..."
   ```
5. Run the scraper: `python3 live_signals_scraper.py`
