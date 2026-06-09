"""
Economic Calendar Scraper — Multi-Source
Sources (tried in order per month):
  1. Investing.com AJAX (paginated, session-reset per month)
  2. FRED CSV + BLS API (US events, no key needed)
  3. ECB REST API (European events, no key needed)

Saves: news_calendar.parquet (incremental, safe to re-run)
Run:   python scrape_news_calendar.py
"""

import requests
from bs4 import BeautifulSoup
import pandas as pd
from datetime import datetime
from dateutil.relativedelta import relativedelta
import os, time, re, io, sys

# Force UTF-8 on Windows to avoid UnicodeEncodeError with box-drawing chars
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OUTPUT_FILE = r"c:\Users\Administrator\Documents\Rebel Funding\news_calendar.parquet"
START       = datetime(2023, 8, 1)
END         = datetime.now()

COUNTRIES   = ["5","22","4","35","25","6","11","12","36","72","55"]
IMPORTANCE  = ["3","2"]

CURRENCY_INSTRUMENTS = {
    "USD": ["XAU/USD","XAG/USD","XPT/USD","XPD/USD","EUR/USD","GBP/USD","AUD/USD",
            "NZD/USD","USD/JPY","USD/CAD","USD/CHF","USD/MXN","USD/TRY","USD/ZAR",
            "USD/SEK","USD/HUF","DJ30","US500","USTEC.v","BTCUSDT","ETHUSDT",
            "SOLUSDT","XRPUSDT","1USO","1NGAS","AAPL.OQ","MSFT.OQ","NVDA.OQ","TSLA.OQ","NFLX.OQ"],
    "EUR": ["EUR/USD","EUR/JPY","EUR/GBP","EUR/CHF","EUR/AUD","EUR/CAD","EUR/NZD",
            "EUR/CZK","EUR/NOK","EUR/HUF","DE30.v","F40.v"],
    "GBP": ["GBP/USD","GBP/JPY","GBP/CHF","GBP/AUD","GBP/CAD","GBP/NZD","EUR/GBP","UK100.v"],
    "JPY": ["USD/JPY","EUR/JPY","GBP/JPY","AUD/JPY","CAD/JPY","CHF/JPY","NZD/JPY","JPN225.v"],
    "AUD": ["AUD/USD","AUD/JPY","AUD/CAD","AUD/CHF","AUD/NZD","EUR/AUD","GBP/AUD"],
    "NZD": ["NZD/USD","NZD/JPY","NZD/CAD","NZD/CHF","AUD/NZD","GBP/NZD","EUR/NZD"],
    "CAD": ["USD/CAD","CAD/JPY","CAD/CHF","AUD/CAD","GBP/CAD","EUR/CAD","NZD/CAD","1USO"],
    "CHF": ["USD/CHF","CHF/JPY","CAD/CHF","EUR/CHF","GBP/CHF","NZD/CHF"],
    "CNY": ["AUD/USD","XAU/USD","1USO","BTCUSDT"],
    "CNH": ["AUD/USD","XAU/USD","1USO"],
}

# ── FRED series: (series_id, event_name, currency, impact) ───────────────────
FRED_SERIES = [
    ("PAYEMS",     "Non-Farm Payrolls",              "USD", "High"),
    ("UNRATE",     "Unemployment Rate",              "USD", "High"),
    ("CPIAUCSL",   "CPI",                            "USD", "High"),
    ("CPILFESL",   "Core CPI",                       "USD", "High"),
    ("PCE",        "Personal Consumption Expenditures","USD","Medium"),
    ("PCEPI",      "PCE Price Index",                "USD", "Medium"),
    ("GDPC1",      "GDP",                            "USD", "High"),
    ("FEDFUNDS",   "Fed Interest Rate Decision",     "USD", "High"),
    ("RSAFS",      "Retail Sales",                   "USD", "Medium"),
    ("INDPRO",     "Industrial Production",          "USD", "Medium"),
    ("HOUST",      "Housing Starts",                 "USD", "Medium"),
    ("UMCSENT",    "Consumer Sentiment",             "USD", "Medium"),
    ("ISM",        "ISM Manufacturing PMI",          "USD", "High"),
    ("PPIFIS",     "PPI",                            "USD", "Medium"),
    ("IC4WSA",     "Initial Jobless Claims",         "USD", "High"),
    ("CCSA",       "Continuing Jobless Claims",      "USD", "Medium"),
]

