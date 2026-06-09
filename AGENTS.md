# Project: Rebel Funding Trader Intelligence

Rebel Funding runs monthly prop-firm trading competitions (~150 traders/month). This project reverse-engineers **which traders are genuinely skilled (vs. lucky)**, discovers **what macro + technical conditions they exploit**, and codifies that edge into a **repeatable strategy playbook** — so the prop firm can decide who to fund and investors can find setups with historical edge.

**Core thesis — every meaningful trading day has TWO layers:**

1. **MACRO layer** — a news/economic event (e.g. CPI, NFP, rate decision) with a `surprise` of beat / miss / inline.
2. **TECHNICAL layer** — price context relative to support/resistance (previous-day high/low, round numbers).

The crowd follows the obvious narrative and is often wrong; the smart minority reads **both** layers. We identify the smart minority by finding **who was on the CORRECT side of the actual price reaction** (the direction price actually moved over the 4H window after the event), then analyze what their trades looked like (entry timing, SL placement, R:R, hold duration). The recurring, statistically-consistent setups become **strategy cards**.

---

## Environment

- **OS:** Windows 11 Pro. Shell is **PowerShell** (use PowerShell syntax: `$null` not `/dev/null`, `$env:VAR` not `$VAR`, backtick for line continuation). A Bash tool is also available for POSIX scripts.
- **Python — USE THIS EXACT INTERPRETER:**
  ```
  C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe
  ```
  Pip:
  ```
  C:\Users\Administrator\AppData\Local\Programs\Python\Python312\Scripts\pip.exe
  ```
  **Python 3.11 is BROKEN on this machine — NEVER use it.** Always invoke the full Python 3.12 path above. All `.bat` files already pin these absolute paths.
- **Project root:** `c:\Users\Administrator\Documents\Rebel Funding`. Almost every script uses **hardcoded absolute paths** rooted here, and a few are **cwd-dependent** (notably the scraper). Always run from the project root.
- **MetaTrader 5:** `download_candles.py` attaches to a **locally running, logged-in MT5 terminal** via the `MetaTrader5` Python API. The MT5 terminal **must be open and logged in** on the same machine, or candle downloads fail immediately (`MT5 init failed`).
- **Authenticated Chrome:** `rebels_funding_scraper.py` drives a plain `webdriver.Chrome()` — it does **not** log in. You must already be authenticated to `rebelsfunding.com` in the Chrome profile it launches, and a **matching chromedriver** must be on PATH (helper scripts `setup_chromedriver.py`, `download_correct_chromedriver.py`, `check_chrome.py` assist).

### Required pip packages

| Package | Used by |
|---|---|
| `pandas` | every analytics/ingestion script |
| `pyarrow` | all `.parquet` read/write |
| `openpyxl` | reading trader `.xlsx` (`build_master_trades.py`) |
| `numpy` | scoring / aggregation |
| `MetaTrader5` | `download_candles.py` |
| `requests` | `scrape_news_calendar.py` (Investing.com AJAX) |
| `beautifulsoup4` | `scrape_news_calendar.py` HTML parsing |
| `python-dateutil` | date parsing in news scraper |
| `selenium` | `rebels_funding_scraper.py` |

Install (run from project root):
```
C:\Users\Administrator\AppData\Local\Programs\Python\Python312\Scripts\pip.exe install pandas pyarrow openpyxl numpy MetaTrader5 requests beautifulsoup4 python-dateutil selenium
```

### Security note

`config.py` contains a **hardcoded plaintext FMP (Financial Modeling Prep) API key**. **Do NOT commit `config.py`** to any shared/public repo. If it has been exposed, rotate the key. Prefer migrating it to an environment variable.

---

## Data Pipeline Overview

```
                              RAW SOURCES
 ┌──────────────────────┐  ┌────────────────────┐  ┌───────────────────────┐
 │ rebelsfunding.com     │  │ MetaTrader 5        │  │ Investing.com calendar │
 │ leaderboard (Selenium)│  │ terminal (MT5 API)  │  │ (HTTP AJAX endpoint)   │
 └──────────┬───────────┘  └─────────┬──────────┘  └───────────┬───────────┘
            │ rebels_funding_scraper  │ download_candles         │ scrape_news_calendar
            ▼                         ▼                          ▼
   trader_trades/*.xlsx      candle_data/<TF>/*.parquet     news_calendar.parquet
   (one per trader)          (M15,H1,H4,D1 × 51 symbols)    + symbol_map.csv (from candles)
            │                         │                          │
            │ build_master_trades     │                          │ analyze_events
            ▼                         │                          ▼
   master_trades.parquet  ◄───────────┴───────────────►  event_reactions.parquet
   (+ processed_competitions.txt)        (H1 candles join)  (event × instrument reactions)
            │                                                    │
            │                                  ┌─────────────────┤
            │ cluster_traders                  │                 │ macro_playbook
            ▼                                  ▼                 ▼
   trader_clusters.parquet  +  event_playbook.parquet     macro_playbook.parquet
   (every in-window trade,     (per-event crowd-vs-       (Event×Symbol pattern DB:
    labelled CORRECT/WRONG)      smart cluster stats)       "UP/DOWN X% of the time")
            │                          │                          │
            │ score_traders            │                          │ generate_strategies
            ▼                          ▼                          ▼
   trader_scores.parquet         (feeds reports)         strategy_cards.json + .html
   (0-100 skill_score,                                   (human-readable edge cards)
    franchise/contrarian flags)
            │
            └──────────────► monthly_report.py ──► reports/report_YYYY-MM.html
                             (reads ALL of the above)
```

Newest-competition-first processing; all intermediate artifacts are **parquet**; symbol names are normalized so trades, candles, and news all join.

---

## Scripts Reference

> All run commands assume cwd = `c:\Users\Administrator\Documents\Rebel Funding`. `PYTHON` below = `C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe`.

### Ingestion

#### `rebels_funding_scraper.py`
- **Purpose:** Selenium automation that downloads per-trader trade-history Excel files for all traders across all Rebel Funding competitions (Aug 2023 – May 2026). Navigates the leaderboard-history pages, clicks each trader row, triggers the in-page **Excel export**, then renames the file. Idempotent/resumable: skips ranks already on disk.
- **Run:** `PYTHON rebels_funding_scraper.py` (from project root, with an authenticated Chrome + matching chromedriver on PATH).
- **Inputs:** WEBSITE only. Navigates `https://rf-zone.rebelsfunding.com/leaderboard/history/{comp_id}` for 32 hardcoded `(id, name)` competitions in `COMPETITIONS`. No local input files.
- **Outputs:** `trader_trades/{comp_name}_R{idx}_{safe_name}.xlsx` (e.g. `Competition-01-26_R100_Abhishek V.xlsx`). Browser first drops `trades_*.xlsx` into `trader_trades/`, then the script renames the newest match. Progress to stdout only.
- **Output schema:** Raw broker `.xlsx` (internal schema documented under `build_master_trades.py`). **The filename convention IS the downstream contract:** `Competition-MM-YY_R{rank}_{trader}.xlsx`. `idx` = table row index used as rank `R`; `safe_name` = trader name with non-word chars stripped, truncated to 30 chars; collisions get `_1`, `_2` suffix.
- **Key params:** `COMPETITIONS` (32 tuples, Aug 2023→May 2026; June 2026 id `1336` deliberately skipped as "latest"). `dl_dir = os.path.join(os.getcwd(),'trader_trades')`. Hardcoded sleeps: 6s after page load, 3s after row click, 5s after export, 1s between traders; row refresh every 15 traders. Selectors: rows `tr[role="row"]`, name `td:nth-child(2) span`, export `//span[contains(text(),'Excel export')]` → `./ancestor::button`, modal close `.rf-modal-close`.
- **Gotchas:** (1) **cwd-dependent** — `dl_dir` is relative to `getcwd()`; MUST run from project root. (2) Does NOT log in — needs an already-authenticated Chrome. (3) Resume regex `{comp_name}_R(\d+)_` resumes from `max(existing)+1`; **a failed middle rank is NOT retried**. (4) Bare `except:` swallows everything — chromedriver version mismatches look like per-trader failures. (5) Fragile DOM selectors; any redesign breaks it. (6) Emoji in stdout — Windows console may need UTF-8.

