import pandas as pd
import matplotlib.pyplot as plt
import sys
from pathlib import Path
import os
import glob

# --- Configuration ---
INPUT_FOLDER = "trader_trades"
OUTPUT_PROFILES = "summary.csv"
OUTPUT_COMPARISON = "comparison.txt"
OUTPUT_CHART = "style_distribution.png"

# Style thresholds (seconds)
SCALPER_MAX_SEC = 300        # <5 minutes
DAY_TRADER_MAX_SEC = 14400   # <4 hours (4*3600)

# --- Helper functions ---
def classify_style(avg_holding_seconds):
    if pd.isna(avg_holding_seconds):
        return "Unknown"
    if avg_holding_seconds < SCALPER_MAX_SEC:
        return "Scalper"
    elif avg_holding_seconds < DAY_TRADER_MAX_SEC:
        return "Day Trader"
    else:
        return "Swing Trader"

def safe_profit_factor(profits):
    """Calculate profit factor: gross profit / gross loss (absolute)."""
    gross_profit = profits[profits > 0].sum()
    gross_loss = abs(profits[profits < 0].sum())
    if gross_loss == 0:
        return float('inf') if gross_profit > 0 else 0.0
    return gross_profit / gross_loss

def parse_duration_to_seconds(duration_str):
    """Convert duration string like '156:41' (HH:MM) or '1:23:45' (H:MM:SS) to seconds."""
    if pd.isna(duration_str):
        return None
    try:
        parts = str(duration_str).split(':')
        if len(parts) == 2:
            # HH:MM format
            hours, minutes = map(int, parts)
            return hours * 3600 + minutes * 60
        elif len(parts) == 3:
            # H:MM:SS or HH:MM:SS format
            hours, minutes, seconds = map(int, parts)
            return hours * 3600 + minutes * 60 + seconds
        else:
            return None
    except:
        return None

def load_trades_from_excel(filepath):
    """Load trades from an Excel file and extract trader info from filename."""
    try:
        df = pd.read_excel(filepath)
    except Exception as e:
        print(f"⚠️ Could not read {filepath}: {e}")
        return None
    
    if df.empty:
        return None
    
    # Extract trader name from filename (e.g., "Competition-01-24_R10_Cheryn T.xlsx" -> "Cheryn T")
    filename = os.path.basename(filepath)
    # Remove extension and try to extract trader name
    name_part = os.path.splitext(filename)[0]
    # Try to get the part after the last underscore or dash-number pattern
    trader_id = name_part
    # Common pattern: Competition-XX-XX_RN_Name -> extract Name
    if '_R' in name_part:
        parts = name_part.split('_R')
        if len(parts) > 1:
            # Get everything after _R followed by number
            remainder = '_R'.join(parts[1:])
            # Find first digit sequence and take text after it
            import re
            match = re.match(r'\d+_(.+)', remainder)
            if match:
                trader_id = match.group(1).strip()
    
    # Map columns to standard format
    rename_map = {
        'symbol': 'symbol',
        'p/l': 'profit',
        'direction': 'side',
        'opened': 'entry_time',
        'closed': 'exit_time',
        'duration': 'duration_str',
        'volume': 'volume',
        'order number': 'order_id'
    }
    
    df.columns = df.columns.str.strip().str.lower()
    df.rename(columns=rename_map, inplace=True)
    
    # Add trader_id column
    df['trader_id'] = trader_id
    
    # Convert profit - handle currency symbols like "$-6.15"
    if 'profit' in df.columns:
        def clean_profit(val):
            if pd.isna(val):
                return None
            try:
                # Remove $ and other currency symbols
                cleaned = str(val).replace('$', '').replace(',', '').strip()
                return float(cleaned)
            except:
                return None
        df['profit'] = df['profit'].apply(clean_profit)
    
    # Parse times
    if 'entry_time' in df.columns:
        df['entry_time'] = pd.to_datetime(df['entry_time'], errors='coerce', dayfirst=True)
    if 'exit_time' in df.columns:
        df['exit_time'] = pd.to_datetime(df['exit_time'], errors='coerce', dayfirst=True)
    
    # Calculate holding seconds from duration string if available
    if 'duration_str' in df.columns:
        df['holding_seconds'] = df['duration_str'].apply(parse_duration_to_seconds)
    
    # If holding_seconds is still null, calculate from entry/exit times
    if 'holding_seconds' in df.columns and 'entry_time' in df.columns and 'exit_time' in df.columns:
        mask = df['holding_seconds'].isna() & df['entry_time'].notna() & df['exit_time'].notna()
        df.loc[mask, 'holding_seconds'] = (df.loc[mask, 'exit_time'] - df.loc[mask, 'entry_time']).dt.total_seconds()
    
    # Keep only needed columns
    keep_cols = ['trader_id', 'symbol', 'side', 'profit', 'entry_time', 'exit_time', 'holding_seconds']
    available_cols = [c for c in keep_cols if c in df.columns]
    df = df[available_cols].copy()
    
    # Drop rows without valid profit or holding time
    df = df.dropna(subset=['profit'])
    
    return df