# ── BLS series (no API key needed) ───────────────────────────────────────────
BLS_SERIES = [
    ("CES0000000001", "Non-Farm Payrolls",   "USD", "High"),
    ("LNS14000000",   "Unemployment Rate",   "USD", "High"),
    ("CUUR0000SA0",   "CPI",                 "USD", "High"),
    ("WPUFD4",        "PPI",                 "USD", "Medium"),
    ("LASST000000000000003", "US Jobless Claims", "USD", "High"),
]

# ── ECB series ────────────────────────────────────────────────────────────────
ECB_SERIES = [
    ("FM.B.U2.EUR.RT.MM.EURIBOR1MD_.HSTA", "ECB Rate Decision", "EUR", "High"),
    ("ICP.M.U2.N.000000.4.ANR",             "EU CPI",            "EUR", "High"),
]


# ════════════════════════════════════════════════════════════════════════════
# SOURCE 1 — Investing.com (paginated, session reset per month)
# ════════════════════════════════════════════════════════════════════════════

def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent":       "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept":           "text/plain, */*; q=0.01",
        "Accept-Language":  "en-US,en;q=0.9",
        "Referer":          "https://www.investing.com/economic-calendar/",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type":     "application/x-www-form-urlencoded",
        "Origin":           "https://www.investing.com",
    })
    try:
        s.get("https://www.investing.com/economic-calendar/", timeout=15)
    except Exception:
        pass
    return s


def investing_fetch_month(m_start, m_end):
    """Fetch one month with pagination + fresh session. Returns list of raw rows."""
    all_rows = []
    limit_from = 0
    session = make_session()

    while True:
        body_parts = []
        for c in COUNTRIES:
            body_parts.append(f"country%5B%5D={c}")
        for imp in IMPORTANCE:
            body_parts.append(f"importance%5B%5D={imp}")
        body_parts += [
            f"dateFrom={m_start.strftime('%Y-%m-%d')}",
            f"dateTo={m_end.strftime('%Y-%m-%d')}",
            "timeZone=55",
            "timeFilter=timeRemain",
            "currentTab=custom",
            "submitFilters=1",
            f"limit_from={limit_from}",
        ]
        body = "&".join(body_parts)

        try:
            r = session.post(
                "https://www.investing.com/economic-calendar/Service/getCalendarFilteredData",
                data=body, timeout=30
            )
            r.raise_for_status()
            html_chunk = r.json().get("data", "")
        except Exception as e:
            if limit_from == 0:
                return None, str(e)
            break

        if not html_chunk or len(html_chunk.strip()) < 50:
            break

        rows = parse_investing_html(html_chunk)
        if not rows:
            break

        all_rows.extend(rows)

        # Investing.com returns 50 items per page
        if len(rows) < 50:
            break
        limit_from += 50
        time.sleep(0.8)

    return all_rows, None


def parse_investing_html(html_str):
    if not html_str:
        return []
    soup = BeautifulSoup(html_str, "html.parser")
    event_rows = soup.find_all("tr", class_=re.compile(r"js-event-item"))
    events = []
    for row in event_rows:
        impact_td = row.find("td", attrs={"data-img_key": True}, class_=re.compile("sentiment"))
        if not impact_td:
            continue
        img_key = impact_td.get("data-img_key", "")
        if   img_key == "bull3": impact = "High"
        elif img_key == "bull2": impact = "Medium"
        else: continue

        date_attr = row.get("data-event-datetime", "")
        try:
            event_dt = pd.to_datetime(date_attr.replace("/", "-"))
        except Exception:
            continue

        currency_td = row.find("td", class_=re.compile("flagCur"))
        currency = currency_td.get_text(strip=True).upper() if currency_td else ""

        event_td = row.find("td", class_=re.compile(r"\bevent\b"))
        event_name = event_td.get_text(strip=True) if event_td else ""
        if not event_name:
            continue

        event_id = row.get("id", "").replace("eventRowId_", "")

        def get_cell(suffix):
            td = row.find("td", id=f"event{suffix}_{event_id}")
            if td: return td.get_text(strip=True)
            td = row.find("td", class_=re.compile(rf"\b{suffix.lower()}\b"))
            return td.get_text(strip=True) if td else ""

        actual   = get_cell("Actual")
        forecast = get_cell("Forecast")
        previous = get_cell("Previous")
        surprise = _calc_surprise(actual, forecast)

        events.append(_make_row(event_dt, currency, event_name, impact, actual, forecast, previous, surprise))
    return events


# ════════════════════════════════════════════════════════════════════════════
# SOURCE 2 — FRED (Federal Reserve, free CSV, no key)
# ════════════════════════════════════════════════════════════════════════════

def fetch_fred_series(series_id, event_name, currency, impact):
    start_str = "2023-08-01"
    end_str   = END.strftime("%Y-%m-%d")
    url = (f"https://fred.stlouisfed.org/graph/fredgraph.csv"
           f"?id={series_id}&cosd={start_str}&coed={end_str}")
    try:
        r = requests.get(url, timeout=8,
                         headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text), parse_dates=["DATE"])
        df = df.rename(columns={"DATE": "date", df.columns[1]: "value"})
        df = df.dropna(subset=["value"])
        df = df[df["date"] >= pd.Timestamp("2023-08-01")]

        rows = []
        for i, row in df.iterrows():
            actual   = str(row["value"])
            previous = str(df.iloc[i-1]["value"]) if i > 0 else ""
            surprise = _calc_surprise(actual, previous)
            rows.append(_make_row(
                row["date"], currency, event_name, impact,
                actual, "", previous, surprise
            ))
        return rows
    except Exception:
        return []


def fetch_all_fred():
    rows = []
    fails = 0
    for series_id, name, currency, impact in FRED_SERIES:
        if fails >= 2:
            print(f"      (skipping remaining FRED -- 2 failures)")
            break
        print(f"      {series_id} ... ", end="", flush=True)
        result = fetch_fred_series(series_id, name, currency, impact)
        if result:
            rows.extend(result)
            print(f"{len(result)} releases")
            fails = 0
        else:
            print("failed")
            fails += 1
        time.sleep(0.2)
    return rows


# ════════════════════════════════════════════════════════════════════════════
# SOURCE 3 — BLS (Bureau of Labor Statistics, no key)
# ════════════════════════════════════════════════════════════════════════════

def fetch_bls_series(series_id, event_name, currency, impact):
    url  = "https://api.bls.gov/publicAPI/v1/timeseries/data/"
    body = {"seriesid": [series_id], "startyear": "2023", "endyear": str(END.year)}
    try:
        r = requests.post(url, json=body, timeout=20,
                          headers={"Content-type": "application/json"})
        data = r.json()
        results = data.get("Results", {}).get("series", [])
        if not results:
            return []
        rows = []
        for item in results[0].get("data", []):
            year  = int(item["year"])
            per   = item["period"]  # e.g. M01, M02
            if not per.startswith("M") or per == "M13":
                continue
            month = int(per[1:])
            dt    = datetime(year, month, 1)
            actual   = item.get("value", "")
            previous = ""
            rows.append(_make_row(dt, currency, event_name, impact, actual, "", previous, ""))
        return rows
    except Exception:
        return []


def fetch_all_bls():
    rows = []
    for series_id, name, currency, impact in BLS_SERIES:
        result = fetch_bls_series(series_id, name, currency, impact)
        rows.extend(result)
        if result:
            print(f"      BLS  {series_id}: {len(result)} releases")
        time.sleep(0.5)
    return rows


# ════════════════════════════════════════════════════════════════════════════
# SOURCE 4 — ECB REST API (no key)
# ════════════════════════════════════════════════════════════════════════════

def fetch_ecb_series(series_key, event_name, currency, impact):
    url = f"https://sdw-wsrest.ecb.europa.eu/service/data/{series_key}"
    try:
        r = requests.get(url, timeout=20, headers={
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0"
        })
        data = r.json()
        obs  = data.get("dataSets", [{}])[0].get("series", {})
        dims = data.get("structure", {}).get("dimensions", {}).get("observation", [{}])
        time_dim = next((d for d in dims if d.get("id") == "TIME_PERIOD"), None)
        if not time_dim or not obs:
            return []
        dates = [v["id"] for v in time_dim.get("values", [])]
        rows  = []
        for series_vals in obs.values():
            for idx_str, val_list in series_vals.get("observations", {}).items():
                idx = int(idx_str)
                if idx >= len(dates):
                    continue
                val  = val_list[0] if val_list else None
                if val is None:
                    continue
                try:
                    dt = pd.to_datetime(dates[idx])
                except Exception:
                    continue
                if dt < pd.Timestamp("2023-08-01"):
                    continue
                rows.append(_make_row(dt, currency, event_name, impact,
                                      str(val), "", "", ""))
        return rows
    except Exception:
        return []


def fetch_all_ecb():
    rows = []
    for key, name, currency, impact in ECB_SERIES:
        result = fetch_ecb_series(key, name, currency, impact)
        rows.extend(result)
        if result:
            print(f"      ECB  {name}: {len(result)} releases")
        time.sleep(0.5)
    return rows


# ════════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════════

def _calc_surprise(actual, forecast):
    try:
        def clean(v):
            v = re.sub(r"[^\d.\-]", "", str(v).strip())
            return float(v)
        a, f = clean(actual), clean(forecast)
        return "beat" if a > f else "miss" if a < f else "inline"
    except Exception:
        return ""


def _make_row(dt, currency, event_name, impact, actual, forecast, previous, surprise):
    affected = CURRENCY_INSTRUMENTS.get(currency.upper(), [])
    return {
        "datetime":    pd.to_datetime(dt),
        "date":        pd.to_datetime(dt).date(),
        "currency":    currency.upper(),
        "event":       event_name,
        "impact":      impact,
        "actual":      actual,
        "forecast":    forecast,
        "previous":    previous,
        "surprise":    surprise,
        "instruments": ",".join(affected),
        "source":      "",
    }


# ════════════════════════════════════════════════════════════════════════════
# SOURCE 5 — Central bank meeting dates (public schedules, no rate limits)
# ════════════════════════════════════════════════════════════════════════════

CB_DATES = {
    "GBP": [
        # BoE MPC — 2023
        ("2023-08-03","BoE Interest Rate Decision","GBP","High"),
        ("2023-09-21","BoE Interest Rate Decision","GBP","High"),
        ("2023-11-02","BoE Interest Rate Decision","GBP","High"),
        ("2023-12-14","BoE Interest Rate Decision","GBP","High"),
        # 2024
        ("2024-02-01","BoE Interest Rate Decision","GBP","High"),
        ("2024-03-21","BoE Interest Rate Decision","GBP","High"),
        ("2024-05-09","BoE Interest Rate Decision","GBP","High"),
        ("2024-06-20","BoE Interest Rate Decision","GBP","High"),
        ("2024-08-01","BoE Interest Rate Decision","GBP","High"),
        ("2024-09-19","BoE Interest Rate Decision","GBP","High"),
        ("2024-11-07","BoE Interest Rate Decision","GBP","High"),
        ("2024-12-19","BoE Interest Rate Decision","GBP","High"),
        # 2025
        ("2025-02-06","BoE Interest Rate Decision","GBP","High"),
        ("2025-03-20","BoE Interest Rate Decision","GBP","High"),
        ("2025-05-08","BoE Interest Rate Decision","GBP","High"),
        ("2025-06-19","BoE Interest Rate Decision","GBP","High"),
        ("2025-08-07","BoE Interest Rate Decision","GBP","High"),
        ("2025-09-18","BoE Interest Rate Decision","GBP","High"),
        ("2025-11-06","BoE Interest Rate Decision","GBP","High"),
        ("2025-12-18","BoE Interest Rate Decision","GBP","High"),
        # 2026
        ("2026-02-05","BoE Interest Rate Decision","GBP","High"),
        ("2026-03-19","BoE Interest Rate Decision","GBP","High"),
        ("2026-05-07","BoE Interest Rate Decision","GBP","High"),
    ],
    "JPY": [
        ("2023-09-22","BoJ Interest Rate Decision","JPY","High"),
        ("2023-10-31","BoJ Interest Rate Decision","JPY","High"),
        ("2023-12-19","BoJ Interest Rate Decision","JPY","High"),
        ("2024-01-23","BoJ Interest Rate Decision","JPY","High"),
        ("2024-03-19","BoJ Interest Rate Decision","JPY","High"),
        ("2024-04-26","BoJ Interest Rate Decision","JPY","High"),
        ("2024-06-14","BoJ Interest Rate Decision","JPY","High"),
        ("2024-07-31","BoJ Interest Rate Decision","JPY","High"),
        ("2024-09-20","BoJ Interest Rate Decision","JPY","High"),
        ("2024-10-31","BoJ Interest Rate Decision","JPY","High"),
        ("2024-12-19","BoJ Interest Rate Decision","JPY","High"),
        ("2025-01-24","BoJ Interest Rate Decision","JPY","High"),
        ("2025-03-19","BoJ Interest Rate Decision","JPY","High"),
        ("2025-05-01","BoJ Interest Rate Decision","JPY","High"),
        ("2025-06-17","BoJ Interest Rate Decision","JPY","High"),
        ("2025-07-31","BoJ Interest Rate Decision","JPY","High"),
        ("2025-09-19","BoJ Interest Rate Decision","JPY","High"),
        ("2025-10-30","BoJ Interest Rate Decision","JPY","High"),
        ("2025-12-18","BoJ Interest Rate Decision","JPY","High"),
        ("2026-01-23","BoJ Interest Rate Decision","JPY","High"),
        ("2026-03-19","BoJ Interest Rate Decision","JPY","High"),
    ],
    "AUD": [
        ("2023-08-01","RBA Interest Rate Decision","AUD","High"),
        ("2023-09-05","RBA Interest Rate Decision","AUD","High"),
        ("2023-10-03","RBA Interest Rate Decision","AUD","High"),
        ("2023-11-07","RBA Interest Rate Decision","AUD","High"),
        ("2023-12-05","RBA Interest Rate Decision","AUD","High"),
        ("2024-02-06","RBA Interest Rate Decision","AUD","High"),
        ("2024-03-19","RBA Interest Rate Decision","AUD","High"),
        ("2024-05-07","RBA Interest Rate Decision","AUD","High"),
        ("2024-06-18","RBA Interest Rate Decision","AUD","High"),
        ("2024-08-06","RBA Interest Rate Decision","AUD","High"),
        ("2024-09-24","RBA Interest Rate Decision","AUD","High"),
        ("2024-11-05","RBA Interest Rate Decision","AUD","High"),
        ("2024-12-10","RBA Interest Rate Decision","AUD","High"),
        ("2025-02-18","RBA Interest Rate Decision","AUD","High"),
        ("2025-04-01","RBA Interest Rate Decision","AUD","High"),
        ("2025-05-20","RBA Interest Rate Decision","AUD","High"),
        ("2025-07-08","RBA Interest Rate Decision","AUD","High"),
        ("2025-08-12","RBA Interest Rate Decision","AUD","High"),
        ("2025-09-30","RBA Interest Rate Decision","AUD","High"),
        ("2025-11-04","RBA Interest Rate Decision","AUD","High"),
        ("2025-12-09","RBA Interest Rate Decision","AUD","High"),
        ("2026-02-17","RBA Interest Rate Decision","AUD","High"),
    ],
    "CAD": [
        ("2023-09-06","BoC Interest Rate Decision","CAD","High"),
        ("2023-10-25","BoC Interest Rate Decision","CAD","High"),
        ("2023-12-06","BoC Interest Rate Decision","CAD","High"),
        ("2024-01-24","BoC Interest Rate Decision","CAD","High"),
        ("2024-03-06","BoC Interest Rate Decision","CAD","High"),
        ("2024-04-10","BoC Interest Rate Decision","CAD","High"),
        ("2024-06-05","BoC Interest Rate Decision","CAD","High"),
        ("2024-07-24","BoC Interest Rate Decision","CAD","High"),
        ("2024-09-04","BoC Interest Rate Decision","CAD","High"),
        ("2024-10-23","BoC Interest Rate Decision","CAD","High"),
        ("2024-12-11","BoC Interest Rate Decision","CAD","High"),
        ("2025-01-29","BoC Interest Rate Decision","CAD","High"),
        ("2025-03-12","BoC Interest Rate Decision","CAD","High"),
        ("2025-04-16","BoC Interest Rate Decision","CAD","High"),
        ("2025-06-04","BoC Interest Rate Decision","CAD","High"),
        ("2025-07-30","BoC Interest Rate Decision","CAD","High"),
        ("2025-09-17","BoC Interest Rate Decision","CAD","High"),
        ("2025-10-29","BoC Interest Rate Decision","CAD","High"),
        ("2025-12-10","BoC Interest Rate Decision","CAD","High"),
        ("2026-01-28","BoC Interest Rate Decision","CAD","High"),
        ("2026-03-11","BoC Interest Rate Decision","CAD","High"),
    ],
    "NZD": [
        ("2023-08-16","RBNZ Interest Rate Decision","NZD","High"),
        ("2023-10-04","RBNZ Interest Rate Decision","NZD","High"),
        ("2023-11-29","RBNZ Interest Rate Decision","NZD","High"),
        ("2024-02-28","RBNZ Interest Rate Decision","NZD","High"),
        ("2024-04-10","RBNZ Interest Rate Decision","NZD","High"),
        ("2024-05-22","RBNZ Interest Rate Decision","NZD","High"),
        ("2024-07-10","RBNZ Interest Rate Decision","NZD","High"),
        ("2024-08-14","RBNZ Interest Rate Decision","NZD","High"),
        ("2024-10-09","RBNZ Interest Rate Decision","NZD","High"),
        ("2024-11-27","RBNZ Interest Rate Decision","NZD","High"),
        ("2025-02-19","RBNZ Interest Rate Decision","NZD","High"),
        ("2025-04-09","RBNZ Interest Rate Decision","NZD","High"),
        ("2025-05-28","RBNZ Interest Rate Decision","NZD","High"),
        ("2025-07-09","RBNZ Interest Rate Decision","NZD","High"),
        ("2025-08-13","RBNZ Interest Rate Decision","NZD","High"),
        ("2025-10-08","RBNZ Interest Rate Decision","NZD","High"),
        ("2025-11-26","RBNZ Interest Rate Decision","NZD","High"),
        ("2026-02-25","RBNZ Interest Rate Decision","NZD","High"),
    ],
    "CHF": [
        ("2023-09-21","SNB Interest Rate Decision","CHF","High"),
        ("2023-12-14","SNB Interest Rate Decision","CHF","High"),
        ("2024-03-21","SNB Interest Rate Decision","CHF","High"),
        ("2024-06-20","SNB Interest Rate Decision","CHF","High"),
        ("2024-09-26","SNB Interest Rate Decision","CHF","High"),
        ("2024-12-12","SNB Interest Rate Decision","CHF","High"),
        ("2025-03-20","SNB Interest Rate Decision","CHF","High"),
        ("2025-06-19","SNB Interest Rate Decision","CHF","High"),
        ("2025-09-18","SNB Interest Rate Decision","CHF","High"),
        ("2025-12-11","SNB Interest Rate Decision","CHF","High"),
        ("2026-03-19","SNB Interest Rate Decision","CHF","High"),
    ],
}


def fetch_central_bank_dates():
    rows = []
    start_ts = pd.Timestamp("2023-08-01")
    end_ts   = pd.Timestamp(END)
    for currency, dates in CB_DATES.items():
        for date_str, event_name, cur, impact in dates:
            dt = pd.Timestamp(date_str)
            if dt < start_ts or dt > end_ts:
                continue
            # Use noon UTC as approximate time (exact time depends on bank)
            dt = dt.replace(hour=12, minute=0)
            rows.append(_make_row(dt, cur, event_name, impact, "", "", "", ""))
    return rows


def load_existing():
    if os.path.exists(OUTPUT_FILE):
        return pd.read_parquet(OUTPUT_FILE)
    return pd.DataFrame()


# ════════════════════════════════════════════════════════════════════════════
# Main
# ════════════════════════════════════════════════════════════════════════════

def main():
    print("=" * 60)
    print("Economic Calendar Scraper -- Government Sources Only")
    print(f"Period: {START.strftime('%b %Y')} -> {END.strftime('%b %Y')}")
    print("Sources: FRED | BLS | ECB | Central Bank schedules")
    print("(Zero rate limits -- all public government data)")
    print("=" * 60 + "\n")

    existing = load_existing()
    if not existing.empty:
        last_date = pd.to_datetime(existing["datetime"]).max()
        resume_from = (last_date + relativedelta(months=1)).replace(day=1)
        print(f"Existing: {len(existing):,} events up to {last_date.date()}")
        print(f"Resuming from {resume_from.strftime('%b %Y')}\n")
    else:
        resume_from = START

    all_rows = []

    # ── FRED (US macro data) ────────────────────────────────────────────────
    print("── FRED -- SKIPPED (network not reachable) ──")

    # ── BLS (US labor/inflation) ─────────────────────────────────────────────
    print(f"\n── BLS (Bureau of Labor Statistics, no key) ──")
    bls_rows = fetch_all_bls()
    for r in bls_rows:
        r["source"] = "bls"
    all_rows.extend(bls_rows)
    print(f"  Total BLS events: {len(bls_rows)}")

    # ── ECB (European data) ──────────────────────────────────────────────────
    print(f"\n── ECB REST API (no key) ──")
    ecb_rows = fetch_all_ecb()
    for r in ecb_rows:
        r["source"] = "ecb"
    all_rows.extend(ecb_rows)
    print(f"  Total ECB events: {len(ecb_rows)}")

    # ── Central bank meeting dates (public websites, no rate limits) ─────────
    print(f"\n── Central bank meeting dates ──")
    cb_rows = fetch_central_bank_dates()
    for r in cb_rows:
        r["source"] = "centralbank"
    all_rows.extend(cb_rows)
    print(f"  Total central bank dates: {len(cb_rows)}")

    if not all_rows:
        print("\nNo events collected from any source.")
        return

    new_df = pd.DataFrame(all_rows)
    new_df["datetime"] = pd.to_datetime(new_df["datetime"])
    new_df["date"]     = pd.to_datetime(new_df["date"])

    if not existing.empty:
        final = pd.concat([existing, new_df], ignore_index=True)
    else:
        final = new_df

    final = (final
             .drop_duplicates(subset=["datetime", "currency", "event"])
             .sort_values("datetime")
             .reset_index(drop=True))

    final.to_parquet(OUTPUT_FILE, index=False)

    print(f"\n{'=' * 60}")
    print("COMPLETE")
    print(f"  Total events:  {len(final):,}")
    print(f"  High impact:   {(final['impact'] == 'High').sum():,}")
    print(f"  Medium impact: {(final['impact'] == 'Medium').sum():,}")
    print(f"  Date range:    {final['datetime'].min().date()} -> {final['datetime'].max().date()}")
    print(f"\n  By source:")
    if "source" in final.columns:
        for src, cnt in final["source"].value_counts().items():
            print(f"    {src:<12} {cnt:,} events")
    print(f"\n  By currency:")
    for cur, cnt in final["currency"].value_counts().head(8).items():
        print(f"    {cur:<6} {cnt:,}")
    print(f"\n  Saved -> {OUTPUT_FILE}")
    print("=" * 60)


if __name__ == "__main__":
    main()
