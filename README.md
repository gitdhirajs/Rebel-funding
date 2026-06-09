# Rebel Funding — Trader Intelligence & Trading Engine

Prop-firm trading competition analysis: 3,400+ traders across 32 competitions (Aug 2023 – May 2026). Reverse-engineers which traders are genuinely skilled, discovers what macro + technical conditions they exploit, and codifies that edge into repeatable strategies.

---

## Project Structure

### Data Downloaders (root)

| File | Purpose |
|---|---|
| `rebels_funding_scraper.py` | Selenium scraper — downloads per-trader trade-history Excel files from rebelsfunding.com leaderboard. Needs authenticated Chrome + matching chromedriver on PATH. |
| `scrape_news_calendar.py` | Scrapes Investing.com economic calendar (High/Medium impact events) for 11 countries. Computes beat/miss/inline surprise labels vs forecasts. |

### Documentation

| File | Purpose |
|---|---|
| `AGENTS.md` | Full project documentation — pipeline architecture, data schemas, script reference, gotchas, roadmap. **Read this first.** |

### Raw Data

| Folder | Contents |
|---|---|
| `trader_trades/` | 3,441 per-trader Excel files (one per competition rank). Naming: `Competition-MM-YY_R{rank}_{trader}.xlsx` |
| `candle_data/` | OHLCV candles from MetaTrader 5 — 51 symbols × 4 timeframes (M15, H1, H4, D1). Parquet format. `symbol_map.csv` maps canonical→broker names. |
| `price_cache/` | Cached price data for quick lookups. |

### Trading Engine (`engine/`)

Phase 2 — backtested trading strategies built on trader behavior analysis.

| File | Purpose |
|---|---|
| `setup_miner.py` | Clusters 118K trades into 124 setup rules (entry patterns from winning traders). |
| `event_miner.py` | Event-based rule mining — what price does after specific news events. |
| `enricher.py` | One-time trade context enrichment (S/R levels, session, trend at entry). |
| `seasonality_cot.py` | 10-year seasonality patterns + simulated COT (Commitment of Traders) data. |
| `cot_data.py` | CFTC COT downloader (blocked on this network — uses simulation fallback). |
| `market_state.py` | Live MT5 market readings: S/R levels, trend, session, spread. |
| `bernd_strategy.py` | Supply/demand zone detection on D1 + COT + seasonality bias hierarchy. |
| `challenge_engine.py` | 5-dimension consensus engine (technical, COT, news, seasonality, rebel winners). |
| `combined_engine.py` | Bernd daily bias filter + rebel intraday entry setup combined. |
| `signal_engine.py` | Signal generation from mined setup rules. |
| `orchestrator.py` | Master controller — mine, scan, watch, live modes. |
| `signal_quality.py` | Signal quality analysis and reporting. |
| `backtest.py` | D1 zone strategy backtest (13 trades, 54% WR, +27.1% P&L). |
| `backtest_combined.py` | Combined bias+setup backtest (7 trades, 0% WR — over-constrained). |

**Engine Data/Output Files:**

| File | Contents |
|---|---|
| `setup_rules.json` | 124 rebel winner entry patterns. |
| `setup_rules.html` | Visual strategy cards (open in browser). |
| `event_rules.json` | 3 event-based patterns. |
| `seasonality.parquet` | 120 month-symbol bias patterns (10yr history). |
| `cot_simulated.parquet` | 5,220 weekly COT records for 10 instruments. |
| `enriched_trades.parquet` | Cached trade context enrichment. |
| `backtest_results.parquet` | D1 zone backtest trade log. |
| `backtest_combined.parquet` | Combined backtest trade log. |
| `challenge_log.jsonl` | Live challenge engine signals. |
| `combined_signals.jsonl` | Combined engine signal log. |

### Reports

| File | Purpose |
|---|---|
| `reports/report_2026-05.html` | May 2026 monthly intelligence report — KPIs, top traders, key events, strategy patterns. |

---

## How to Run

### Prerequisites

- Python 3.12 (`C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe`)
- Chrome browser (for scraper)
- MetaTrader 5 terminal (for candle downloads, optional)

### Install dependencies

```bash
pip install pandas pyarrow openpyxl numpy MetaTrader5 requests beautifulsoup4 python-dateutil selenium
```

### Download trader data

1. Log into rebelsfunding.com in Chrome
2. Ensure matching chromedriver is on PATH
3. Run from project root:
```bash
python rebels_funding_scraper.py
```

### Download news calendar

```bash
python scrape_news_calendar.py
```

### Download candle data

Open and log into MT5 terminal first, then:
```bash
python download_candles.py
```

---

## Key Backtest Findings (Phase 2)

| Finding | Detail |
|---|---|
| 4/4 consensus = 67% WR | Location + trend + COT + seasonality agreement |
| XAU/USD best instrument | 67% WR vs XAG/USD 43% |
| 0.5% SL is viable | Drawdown stays within 10% prop firm limit |
| D1 too slow for challenges | 1-2 trades/month won't pass 30-day challenge |
| Rebel-only intraday ready | 124 setup rules ready for H1 backtest (50+ trades/month) |

---

## Environment

- **OS:** Windows 11 Pro
- **Python:** 3.12 only (3.11 is broken on this machine)
- **Project root:** `c:\Users\Administrator\Documents\Rebel Funding`

---

## Security Note

`config.py` (if present) contains API keys. Do NOT commit it to public repos. Rotate keys if exposed.