# --- Main ---
def main():
    # 1. Load all trade files
    all_trades = []
    
    # Check if trades.csv exists first (for backward compatibility)
    if os.path.exists("trades.csv"):
        print("📄 Found trades.csv, using it directly...")
        try:
            df = pd.read_csv("trades.csv")
            all_trades.append(df)
        except Exception as e:
            print(f"❌ Error reading trades.csv: {e}")
            sys.exit(1)
    elif os.path.exists(INPUT_FOLDER):
        # Look for Excel files
        excel_files = glob.glob(os.path.join(INPUT_FOLDER, "*.xlsx")) + \
                      glob.glob(os.path.join(INPUT_FOLDER, "*.xls"))
        
        if not excel_files:
            print(f"❌ No Excel files found in {INPUT_FOLDER}/")
            sys.exit(1)
        
        print(f"📂 Found {len(excel_files)} trade files in {INPUT_FOLDER}/")
        
        for filepath in excel_files:
            trades_df = load_trades_from_excel(filepath)
            if trades_df is not None and not trades_df.empty:
                all_trades.append(trades_df)
                print(f"  ✅ Loaded {len(trades_df)} trades from {trades_df['trader_id'].iloc[0]}")
    else:
        print(f"❌ Neither trades.csv nor {INPUT_FOLDER}/ folder found.")
        sys.exit(1)
    
    if not all_trades:
        print("❌ No trades loaded.")
        sys.exit(1)
    
    # Combine all trades
    df = pd.concat(all_trades, ignore_index=True)
    
    # Standardize column names
    df.columns = df.columns.str.strip().str.lower()
    
    required_cols = ['trader_id', 'profit']
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        print(f"❌ Missing required columns: {missing}")
        print("Available columns:", df.columns.tolist())
        sys.exit(1)
    
    # Ensure holding_seconds exists
    if 'holding_seconds' not in df.columns:
        df['holding_seconds'] = None
    
    # Calculate win flag
    df['is_win'] = df['profit'] > 0
    
    print(f"\n✅ Loaded {len(df)} total trades from {df['trader_id'].nunique()} traders.")
    
    # 2. Build trader profiles
    profiles = df.groupby('trader_id').agg(
        total_trades=('profit', 'count'),
        win_rate=('is_win', 'mean'),
        avg_holding_seconds=('holding_seconds', 'mean'),
        avg_profit=('profit', 'mean'),
        avg_win=('profit', lambda x: x[x > 0].mean() if (x > 0).any() else None),
        avg_loss=('profit', lambda x: x[x < 0].mean() if (x < 0).any() else None),
        total_profit=('profit', 'sum'),
        profit_factor=('profit', safe_profit_factor),
        top_symbol=('symbol', lambda x: x.mode().iloc[0] if not x.mode().empty else 'N/A')
    ).reset_index()
    
    # Classify trading style
    profiles['style'] = profiles['avg_holding_seconds'].apply(classify_style)
    
    # 3. Identify top 10 by total profit
    top_10 = profiles.nlargest(10, 'total_profit')
    all_avg = profiles.mean(numeric_only=True)
    top_10_avg = top_10.mean(numeric_only=True)
    
    # 4. Save full profiles
    profiles.to_csv(OUTPUT_PROFILES, index=False)
    print(f"📄 Saved trader profiles to {OUTPUT_PROFILES}")
    
    # 5. Save comparison report
    with open(OUTPUT_COMPARISON, 'w') as f:
        f.write("=== Top 10 Traders (by total profit) vs All Traders ===\n\n")
        comparison_df = pd.DataFrame({'All_Traders': all_avg, 'Top_10': top_10_avg})
        comparison_df = comparison_df[comparison_df.index.isin([
            'total_trades', 'win_rate', 'avg_holding_seconds', 'avg_profit',
            'avg_win', 'avg_loss', 'profit_factor'
        ])]
        f.write(comparison_df.to_string())
        f.write("\n\nTop 10 Traders:\n")
        f.write(top_10[['trader_id', 'total_profit', 'win_rate', 'style']].to_string(index=False))
    print(f"📄 Saved comparison to {OUTPUT_COMPARISON}")
    
    # 6. Style distribution chart
    style_counts = profiles['style'].value_counts()
    plt.figure(figsize=(8, 5))
    colors = []
    for style in style_counts.index:
        if style == 'Scalper':
            colors.append('#ff9999')
        elif style == 'Day Trader':
            colors.append('#66b3ff')
        elif style == 'Swing Trader':
            colors.append('#99ff99')
        else:
            colors.append('#ffcc99')
    
    bars = plt.bar(style_counts.index, style_counts.values, color=colors)
    plt.title('Trading Style Distribution')
    plt.xlabel('Style')
    plt.ylabel('Number of Traders')
    for bar in bars:
        yval = bar.get_height()
        plt.text(bar.get_x() + bar.get_width()/2, yval + 0.5, int(yval), ha='center', va='bottom')
    plt.tight_layout()
    plt.savefig(OUTPUT_CHART)
    print(f"📊 Saved chart to {OUTPUT_CHART}")
    
    # Print summary to logs
    print("\n=== Analysis Complete ===")
    print(f"Total traders analyzed: {len(profiles)}")
    print("Style distribution:")
    for style, count in style_counts.items():
        print(f"  {style}: {count}")
    print("\nTop 10 vs All comparison:")
    print(comparison_df.to_string())

if __name__ == "__main__":
    main()