#### `download_candles.py`
- **Purpose:** Downloads OHLCV candle history for the Rebel Funding instruments from a running MT5 terminal; one parquet per `(timeframe, symbol)`. Resolves each instrument's broker-specific name via a variant list, then incrementally fetches/appends. Idempotent: skips up-to-date symbols, else fetches only the delta.
- **Run:** `PYTHON download_candles.py` (MT5 terminal open + logged in).
- **Inputs:** `MetaTrader5` API attached to running terminal. Existing `candle_data/<tf>/*.parquet` read to compute incremental start. `SYMBOL_VARIANTS` maps 71 canonical names → ordered broker candidates (e.g. `XAU/USD` → `[XAUUSD, XAU/USD, GOLD]`).
- **Outputs:** `candle_data/<TF>/<symbol>.parquet` for `TF ∈ {M15,H1,H4,D1}`; filename = canonical name with `/` and `.` → `_` (e.g. `AUD/CAD`→`AUD_CAD.parquet`, `USTEC.v`→`USTEC_v.parquet`). Also writes `candle_data/symbol_map.csv`. **At doc time: 51 of 71 symbols resolved** (present in each TF).
- **Output schema (per-candle parquet):**

  | Column | Type | Meaning |
  |---|---|---|
  | `time` | datetime64 | candle open time (from epoch seconds) |
  | `symbol` | str | canonical instrument name |
  | `open` `high` `low` `close` | float64 | OHLC |
  | `volume` | int64 | MT5 **tick_volume** (NOT real contract volume) |
  | `spread` | int64 | broker spread |

  `symbol_map.csv`: `symbol` (canonical), `broker_name` (resolved broker symbol).
- **Key params:** `START_DATE=2023-08-01` (H1/H4/D1), `START_DATE_M15=2025-12-01` (brokers keep less M15 history), `END_DATE=now()`. `TIMEFRAMES` dict pairs each TF with its start. Retry fallback `alt_start=2025-12-01`. `symbol_select(...,True)` + 0.1s sleep before `copy_rates_range`.
- **Gotchas:** (1) MT5 terminal MUST be running/logged in. (2) **M15 only goes back to ~Dec 2025** by design. (3) **DE30.v is unavailable** — `symbol_map.csv` shows `DE30.v→GER30` resolved but NO DE30 parquet exists in any TF; treat DE30/DAX as unavailable downstream. (4) 20 of 71 symbols did not resolve at all. (5) Incremental append re-fetches the boundary candle (dedup on `time` handles it). (6) `volume` is tick_volume. (7) Filenames replace BOTH `/` and `.` with `_` — match exactly when reading (`analyze_events.py` uses the same transform).

