import pandas as pd
import glob, os, re

INPUT_FOLDER = "trader_trades"
EXCLUDE = ["XAU/USD", "XAG/USD"]

def parse_duration_to_seconds(duration_str):
    if pd.isna(duration_str):
        return None
    try:
        parts = str(duration_str).split(':')
        if len(parts) == 2:
            h, m = map(int, parts)
            return h * 3600 + m * 60
        elif len(parts) == 3:
            h, m, s = map(int, parts)
            return h * 3600 + m * 60 + s
    except Exception:
        return None
    return None

def load_file(filepath):
    try:
        df = pd.read_excel(filepath)
    except Exception as e:
        print(f"skip {filepath}: {e}")
        return None
    if df.empty:
        return None

    filename = os.path.basename(filepath)
    name_part = os.path.splitext(filename)[0]
    trader_id = name_part
    if '_R' in name_part:
        parts = name_part.split('_R')
        if len(parts) > 1:
            remainder = '_R'.join(parts[1:])
            match = re.match(r'\d+_(.+)', remainder)
            if match:
                trader_id = match.group(1).strip()

    df.columns = df.columns.str.strip().str.lower()
    rename_map = {
        'symbol': 'symbol', 'p/l': 'profit', 'direction': 'side',
        'opened': 'entry_time', 'closed': 'exit_time',
        'duration': 'duration_str', 'volume': 'volume',
        'order number': 'order_id'
    }
    df.rename(columns=rename_map, inplace=True)
    df['trader_id'] = trader_id

    if 'profit' in df.columns:
        def clean(v):
            if pd.isna(v):
                return None
            try:
                return float(str(v).replace('$', '').replace(',', '').strip())
            except Exception:
                return None
        df['profit'] = df['profit'].apply(clean)

    if 'duration_str' in df.columns:
        df['holding_seconds'] = df['duration_str'].apply(parse_duration_to_seconds)

    keep = ['trader_id', 'symbol', 'profit', 'volume', 'holding_seconds']
    avail = [c for c in keep if c in df.columns]
    df = df[avail].copy()
    df = df.dropna(subset=['profit'])
    return df

def classify_style(sec):
    if pd.isna(sec):
        return "Unknown"
    if sec < 300:
        return "Scalper"
    elif sec < 14400:
        return "Day Trader"
    return "Swing Trader"

def safe_profit_factor(profits):
    gp = profits[profits > 0].sum()
    gl = abs(profits[profits < 0].sum())
    if gl == 0:
        return float('inf') if gp > 0 else 0.0
    return gp / gl

def main():
    files = glob.glob(os.path.join(INPUT_FOLDER, "*.xlsx")) + glob.glob(os.path.join(INPUT_FOLDER, "*.xls"))
    print(f"Found {len(files)} files")
    frames = []
    for i, f in enumerate(files):
        d = load_file(f)
        if d is not None and not d.empty:
            frames.append(d)
        if (i + 1) % 500 == 0:
            print(f"  processed {i+1}/{len(files)}")
    df = pd.concat(frames, ignore_index=True)
    df['symbol'] = df['symbol'].astype(str).str.strip().str.upper()
    print(f"Total trades loaded: {len(df)}, traders: {df['trader_id'].nunique()}")

    # ---- Task A: trader leaderboard excluding XAU/USD, XAG/USD ----
    df_f = df[~df['symbol'].isin(EXCLUDE)].copy()
    df_f['is_win'] = df_f['profit'] > 0
    profiles = df_f.groupby('trader_id').agg(
        total_trades=('profit', 'count'),
        win_rate=('is_win', 'mean'),
        avg_holding_seconds=('holding_seconds', 'mean'),
        avg_profit=('profit', 'mean'),
        avg_win=('profit', lambda x: x[x > 0].mean() if (x > 0).any() else None),
        avg_loss=('profit', lambda x: x[x < 0].mean() if (x < 0).any() else None),
        total_profit=('profit', 'sum'),
        profit_factor=('profit', safe_profit_factor),
        top_symbol=('symbol', lambda x: x.mode().iloc[0] if not x.mode().empty else 'N/A'),
    ).reset_index()
    profiles['style'] = profiles['avg_holding_seconds'].apply(classify_style)
    profiles = profiles.sort_values('total_profit', ascending=False)
    profiles.to_csv('summary_no_metals.csv', index=False)
    print("Saved summary_no_metals.csv")

    # ---- Task B: per-symbol risk profile normalized to 0.01 lot ----
    dv = df[(df['volume'].notna()) & (df['volume'] > 0)].copy()
    dv['profit_001'] = dv['profit'] * (0.01 / dv['volume'])
    sym = dv.groupby('symbol').agg(
        trades=('profit_001', 'count'),
        win_rate=('profit_001', lambda x: (x > 0).mean()),
        avg_pl_001=('profit_001', 'mean'),
        avg_win_001=('profit_001', lambda x: x[x > 0].mean() if (x > 0).any() else None),
        avg_loss_001=('profit_001', lambda x: x[x < 0].mean() if (x < 0).any() else None),
        worst_loss_001=('profit_001', 'min'),
        std_001=('profit_001', 'std'),
    ).reset_index()
    sym = sym[sym['trades'] >= 20]  # need enough samples to trust the stat
    sym['is_metal'] = sym['symbol'].isin(EXCLUDE)
    sym = sym.sort_values('std_001')
    sym.to_csv('symbol_risk_profile_001lot.csv', index=False)
    print("Saved symbol_risk_profile_001lot.csv")

if __name__ == "__main__":
    main()