#### `scrape_news_calendar.py`
- **Purpose:** Scrapes the Investing.com economic calendar (US, EU, UK, Japan, Australia, Canada, China, Switzerland, NZ, Germany, France) for High/Medium impact events, parses actual/forecast/previous, computes a beat/miss/inline `surprise`, maps each currency to affected instruments. Incremental/resumable: re-scrapes only the last ~2 months and merges.
- **Run:** `PYTHON scrape_news_calendar.py` (internet required; expect occasional blocking — just re-run).
- **Inputs:** HTTP POST `https://www.investing.com/economic-calendar/Service/getCalendarFilteredData` (JSON whose `data` is an HTML fragment). Browser-like session with spoofed UA + `X-Requested-With=XMLHttpRequest`. Reads existing `news_calendar.parquet` for resume point. `CURRENCY_INSTRUMENTS` maps each currency → affected instruments.
- **Outputs:** `news_calendar.parquet` (single file, deduped + sorted by datetime).
- **Output schema:** see [Data Artifacts](#data-artifacts--schemas) below.
- **Key params:** `START=2023-08-01`, `END=now()`. `COUNTRIES=['5','22','4','35','25','6','11','12','36','72','55']` (5=US,22=Eurozone,4=UK,35=Japan,25=Australia,6=Canada,11=China,12=Switzerland,36=NZ,72=Germany,55=France). `IMPORTANCE=['3','2']` (3=High,2=Medium). POST form: `timeZone='55'`, `timeFilter='timeRemain'`, `currentTab='custom'`, `dateFrom/dateTo` per month. Resume: `last stored datetime − 2 months, day=1`; existing rows ≥ that are re-fetched. Sleeps 2s/month, 3s+session refresh on a failed month (one retry).
- **Gotchas:** (1) Parser depends on Investing.com DOM: rows `tr.js-event-item`; **impact read from the sentiment `<td>`'s `data-img_key`** (`bull3`=High, `bull2`=Medium, else skipped) — if that attr changes, the impact filter silently drops everything. (2) Event datetime from row `data-event-datetime` (slashes→dashes). (3) actual/forecast/previous via `eventActual_{id}` / `eventForecast_{id}` / `eventPrevious_{id}` with a class-regex fallback. (4) `surprise` strips non-`[0-9.-]` then compares actual vs forecast — **% vs absolute and K/M/B suffixes are NOT unit-normalized**, so surprise can be wrong for suffixed values; any parse failure → `surprise=''`. (5) Anti-bot may block → prints "Investing.com may be blocking requests" and bails; re-run later. (6) `fetch_month` returns an HTML string OR `(None, errmsg)` tuple — callers must `isinstance(...,tuple)` check. (7) Dedup key `(datetime, currency, event)`.
- **Note:** `update_news.bat` window title/echoes say "ForexFactory" but the script actually scrapes **Investing.com** (ForexFactory blocks scraping).

### Analysis

#### `build_master_trades.py`
- **Purpose:** Consolidates all per-trader `.xlsx` into one tidy `master_trades.parquet`. Parses competition/rank/trader from each filename, normalizes columns, parses timestamps, appends competition-by-competition (newest first). Idempotent: tracks completed competitions and skips them.
- **Run:** `PYTHON build_master_trades.py`
- **Inputs:** all `*.xlsx` in `TRADES_DIR = ...\trader_trades`, `sheet_name='Trades'` (engine `openpyxl`). Reads/writes `processed_competitions.txt`. Filenames must match `FILENAME_RE = Competition-(MM)-(YY)_R(rank)_(trader).xlsx` (case-insensitive); non-matching files silently skipped.
- **Outputs:** `master_trades.parquet` (combined, rewritten each run) + `processed_competitions.txt` (one key like `2026-05` per line).
- **Output schema:** see [Data Artifacts](#data-artifacts--schemas). **Note the doubled `pct` in `p_l_pct_pct`** — the source header is literally `P/L % %`.
- **Key params:** `TRADES_DIR`, `OUTPUT_FILE`, `PROGRESS_FILE` absolute paths at top. Competition key = `'20{YY}-{MM}'`. Final sort `['competition','opened']` ascending `[False, True]`. Win-rate uses `status.str.upper()=='WIN'`.
- **Gotchas:** (1) **`dayfirst=True` is REQUIRED** — source dates are `DD/MM/YYYY HH:MM`. (2) Only reads a sheet literally named `Trades`; files without it are skipped with an ERROR print. (3) `commission` arrives as `'$-0.98'`; `$` stripped before `to_numeric(errors='coerce')`. (4) **Idempotency depends on `processed_competitions.txt`** — delete it to force a full rebuild. **If you delete `master_trades.parquet` but NOT the txt, a re-run produces nothing.** (5) On append it removes existing rows for the current competition first (safe re-processing). (6) `duration` stays a string `'HH:MM'` here (parsed to minutes downstream). (7) The doubled `p_l_pct_pct` name is easy to mistype downstream.

#### `analyze_events.py`
- **Purpose:** Joins news events to candle data. For every `(event, affected-instrument)` pair, measures H1 price move at 1H/4H/8H after the event, derives a primary direction (4H), computes nearby key levels (PDH/PDL + nearest round number), flags level confluence. Also prints (console-only) a macro-playbook aggregation.
- **Run:** `PYTHON analyze_events.py` (requires `news_calendar.parquet` and `candle_data/H1/*.parquet`).
- **Inputs:** `NEWS_FILE=news_calendar.parquet`; H1 candle parquets from `CANDLE_DIR=candle_data` (symbol→file: `/` and `.` → `_`). Candles cached in-memory per symbol.
- **Outputs:** `event_reactions.parquet` (one row per event-instrument pair with candle coverage).
- **Output schema:** see [Data Artifacts](#data-artifacts--schemas).
- **Key params:** `REACTION_WINDOWS={'1H':1,'4H':4,'8H':8}` hours; primary direction = 4H window. `near_level`: within 0.5% of PDH/PDL, within 0.3% of `round_level`; `round_level` magnitude = `10**(len(str(int(price)))-2)` (~2 sig figs). Playbook print filter: occurrences≥3, top 30.
- **Gotchas:** (1) **H1 candles ONLY** — events outside H1 coverage (or on unresolved / DE30 symbols) silently produce no rows. (2) `get_price_at/after` use `searchsorted(side='right')-1` = "last candle at or before target" — measured at candle close boundaries, not exact event tick. (3) `move` is a simple pct change of close prices — **NOT pips, NOT spread/slippage adjusted** (the "pip" naming is misleading). (4) **Round-number logic breaks for sub-1.0 FX prices** (`int(price)==0` → magnitude `10**-1`), so round levels for e.g. EUR/USD are coarse/wrong. (5) Thin/weekend prior days may yield empty PDH/PDL. (6) **No incremental mode** — recomputes from scratch and overwrites the parquet each run. (7) Skips events with empty `instruments`.

#### `cluster_traders.py`
- **Purpose:** The payoff step. For each event with a known price direction, finds all trader trades opened in a window around the event on the affected symbol, splits them **CORRECT vs WRONG** (vs the event's `primary_dir`), and summarizes each cluster (win rate, avg P/L, avg R:R, avg duration, BUY/SELL counts). Produces a labelled-trade table and an event-level playbook.
- **Run:** `PYTHON cluster_traders.py` (requires `event_reactions.parquet` AND `master_trades.parquet`).
- **Inputs:** `REACTIONS_FILE=event_reactions.parquet`, `TRADES_FILE=master_trades.parquet` (both required, else ERROR and return).
- **Outputs:** `trader_clusters.parquet` (every in-window trade, labelled) + `event_playbook.parquet` (one row per event with cluster stats).
- **Output schema:** see [Data Artifacts](#data-artifacts--schemas).
- **Key params:** `TRADE_WINDOW_BEFORE=1h`, `TRADE_WINDOW_AFTER=4h` (matched on `trades.opened`). `correct_trade_dir = BUY if primary_dir=='UP' else SELL`. Highlights: `total_traders≥3`; crowd-wrong if `crowd_wrong_pct≥50`; best-correct if `correct_n≥3`. R:R = `abs(entry−tp)/abs(entry−sl)` per trade, averaged. Duration parsed from `'HH:MM'`→minutes.
- **Gotchas:** (1) Depends on the exact normalized `master_trades` column names — a rename upstream silently zeroes stats. (2) **Trades match events by `opened` in-window AND exact symbol string equality** — symbol naming MUST align between `master_trades` (broker-style e.g. `XAU/USD`) and `event_reactions` (canonical), or zero matches. (3) `win_rate` via `status.str.upper()=='WIN'` (0-1, printed as %). (4) `parse_duration` assumes `'HH:MM'`; anything else → None and dropped. (5) Only events with non-null `primary_dir`; events with no in-window trades produce no playbook row. (6) **No incremental mode** — overwrites both parquets. (7) `crowd_wrong_pct` is share of **trades** (not traders) in the WRONG cluster. (8) Requires `master_trades.parquet` to exist first — errors out otherwise.

### Intelligence

#### `macro_playbook.py`
- **Purpose:** Macro Event Playbook builder. Aggregates per-event reactions into a pattern DB: "When [Event] on [Symbol] → price goes [UP/DOWN] X% of the time." Three nested levels per Event×Symbol: (L1) overall directional stats, (L2) breakdown by `surprise` (beat/miss/inline), (L3) confluence stats when price was near a key S/R level. Filters to patterns meeting min occurrence + confidence, prints a summary + confluence-highlights table.
- **Run:** `PYTHON macro_playbook.py`
- **Inputs:** `event_reactions.parquet` (REQUIRED — missing prints "run analyze_events.py" and returns). Consumes: `event`, `symbol`, `primary_dir`, `surprise`, `move_1H_pct`, `move_4H_pct`, `move_8H_pct` (optional/guarded), `near_level`, `event_datetime`.
- **Outputs:** `macro_playbook.parquet` (`index=False`) + console tables. **If no patterns qualify, NOTHING is written** (a prior parquet is left stale or absent).
- **Output schema:** see [Data Artifacts](#data-artifacts--schemas).
- **Key params:** `MIN_OCCURRENCES=4` (applied to `valid_n` = rows with non-null `primary_dir`), `MIN_CONFIDENCE=60.0`. Inside code (not module constants): per-surprise sub-pattern requires `len(sub)>=2`; confluence L3 requires ≥2 near-level rows AND ≥2 with valid dir; printed confluence HIGHLIGHTS require `confluence_n>=3 AND confluence_confidence>=70`.
- **Gotchas:** (1) `occurrences` counts ALL group rows incl. NaN dir, but `MIN_OCCURRENCES` filters on `valid_n` — they can differ. (2) `dominant_dir` ties (50/50) → `'UP'` (test is `up_pct >= dn_pct`). (3) `avg_move_8H` guarded by `if 'move_8H_pct' in valid` → None if column missing. (4) **`confidence` = max of the two directional %; NOT a significance measure** — `valid_n` as low as 4 can show high "confidence". (5) If zero patterns qualify, no parquet is written → downstream may read stale/missing. (6) Parquet contains **looser** confluence rows (≥2) than the printed highlights (≥3, conf≥70).

#### `score_traders.py`
- **Purpose:** Trader Skill Scoring. Scores every trader 0-100 (`skill_score`) on event-direction accuracy, win rate, risk management, consistency vs crowd behaviour. Flags **franchise** traders (high skill across multiple competitions) and **contrarian** traders (rarely follow the crowd). Merges event stats from `trader_clusters` with risk/volume stats from all `master_trades`.
- **Run:** `PYTHON score_traders.py`
- **Inputs (both required):** `trader_clusters.parquet` (consumes `trader`, `cluster`, optional `status`/`competition`/`opened`) and `master_trades.parquet` (consumes `trader`, `open_price`, `stop_loss`, `take_profit`, `duration`, optional `status`/`competition`/`opened`).
- **Outputs:** `trader_scores.parquet` (`index=False`) + Top-30 / FRANCHISE / CONTRARIAN console tables.
- **Output schema:** see [Data Artifacts](#data-artifacts--schemas).
- **Key params:** Weights (sum=1.0): `W_EVENT_ACCURACY=0.35`, `W_WIN_RATE=0.25`, `W_RISK_MGMT=0.20`, `W_CONSISTENCY=0.20`. `score_risk = rr_score(norm avg_rr 0.5..3.0)*0.6 + sl_score(norm sl_logic_rate 0..1)*0.4`. `score_consistency = comp_score(norm competitions 1..6)*0.7 + volume_score(norm event_trades 0..20)*0.3`. SL "sane" band = 0.05%..5.0% of entry. **Franchise:** `skill_score>=65 AND competitions>=2`. **Contrarian:** `crowd_follow_rate < 0.3`.
- **Gotchas:** (1) **EDITORIAL MODEL ASSUMPTION:** the crowd/majority is treated as WRONG by definition — `crowd_follow_rate = n_wrong/n_events`. Not derived from data. (2) Missing-data fills bias scores to the middle (`event_accuracy`→0.5, `avg_rr`→1.0, `sl_logic_rate`→0.5, `competitions`→1, `event_trades`→0), so a no-data trader still scores ~50. (3) Duration parsed as `X:Y → X*60+Y` minutes; other formats → None/dropped. (4) `calc_rr`/`sl_is_logical` use `iterrows()` over ALL trades — slow on 1100+ traders. (5) Truthiness guards (`if avg_rr`) coerce a genuine `0.0` to None. (6) `competitions` = elementwise max of the two source counts after outer merge.

#### `generate_strategies.py`
- **Purpose:** Strategy Rules Engine. Converts the highest-confidence macro-playbook patterns into human-readable **strategy cards** (JSON + styled standalone HTML dashboard). Enriches each card with crowd-vs-smart-money stats from `event_playbook` and a hold-time/timing note from correct traders' avg duration. Only surfaces patterns meeting min occurrence + confidence.
- **Run:** `PYTHON generate_strategies.py`
- **Inputs:** `macro_playbook.parquet` (REQUIRED — missing → run `macro_playbook.py`). **`CLUSTERS_FILE` (misleadingly named) points at `event_playbook.parquet`** (OPTIONAL — if absent, crowd/smart fields blank). Consumes from event_playbook: `crowd_wrong_pct`, `correct_win_rate`, `correct_avg_rr`, `correct_avg_dur`, `correct_n`.
- **Outputs:** `strategy_cards.json` (indent=2, default=str) + `strategy_cards.html` (UTF-8 dark-theme dashboard) + first-10-cards console print. **If no cards qualify, NOTHING is written.**
- **Output schema:** JSON = list of card objects sorted by confidence desc then occurrences desc. Each card: `id` (1-based in playbook order, assigned BEFORE final sort → ids NOT in displayed order), `event`, `symbol`, `occurrences`, `dominant_dir`, `confidence`, `avg_move_1H_pct`/`4H`/`8H` (or None), `by_surprise` (list of `{type, n, direction, confidence, avg_4H_pct}`, only when `n>=2`), `confluence` (null or `{n, direction, confidence, avg_4H_pct, label}`, only when `confluence_n>=3 AND confluence_confidence>=70`), `crowd_wrong_pct`, `correct_trader_n`, `correct_win_rate`, `correct_avg_rr`, `timing_note`.
- **Key params:** `MIN_OCCUR=5` (STRICTER than macro_playbook's 4), `MIN_CONFIDENCE=65.0` (STRICTER than 60.0). Timing buckets: `<60min`=scalp, `<240min`=intraday, else swing (hrs).
- **Gotchas:** (1) `CLUSTERS_FILE` actually = `event_playbook.parquet`, NOT `trader_clusters.parquet`. (2) Stricter thresholds here drop some rows present in `macro_playbook.parquet`. (3) Card `id` order ≠ JSON/displayed order. (4) Truthiness guards coerce real `0.0` → None. (5) Crowd/smart fields rely on the OPTIONAL event_playbook; absent → blank. (6) crowd/correct fields aggregated via `.mean()/.sum()` across ALL matching event_playbook rows (all months), not one instance. (7) **HTML emitted with raw f-string interpolation — no escaping**, so `<` or `&` in event/symbol can break rendering.

#### `monthly_report.py`
- **Purpose:** Monthly Intelligence Report generator. Auto-generates a standalone HTML report for one competition month: (1) overview KPIs, (2) key macro events with matching historical pattern, (3) crowd-vs-smart event breakdown, (4) top traders joined to skill scores, (5) all-time strongest playbook patterns. Defaults to latest month or accepts `YYYY-MM` via CLI.
- **Run:** `PYTHON monthly_report.py` (latest month) OR `PYTHON monthly_report.py 2026-05` (specific month).
- **Inputs:** optional `sys.argv[1]='YYYY-MM'`. Loads up to six parquets if they exist (never hard-fails on missing → empty DataFrame): `master_trades` (`trades`), `trader_clusters` (`clusters`, **loaded but unused**), `event_playbook` (`playbook`), `trader_scores` (`scores`), `macro_playbook` (`macro`), `news_calendar` (`news`). Columns containing `date`/`time` or named `opened`/`closed` coerced to datetime.
- **Outputs:** `reports/report_{month_str}.html` (dir auto-created, UTF-8, overwritten per month). No parquet output.
- **Key params:** Events matched on playbook `event[:15]` (case-insensitive contains) AND exact symbol; checks first 2 instruments; display caps at first 4 instruments. Crowd-vs-smart head(15); top traders head(20); strongest playbook head(5). Franchise star uses `skill_score>=65`. Month-news filter requires `impact=='High'`.
- **Gotchas:** (1) Month precedence: CLI arg > `trades['competition'].max()` (lexical max — works only because months are zero-padded `YYYY-MM`) > `now()`. (2) Sections filtered by DIFFERENT month keys: trades by `competition==month_str`; news by `datetime` year+month AND `impact=='High'`; event_playbook by `event_date` year+month — a mismatch desyncs sections. (3) `clusters` loaded but never used. (4) News filter needs both `datetime` and `impact=='High'`; case/spelling differences silently drop events. (5) Truthiness guards (`if r['skill_score']`) print genuine `0.0` skill as em-dash. (6) **Raw f-string HTML, no escaping.** (7) `event[:15]` prefix match can mis-match short/similar names. (8) Robust to missing inputs but then renders mostly-empty sections.

### Orchestration

#### `config.py`
- **Purpose:** Single-line config module holding the FMP API key for upstream data-fetch scripts.
- **Schema/params:** `FMP_API_KEY` (str). Current literal: `"gI1YvNJI3Au609sRd7ZDp3IlxDtuLcFt"`.
- **Run:** Not a pipeline step. Imported via `from config import FMP_API_KEY`. Inspect: `PYTHON -c "import config; print(config.FMP_API_KEY)"`.
- **Gotchas:** (1) **SECURITY** — live-looking key in plaintext; do not commit; rotate if leaked. (2) None of the five analytics scripts import it (they read parquet); it is consumed by upstream fetch scripts. (3) Import-only.

#### `run_all.bat` — Full Intelligence Pipeline orchestrator
- **Purpose:** Canonical end-to-end run order. `cd`s to project dir, pins Python 3.12 + pip absolute paths, installs deps, runs the pipeline stages in fixed sequence, prints key outputs, pauses.
- **Run:** `run_all.bat` (double-click, or `c:\Users\Administrator\Documents\Rebel Funding\run_all.bat`). Self-`cd`s.
- **Stages (exact order):** `[1/6]` pip install `pandas pyarrow openpyxl numpy` → `[2/6]` `build_master_trades.py` → `[3/6]` `analyze_events.py` → `[4/6]` `cluster_traders.py` → `[5/6]` `macro_playbook.py` → `[6/6]` `score_traders.py` → (unnumbered) `generate_strategies.py` → (unnumbered) `monthly_report.py`.
- **Key params:** `PYTHON`/`PIP` Python 3.12 absolute paths; reinstalls deps every run; `color 0A`, `title`, trailing `pause`.
- **Gotchas:** (1) **Labels lie:** prints `[1/6]..[6/6]` but runs **8 stages** (2 unlabeled at the end). (2) **No error checking** — a failing early stage does NOT stop the pipeline; later stages run on stale/missing parquet. (3) Hardcoded Python 3.12 path — fails if not installed there. (4) Reinstalls pip deps every run. (5) Ends with `pause` — **blocks on keypress, unsuitable for unattended/scheduled runs** without modification. (6) Does NOT run `download_candles.py`, `scrape_news_calendar.py`, or `rebels_funding_scraper.py` — those are upstream (`download_candles.bat`, `update_news.bat`, scraper) and must be run first.

#### `download_candles.bat`
- Steps: pip install `MetaTrader5 pandas pyarrow` → `download_candles.py` → `build_master_trades.py`. (Despite labels `[1/2]`/`[2/2]`/`[3/3]`, it does candles **and** the master-trades build.) MT5 must be open.

#### `update_news.bat`
- Steps: pip install `requests pandas pyarrow python-dateutil playwright beautifulsoup4` + `playwright install chromium` → `scrape_news_calendar.py`. (Window title says "ForexFactory" but the scraper hits **Investing.com**.) Safe to re-run; resumes.

---

## Data Artifacts & Schemas

> Root for all parquet artifacts: `c:\Users\Administrator\Documents\Rebel Funding`. **At documentation time, `master_trades.parquet`, `event_reactions.parquet`, `event_playbook.parquet`, `trader_clusters.parquet`, `macro_playbook.parquet`, `trader_scores.parquet`, `strategy_cards.*`, and `reports/` did NOT yet exist** — they are generated by a full pipeline run. Present root data artifacts: `leaderboard_full.csv`, `leaderboard_raw.json`, `news_calendar.parquet`, `nonascii_lines.txt`, `requirements.txt`, `trader_ids.txt`. Candle parquets live under `candle_data/<TF>/`.

### `master_trades.parquet` — from `build_master_trades.py`
One row per individual trade across all traders/competitions. Column names are normalized: lowercase, spaces→`_`, `/`→`_`, `%`→`pct`.

| Column | Type | Meaning |
|---|---|---|
| `symbol` | str | broker symbol (e.g. `XAU/USD`) |
| `order_number` | int64 | broker order id |
| `status` | str | `WIN` / `LOSS` |
| `direction` | str | `BUY` / `SELL` |
| `p_l` | float64 | profit/loss (currency) |
| `p_l_pct_pct` | float64 | P/L percent (**doubled `pct`** — source header was `P/L % %`) |
| `open_price` | float64 | entry price |
| `close_price` | float64 | exit price |
| `stop_loss` | float64 | SL price |
| `take_profit` | float64 | TP price |
| `volume` | float64 | lot size |
| `opened` | datetime64 | open time (parsed `dayfirst=True` from `DD/MM/YYYY HH:MM`) |
| `closed` | datetime64 | close time (`dayfirst=True`) |
| `duration` | str | `'HH:MM'` (string; parsed to minutes downstream) |
| `commission` | float64 | `$` stripped → numeric |
| `swap` | int64 | swap |
| `competition` | str | `'YYYY-MM'` (from filename) |
| `rank` | int | leaderboard rank (from filename `R{rank}`) |
| `trader` | str | trader display name (verbatim from filename, stripped) |

Sorted by `['competition','opened']` ascending `[False, True]` (newest competition first, trades chronological within).

### `news_calendar.parquet` — from `scrape_news_calendar.py`

| Column | Type | Meaning |
|---|---|---|
| `datetime` | datetime64 | event timestamp |
| `date` | datetime64 | normalized day |
| `currency` | str | e.g. `USD` |
| `event` | str | event name |
| `impact` | str | `High` / `Medium` |
| `actual` | str | raw actual (text, not unit-normalized) |
| `forecast` | str | raw forecast |
| `previous` | str | raw previous |
| `surprise` | str | `beat` / `miss` / `inline` / `''` |
| `instruments` | str | comma-joined affected instruments (`CURRENCY_INSTRUMENTS`) |

Deduped on `(datetime, currency, event)`, sorted by datetime.

### `event_reactions.parquet` — from `analyze_events.py`
One row per `(event, instrument)` with H1 candle coverage.

| Column | Type | Meaning |
|---|---|---|
| `event_datetime` | datetime64 | event time |
| `event_date` | datetime64 | event day |
| `currency` | str | event currency |
| `event` | str | event name |
| `impact` | str | High/Medium |
| `actual` `forecast` | str | raw values |
| `surprise` | str | beat/miss/inline/'' |
| `symbol` | str | affected instrument (canonical) |
| `price_at_event` | float64 | close of candle at/just-before event |
| `pdh` `pdl` | float\|None | previous-day high/low |
| `round_level` | float\|None | nearest round number (~2 sig figs) |
| `near_level` | str\|None | `PDH` / `PDL` / `ROUND_{lvl}` |
| `primary_dir` | str\|None | `UP` / `DOWN` (= **4H** direction) |
| `move_1H_pct` `move_4H_pct` `move_8H_pct` | float\|None | signed % change of close vs `price_at_event` |
| `dir_1H` `dir_4H` `dir_8H` | str\|None | `UP` / `DOWN` per window |

### `event_playbook.parquet` — from `cluster_traders.py`
One row per event analyzed (with in-window trades).

| Column | Type | Meaning |
|---|---|---|
| `event_datetime` `event_date` | datetime64 | event time/day |
| `currency` `event` `impact` `surprise` | str | event metadata |
| `symbol` | str | instrument |
| `correct_dir` | str | `UP` / `DOWN` (the side that was correct) |
| `price_at_event` | float | price at event |
| `near_level` | str\|None | level confluence tag |
| `move_1H_pct` `move_4H_pct` | float | price moves |
| `total_traders` | int | traders with in-window trades |
| `crowd_wrong_pct` | float | share of **trades** in WRONG cluster |
| `correct_n` | int | unique CORRECT-side traders |
| `correct_win_rate` | float\|None | 0-1, `status=='WIN'` |
| `correct_avg_rr` | float\|None | avg R:R of correct trades |
| `correct_avg_pl` | float\|None | avg P/L of correct trades |
| `correct_avg_dur` | float\|None | avg duration (minutes) |
| `wrong_n` | int | wrong-side trade count |
| `wrong_win_rate` `wrong_avg_rr` `wrong_avg_pl` | float\|None | wrong-cluster stats |

### `trader_clusters.parquet` — from `cluster_traders.py`
Every in-window trade, labelled. = **all `master_trades` columns** for matched trades, PLUS:

| Column | Type | Meaning |
|---|---|---|
| `cluster` | str | `CORRECT` / `WRONG` (vs event `primary_dir`) |
| `event` | str | event name |
| `event_dt` | datetime64 | event time |
| `correct_dir` | str | `UP` / `DOWN` |
| `surprise` | str | beat/miss/inline |
| `near_level` | str\|None | level confluence tag |

### `macro_playbook.parquet` — from `macro_playbook.py`
One row per qualifying Event×Symbol pattern (sorted confidence desc, occurrences desc).

| Column | Type | Meaning |
|---|---|---|
| `event` `symbol` | str | pattern key |
| `surprise_filter` | str | always literal `'all'` |
| `occurrences` | int | total group rows (incl. NaN dir) |
| `valid_n` | int | rows with non-null `primary_dir` |
| `up_pct` `down_pct` | float (1dp) | directional split |
| `dominant_dir` | str | `UP`/`DOWN` (ties → `UP`) |
| `confidence` | float (1dp) | `max(up_pct, down_pct)` (**not** significance) |
| `avg_move_1H` `avg_move_4H` | float (4dp) | avg signed move |
| `avg_move_8H` | float (4dp)\|None | None if column absent |
| `near_level_n` | int | count of non-null `near_level` |
| `{surprise}_n` | int | beat/miss/inline sample counts |
| `{surprise}_dir` | str\|None | per-surprise dominant dir |
| `{surprise}_confidence` | float\|None | per-surprise confidence |
| `{surprise}_avg_4H` | float\|None | per-surprise avg 4H move |
| `confluence_n` | int | near-level rows |
| `confluence_dir` | str\|None | confluence dominant dir |
| `confluence_confidence` | float\|None | confluence confidence |
| `confluence_avg_4H` | float\|None | confluence avg 4H move |

### `trader_scores.parquet` — from `score_traders.py`
One row per trader (sorted `skill_score` desc).

| Column | Type | Meaning |
|---|---|---|
| `trader` | str | name |
| `event_trades` | int | trades matched to events |
| `event_correct` `event_wrong` | int | correct/wrong-cluster counts |
| `event_accuracy` | float (4dp) | `correct/n_events` |
| `event_win_rate` | float\|None | win rate on event trades |
| `crowd_follow_rate` | float (4dp) | `n_wrong/n_events` (**crowd = wrong by model assumption**) |
| `total_trades` | int | all trades |
| `avg_rr` | float (3dp)\|None | avg R:R |
| `sl_logic_rate` | float (3dp)\|None | fraction of SLs in sane 0.05%–5% band |
| `avg_dur_mins` | float (1dp)\|None | avg hold time |
| `overall_wr` | float (4dp)\|None | overall win rate |
| `competitions` | numeric | distinct competition count (max of two sources) |
| `score_event` `score_winrate` `score_risk` `score_consistency` | float (1dp) | 0-100 components |
| `skill_score` | float (1dp) | weighted 0-100 (0.35/0.25/0.20/0.20) |
| `franchise_trader` | bool | `skill_score>=65 AND competitions>=2` |
| `is_contrarian` | bool | `crowd_follow_rate < 0.3` |
| `rank` | int | 1-based by `skill_score` desc |

### Candle parquets — `candle_data/<TF>/<symbol>.parquet`
`TF ∈ {M15, H1, H4, D1}`; 51 resolved symbols each. Columns: `time` (datetime64), `symbol` (str canonical), `open`/`high`/`low`/`close` (float64), `volume` (int64, tick_volume), `spread` (int64). Filename transform: `/` and `.` → `_`.

---

## How to Run

### Canonical full run (cold start / new machine)

Run from `c:\Users\Administrator\Documents\Rebel Funding`. Order matters — later stages read earlier artifacts.

1. **Scrape trader files** (only if you need fresh/new `.xlsx`): authenticate Chrome to rebelsfunding.com, ensure matching chromedriver on PATH, then
   ```
   C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe rebels_funding_scraper.py
   ```
   (Idempotent; only adds ranks above the current max per competition.)
2. **Candles + master trade build** — open and log into the MT5 terminal first, then:
   ```
   download_candles.bat
   ```
   (Runs `download_candles.py` then `build_master_trades.py`.)
3. **News calendar:**
   ```
   update_news.bat
   ```
   (Runs `scrape_news_calendar.py`; internet required; re-run if blocked.)
4. **Full intelligence pipeline:**
   ```
   run_all.bat
   ```
   (Runs build → analyze_events → cluster_traders → macro_playbook → score_traders → generate_strategies → monthly_report. **No internal error checking** — if step 2/3 produced stale data, fix it before this step.)

Outputs to verify after a full run: `master_trades.parquet`, `event_reactions.parquet`, `event_playbook.parquet`, `trader_clusters.parquet`, `macro_playbook.parquet`, `trader_scores.parquet`, `strategy_cards.json` + `strategy_cards.html`, `reports/report_YYYY-MM.html`.

### Monthly update (new competition arrives, ~150 new traders)

1. Run `rebels_funding_scraper.py` to pull the new month's trader `.xlsx` into `trader_trades/`.
2. (Optional, with MT5 open) `download_candles.bat` to extend candles incrementally.
3. `update_news.bat` to extend the news calendar (resumes last ~2 months).
4. `run_all.bat` to rebuild all intelligence artifacts and emit the new monthly report.
   - To regenerate a specific month's report: `...\python.exe monthly_report.py 2026-05`.

### Incremental / resume behavior

- **Trader files (`rebels_funding_scraper.py`):** resumes per competition from `max(existing rank)+1`. A failed *middle* rank is NOT retried.
- **Master trades (`build_master_trades.py`):** tracks done competitions in **`processed_competitions.txt`**; only new competitions are appended. **Delete `processed_competitions.txt` to force a full rebuild.** Caution: deleting `master_trades.parquet` but NOT the txt yields an empty re-run.
- **Candles (`download_candles.py`):** per `(TF, symbol)`, fetches only the delta since the last stored candle; boundary candle re-fetched and deduped.
- **News (`scrape_news_calendar.py`):** re-scrapes only the last ~2 months and merges (dedup on `datetime,currency,event`).
- **`analyze_events.py`, `cluster_traders.py`, `macro_playbook.py`, `score_traders.py`, `generate_strategies.py`:** **NO incremental mode** — they recompute from scratch and overwrite their parquet each run.

---

## Key Conventions & Design Decisions

- **Newest-first processing.** `master_trades` is sorted newest competition first; the scraper deliberately skips the "latest" live competition (id 1336 / June 2026). Monthly reports default to the lexical-max `competition` string (works because months are zero-padded `YYYY-MM`).
- **Parquet over CSV.** All intermediate/analytics artifacts are parquet (typed, compact, fast). CSVs only exist as raw exports (`leaderboard_full.csv`, `symbol_map.csv`).
- **Symbol-name normalization.** Candle filenames replace BOTH `/` and `.` with `_` (`XAU/USD`→`XAU_USD`, `USTEC.v`→`USTEC_v`); `analyze_events.py` uses the identical transform. **But trade↔event matching in `cluster_traders.py` uses exact string equality on the canonical/broker symbol** — if `master_trades` uses `XAU/USD` and `event_reactions` uses a differently-styled name, matches go to zero. Keep symbol styling aligned.
- **"Correct vs Wrong" cluster definition.** A trade is **CORRECT** if its `direction` matches the event's `primary_dir` — i.e. the direction price actually moved over the **4H** window after the event (`BUY` if `UP`, `SELL` if `DOWN`). Otherwise **WRONG**. Trades matched within `event_dt − 1h … event_dt + 4h` on `opened`.
- **Confluence = news + price at a key level.** A reaction is "confluence" when `near_level` is non-null — price was within 0.5% of the previous-day high/low or within 0.3% of a round number at the event. These are the highest-conviction setups.
- **Crowd = wrong, by model assumption.** `score_traders.py` defines `crowd_follow_rate = n_wrong/n_events` — the majority is treated as wrong by definition. This is editorial, not data-derived; revisit if it proves false on data.
- **Franchise trader** = `skill_score >= 65 AND competitions >= 2` (consistent skill, not one lucky month).
- **Contrarian trader** = `crowd_follow_rate < 0.3` (rarely on the crowd/wrong side).
- **Confidence is consistency, not significance.** `confidence = max(up_pct, down_pct)`; small samples (as few as `valid_n=4`) can show high "confidence." Treat low-`occurrences` patterns skeptically.
- **Threshold drift between stages is intentional but easy to trip on.** `macro_playbook.py` keeps patterns at occurrences≥4 / confidence≥60; `generate_strategies.py` is stricter (occurrences≥5 / confidence≥65) so it shows a subset.

---

## Known Limitations & Gotchas

- **M15 candle history starts ~Dec 2025** (`START_DATE_M15=2025-12-01`) — no intraday M15 for 2023–2024. H1/H4/D1 go back to 2023-08-01.
- **DE30.v (DAX) is unavailable.** It "resolves" to `GER30` in `symbol_map.csv` but no DE30 parquet exists in any timeframe — treat as missing downstream.
- **~70 instruments, not 71.** `SYMBOL_VARIANTS` lists 71 canonical names but only **51 resolved** to broker symbols and have candle data; 20 did not resolve at all. Events on unresolved symbols silently produce no reactions.
- **No external order-flow / sentiment data.** The only "crowd" signal we have is our own population of competition traders. There is no broker book, no positioning data, no third-party sentiment.
- **Trader entries lack the chart context they actually saw.** We *approximate* their decision context with candles + computed S/R (PDH/PDL/round) + the news calendar. We do not know what indicators/timeframe/news feed each trader looked at.
- **Reactions are not pips and not slippage-adjusted.** `move_*_pct` is a simple close-to-close percent change measured at H1 candle boundaries (the "pip" naming in code is misleading). Round-number S/R logic is crude and **wrong for sub-1.0 FX prices** (e.g. EUR/USD).
- **Surprise labels are not unit-normalized.** `beat/miss/inline` strips non-numeric chars; `%` vs absolute and K/M/B suffixes are not reconciled, so `surprise` can be wrong for suffixed values.
- **ForexFactory blocks scraping** — we use the **Investing.com** private AJAX endpoint instead, which is anti-bot-prone (may return no data / block; just re-run). Parsing depends on undocumented Investing.com DOM attributes (`data-img_key`, `data-event-datetime`) that can silently break.
- **FMP free tier blocks historical economic calendar** — hence the Investing.com scraper. `config.py`'s FMP key is used by upstream fetch scripts, not the documented analytics chain.
- **Pipeline has no error gating.** `run_all.bat` runs all stages regardless of failures; a broken early stage means later stages silently run on stale/missing parquet. Verify each artifact's mtime/row count after a run.
- **HTML reports are unescaped f-strings** — an event/trader/symbol name containing `<` or `&` can corrupt `strategy_cards.html` / monthly reports.

---

## Roadmap / Open Tasks

The five core deliverables (the "why" of the project):

1. **Identify genuinely skilled traders** (vs lucky) — `trader_scores.parquet` (`skill_score`, `franchise_trader`). *Next:* validate skill persistence across months (does a high score in month N predict month N+1?).
2. **Discover exploited macro+technical conditions** — `macro_playbook.parquet`, confluence flags. *Next:* expand confluence beyond PDH/PDL/round to swing highs/lows and session levels.
3. **Codify edge into a strategy playbook** — `strategy_cards.json` / `.html`. *Next:* attach explicit entry/SL/TP rules and a confidence/sample-size caveat to each card.
4. **Help the prop firm decide who to fund** — franchise/contrarian flags + monthly reports. *Next:* a fundability scorecard combining skill persistence + risk discipline.
5. **Help investors find historical-edge setups** — monthly reports + strategy cards. *Next:* per-setup expectancy and drawdown stats.

Natural extensions:

- **ML classifier on enriched features** — predict `cluster ∈ {CORRECT, WRONG}` (or trader skill) from event type, surprise, near_level, time-of-day, R:R, entry timing. Replaces hand-tuned scoring weights.
- **Intraday S/R auto-detection** — proper swing-point / pivot / volume-profile levels (now that M15 exists from Dec 2025) to fix the crude round-number logic.
- **Backtest engine for strategy cards** — replay each card's rules against candle history to compute realistic expectancy, win rate, max drawdown (with spread/slippage).
- **Live signal generator** — on a fresh High-impact event, look up the matching macro-playbook pattern + confluence and emit a real-time signal with the historical edge attached.
- **Fix incremental modes** — `analyze_events` / `cluster_traders` recompute everything each run; make them append-only for speed as data grows.
- **Add error gating** to `run_all.bat` (`if errorlevel`) so a failed stage halts the pipeline; remove the trailing `pause` for scheduled runs.

---

## Project Phase 2: Trading Engine & Backtest Results (June 2026)

### What Was Tried

Three trading engines were built and backtested against historical data (Jul 2025 – May 2026):

#### Engine A — Bernd D1 Zone Strategy (`engine/bernd_strategy.py`, `engine/backtest.py`)
- **Method**: Supply/demand zone detection on D1 candles + COT simulation + seasonality + location gate + Bernd's bias hierarchy
- **Entry**: At daily demand/supply zones when location = cheap/expensive
- **SL/TP**: Zone-based (distal/proximal lines), wide stops (2-5%)
- **Result**: **13 trades, 54% WR, +27.1% total P&L, +2.09% expectancy per trade**
- **Best symbol**: XAU/USD (6 trades, 67% WR, +23.4%)
- **Issue**: Too few trades for challenge (13 in 11 months), drawdown -36.5% exceeds 10% limit due to wide zone-based SL
- **Verdict**: Directional edge confirmed, but swing-style sizing incompatible with prop firm 5%/10% rules

#### Engine B — Combined Bernd Bias + Rebel Entry (`engine/combined_engine.py`, `engine/backtest_combined.py`)
- **Method**: Daily bias from Bernd (COT + seasonality + rebel direction consensus) filtered intraday rebel winner setup rules
- **Entry**: Only when daily bias direction == rebel setup direction AND current session/trend/S_R match
- **SL/TP**: Fixed 0.5% SL, 1:2 R:R
- **Result**: **7 trades, 0% WR, -7R total**
- **Issue**: Over-constrained — requiring both bias AND setup match eliminated nearly all signals. Only 1 trade per symbol in 10 months.
- **Verdict**: Gating too aggressively kills signal count without improving quality

#### Engine C — 5-Dimension Consensus (`engine/challenge_engine.py`)
- **Method**: Equal-vote consensus across technical, COT, news, seasonality, rebel_winners
- **Entry**: When 2+ dimensions agree on direction
- **Result**: Generates real-time signals. XAU/USD SELL with 3/5 agreement on Jun 9 2026.
- **Verdict**: Works for live scanning but not backtested on historical data

### Key Backtest Findings

| Finding | Detail |
|---|---|
| **4/4 consensus = 67% WR** | When location + trend + COT + seasonality all agree, win rate jumps to 67% vs 43% for 3/4 |
| **XAU/USD is the best instrument** | 67% WR vs XAG/USD 43% WR |
| **All LONG trades** | System never generated a SHORT signal — supply zone detection on D1 too weak |
| **FX pairs dead** | EUR/USD, GBP/USD, USD/JPY, AUD/USD, USD/CAD generated ZERO trades on D1 |
| **D1 is too slow for challenges** | 1-2 trades/month won't pass a 30-day challenge |
| **0.5% SL is viable** | Drawdown stays within 10% limit with tight SL |

### What Worked

- **Rebel winner pattern mining**: 124 setup rules from 118,606 trades (setup_miner.py → setup_rules.json)
- **COT simulation**: 5,220 weekly records for 10 instruments via yfinance (cot_simulated.parquet)
- **Seasonality**: 120 month-symbol patterns from 10yr data (seasonality.parquet)
- **Event playbook**: 65 events with crowd-vs-smart breakdown (event_playbook.parquet)
- **Trader scoring**: Skill scores + franchise/contrarian flags (trader_scores.parquet)
- **Strategy cards**: 124 tradeable rules in HTML/JSON (strategy_cards.html/json)
- **Monthly report**: Auto-generated HTML for May 2026 (reports/report_2026-05.html)

### What Didn't Work

- **BLS news dates**: BLS timestamps data to month-start (observation dates), not actual release Fridays → NFP/CPI events show 0.000% moves → useless for event matching
- **CFTC COT**: Both cftc.gov file downloads AND SODA API blocked on this network → simulated COT used instead (directionally useful but not real)
- **ECB API**: Returned 0 events (endpoint may have changed)
- **FRED**: Network unreachable
- **Investing.com scraper**: Rate-limited after ~7 months (429 errors), IP-flagged
- **DE30.v (DAX)**: Not available on broker
- **M15 candles**: Only from Dec 2025, not Aug 2023

### Where to Go Next

1. **Rebel-only intraday backtest**: Skip the Bernd bias filter. Use rebel winner setup rules directly (124 patterns) with 0.5% SL and 1:2 R:R. Backtest against H1 data to get 50+ trades/month.
2. **Fix BLS dates**: Scrape `bls.gov/schedule` for actual release dates → NFP/CPI events will match thousands of trades → event_rules.json grows from 3 to 100+ rules
3. **Add session-time filters**: Only trade London and NY sessions when spreads are tightest
4. **Max 2 concurrent trades**: With 5+ signals per day, enforce max positions to prevent correlated losses
5. **M15 entries**: Use M15 candles for precise entry timing once signal fires on H1 bias

### Files Created (engine/)

| File | Purpose | Status |
|---|---|---|
| `market_state.py` | Live MT5 market readings (S/R, trend, session) | ✅ |
| `setup_miner.py` | Clusters 118K trades into 124 setup rules | ✅ |
| `event_miner.py` | Event-based rule mining (3 rules) | ✅ |
| `enricher.py` | One-time trade context enrichment | ✅ |
| `seasonality_cot.py` | 10yr seasonality + COT simulation | ✅ |
| `cot_data.py` | CFTC COT download (blocked) | ❌ |
| `bernd_strategy.py` | Bernd zone detection + bias hierarchy | ✅ |
| `challenge_engine.py` | 5-dim consensus + risk manager | ✅ |
| `combined_engine.py` | Bernd bias + rebel entry filter | ✅ |
| `backtest.py` | D1 zone backtest (13 trades, 54% WR) | ✅ |
| `backtest_combined.py` | Combined backtest (7 trades, 0% WR) | ✅ |
| `signal_quality.py` | Signal quality analysis report | ✅ |
| `orchestrator.py` | Master controller (mine/scan/watch/live) | ✅ |
| `setup_rules.json` | 124 rebel winner patterns | ✅ |
| `setup_rules.html` | Visual strategy cards | ✅ |
| `event_rules.json` | 3 event-based patterns | ✅ |
| `seasonality.parquet` | 120 month-symbol bias patterns | ✅ |
| `cot_simulated.parquet` | 5,220 weekly COT records | ✅ |
| `enriched_trades.parquet` | Cached trade enrichment | ✅ |
| `backtest_results.parquet` | D1 backtest trades | ✅ |
| `backtest_combined.parquet` | Combined backtest trades | ✅ |

---

- **Crowd** — the majority of competition traders on a given event/symbol. In our model the crowd's direction is treated as the **wrong** side by definition (`crowd_follow_rate = n_wrong/n_events`).
- **Smart money / smart minority** — traders on the **CORRECT** side of the actual 4H price reaction; the population we mine for repeatable edge.
- **Confluence** — a setup where a **macro event** coincides with price sitting **at a key technical level** (`near_level` non-null: within 0.5% of PDH/PDL or 0.3% of a round number). The highest-conviction class of setup.
- **Franchise trader** — `skill_score >= 65` across **2+ competitions**; consistently skilled, not a one-month fluke.
- **Contrarian trader** — `crowd_follow_rate < 0.3`; rarely trades the crowd/wrong direction.
- **Surprise** — comparison of an event's `actual` vs `forecast`: **beat** (better than forecast), **miss** (worse), **inline** (matches), or `''` (unparseable).
- **primary_dir** — the event's reference direction = the **4H** post-event price move (`UP` / `DOWN`). The yardstick for CORRECT vs WRONG.
- **PDH / PDL** — Previous-Day High / Previous-Day Low; the primary intraday support/resistance levels used for confluence (computed from the prior calendar day's H1 candles).
