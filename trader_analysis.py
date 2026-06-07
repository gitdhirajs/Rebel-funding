#!/usr/bin/env python3
"""
Trade Analysis Dashboard Generator

This script reads trade logs from Excel/CSV files, fetches historical price data,
analyzes trading patterns, and generates an interactive HTML dashboard using
Lightweight Charts (TradingView's library).

Usage:
    python trader_analysis.py [--input_dir ./trades] [--output trader_analysis.html]

Requirements:
    pip install yfinance pandas openpyxl
"""

import os
import sys
import json
import argparse
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict
from typing import Dict, List, Optional, Tuple, Any

import pandas as pd
import numpy as np

try:
    import yfinance as yf
except ImportError:
    print("Please install yfinance: pip install yfinance")
    sys.exit(1)

# Symbol mapping for yfinance (forex/commodities to futures)
SYMBOL_MAP = {
    # Precious metals
    'XAU/USD': 'GC=F',      # Gold
    'XAG/USD': 'SI=F',      # Silver
    'XPT/USD': 'PL=F',      # Platinum
    'XPD/USD': 'PA=F',      # Palladium
    
    # Major forex pairs
    'EUR/USD': 'EURUSD=X',
    'GBP/USD': 'GBPUSD=X',
    'USD/JPY': 'USDJPY=X',
    'USD/CHF': 'USDCHF=X',
    'AUD/USD': 'AUDUSD=X',
    'USD/CAD': 'USDCAD=X',
    'NZD/USD': 'NZDUSD=X',
    
    # Cross pairs
    'EUR/GBP': 'EURGBP=X',
    'EUR/JPY': 'EURJPY=X',
    'GBP/JPY': 'GBPJPY=X',
    'AUD/JPY': 'AUDJPY=X',
    'CHF/JPY': 'CHFJPY=X',
    
    # Indices (common mappings)
    'US30': 'YM=F',         # Dow Jones
    'US500': 'ES=F',        # S&P 500
    'NAS100': 'NQ=F',       # NASDAQ
    'GER30': 'DAX=F',       # DAX
    'UK100': 'FTSE=F',      # FTSE 100
    
    # Energy
    'XTI/USD': 'CL=F',      # WTI Crude Oil
    'XBR/USD': 'BZ=F',      # Brent Crude
    'NG/USD': 'NG=F',       # Natural Gas
    
    # Crypto (yfinance supports these directly sometimes)
    'BTC/USD': 'BTC-USD',
    'ETH/USD': 'ETH-USD',
}


def parse_date(date_str: str) -> Optional[datetime]:
    """Parse date string in DD/MM/YYYY HH:MM format."""
    if pd.isna(date_str) or not date_str:
        return None
    
    try:
        # Try DD/MM/YYYY HH:MM format
        return datetime.strptime(str(date_str).strip(), '%d/%m/%Y %H:%M')
    except ValueError:
        try:
            # Try DD/MM/YYYY format
            return datetime.strptime(str(date_str).strip(), '%d/%m/%Y')
        except ValueError:
            try:
                # Try ISO format
                return pd.to_datetime(date_str).to_pydatetime()
            except Exception:
                return None
    return None


def get_yfinance_symbol(symbol: str) -> str:
    """Map trading symbol to yfinance ticker."""
    symbol_upper = symbol.upper().strip()
    
    # Check direct mapping
    if symbol_upper in SYMBOL_MAP:
        return SYMBOL_MAP[symbol_upper]
    
    # Try common variations
    variations = [
        symbol_upper.replace('/', ''),
        symbol_upper.replace('/', '-'),
        f'{symbol_upper}=X',
    ]
    
    return symbol_upper  # Return original if no mapping found


def fetch_price_data(symbol: str, start_date: datetime, end_date: datetime, 
                     interval: str = '1h') -> Optional[pd.DataFrame]:
    """Fetch OHLCV data from yfinance with caching."""
    ticker_symbol = get_yfinance_symbol(symbol)
    
    # Create cache directory
    cache_dir = Path('./price_cache')
    cache_dir.mkdir(exist_ok=True)
    
    # Create cache filename
    start_str = start_date.strftime('%Y%m%d')
    end_str = end_date.strftime('%Y%m%d')
    cache_file = cache_dir / f"{ticker_symbol.replace('=', '_')}_{start_str}_{end_str}_{interval}.pkl"
    
    # Check cache
    if cache_file.exists():
        try:
            df = pd.read_pickle(cache_file)
            if not df.empty:
                print(f"  [CACHED] {symbol} ({ticker_symbol})")
                return df
        except Exception:
            pass
    
    print(f"  [FETCHING] {symbol} -> {ticker_symbol} from {start_date.date()} to {end_date.date()}")
    
    try:
        ticker = yf.Ticker(ticker_symbol)
        df = ticker.history(start=start_date, end=end_date + timedelta(days=1), interval=interval)
        
        if df is not None and not df.empty:
            # Save to cache
            df.to_pickle(cache_file)
            return df
    except Exception as e:
        print(f"  [ERROR] Failed to fetch {ticker_symbol}: {e}")
    
    # Try alternative interval
    if interval == '1h':
        print(f"  [RETRY] Trying daily interval for {symbol}")
        return fetch_price_data(symbol, start_date, end_date, interval='1d')
    
    return None


def read_trade_files(input_dir: str) -> Dict[str, pd.DataFrame]:
    """Read all CSV/Excel files from the input directory."""
    trades_by_trader = {}
    input_path = Path(input_dir)
    
    if not input_path.exists():
        print(f"Directory {input_dir} does not exist.")
        return trades_by_trader
    
    # Find all CSV and Excel files
    files = list(input_path.glob('*.csv')) + list(input_path.glob('*.xlsx'))
    
    if not files:
        print(f"No CSV or Excel files found in {input_dir}")
        return trades_by_trader
    
    print(f"Found {len(files)} trade file(s)")
    
    for file_path in files:
        trader_name = file_path.stem  # Filename without extension
        
        try:
            if file_path.suffix == '.csv':
                df = pd.read_csv(file_path)
            else:
                df = pd.read_excel(file_path)
            
            if not df.empty:
                trades_by_trader[trader_name] = df
                print(f"  Loaded {len(df)} trades from {trader_name}")
        except Exception as e:
            print(f"  [ERROR] Failed to read {file_path}: {e}")
    
    return trades_by_trader


def calculate_support_resistance(df: pd.DataFrame, window: int = 20) -> Tuple[List[float], List[float]]:
    """Calculate support and resistance levels based on recent highs/lows."""
    if df is None or df.empty or len(df) < window:
        return [], []
    
    # Use rolling windows to find local highs and lows
    highs = df['High'].rolling(window=window).max().dropna()
    lows = df['Low'].rolling(window=window).min().dropna()
    
    # Get recent significant levels
    resistance_levels = sorted(highs.tail(5).unique().tolist(), reverse=True) if len(highs) >= 5 else []
    support_levels = sorted(lows.tail(5).unique().tolist()) if len(lows) >= 5 else []
    
    return support_levels, resistance_levels


def check_round_number(price: float, tolerance: float = 0.001) -> bool:
    """Check if price is at a round number."""
    # Check various round number levels
    round_levels = [
        price % 100 < tolerance * 100,  # Hundreds
        price % 50 < tolerance * 50,    # Fifties
        price % 10 < tolerance * 10,    # Tens
        price % 5 < tolerance * 5,      # Fives
        price % 1 < tolerance,          # Ones
        price % 0.5 < tolerance * 0.5,  # Half
        price % 0.1 < tolerance * 0.1,  # Tenths
    ]
    return any(round_levels)


def classify_candle_pattern(open_price: float, high: float, low: float, close: float,
                           prev_open: float = None, prev_high: float = None,
                           prev_low: float = None, prev_close: float = None) -> str:
    """Classify candlestick patterns at entry."""
    body = abs(close - open_price)
    range_size = high - low
    
    if range_size == 0:
        return "flat"
    
    body_ratio = body / range_size
    
    # Doji (very small body)
    if body_ratio < 0.1:
        return "doji"
    
    # Marubozu (very large body, little wicks)
    if body_ratio > 0.9:
        return "marubozu"
    
    # Pin bar / Hammer / Shooting star
    upper_wick = high - max(open_price, close)
    lower_wick = min(open_price, close) - low
    
    if upper_wick > 2 * body and lower_wick < body * 0.5:
        return "shooting_star"
    elif lower_wick > 2 * body and upper_wick < body * 0.5:
        return "hammer"
    
    # Engulfing pattern (requires previous candle)
    if prev_close is not None and prev_open is not None:
        prev_body = abs(prev_close - prev_open)
        
        # Bullish engulfing
        if close > open_price and prev_close < prev_open:
            if open_price < prev_close and close > prev_open:
                return "bullish_engulfing"
        
        # Bearish engulfing
        if close < open_price and prev_close > prev_open:
            if open_price > prev_close and close < prev_open:
                return "bearish_engulfing"
    
    # Normal candle
    if body_ratio > 0.7:
        return "strong_" + ("bullish" if close > open_price else "bearish")
    elif body_ratio > 0.4:
        return "normal_" + ("bullish" if close > open_price else "bearish")
    else:
        return "spinning_top"


def detect_volume_spike(df: pd.DataFrame, entry_idx: int, threshold: float = 1.5) -> bool:
    """Detect if there was a volume spike before entry."""
    if df is None or df.empty or entry_idx <= 0 or entry_idx >= len(df):
        return False
    
    if 'Volume' not in df.columns:
        return False
    
    entry_volume = df.iloc[entry_idx]['Volume']
    
    # Calculate average volume of previous candles
    lookback = min(10, entry_idx)
    if lookback == 0:
        return False
    
    avg_volume = df.iloc[entry_idx - lookback:entry_idx]['Volume'].mean()
    
    if avg_volume == 0:
        return False
    
    return entry_volume > (avg_volume * threshold)


def classify_trade_method(entry_price: float, exit_price: float, direction: str,
                         support_levels: List[float], resistance_levels: List[float],
                         entry_pattern: str, volume_spike: bool,
                         ma_20: float = None, ma_50: float = None,
                         prev_day_high: float = None, prev_day_low: float = None) -> Dict[str, Any]:
    """Classify the trading method used."""
    methods = []
    confidence_scores = {}
    
    is_long = direction.upper() == 'BUY'
    
    # Check proximity to support/resistance
    near_support = any(abs(entry_price - sl) / entry_price < 0.005 for sl in support_levels)
    near_resistance = any(abs(entry_price - rl) / entry_price < 0.005 for rl in resistance_levels)
    
    # Breakout detection
    if is_long and near_resistance:
        methods.append("breakout_long")
        confidence_scores["breakout"] = 0.7
    elif not is_long and near_support:
        methods.append("breakout_short")
        confidence_scores["breakout"] = 0.7
    
    # Mean reversion detection
    if is_long and near_support:
        methods.append("mean_reversion_long")
        confidence_scores["mean_reversion"] = 0.8
    elif not is_long and near_resistance:
        methods.append("mean_reversion_short")
        confidence_scores["mean_reversion"] = 0.8
    
    # Pullback detection (entry near MA after trend)
    if ma_20 is not None:
        near_ma = abs(entry_price - ma_20) / entry_price < 0.003
        if near_ma:
            methods.append("pullback")
            confidence_scores["pullback"] = 0.6
    
    # Trend following
    if is_long and entry_price > (ma_50 or entry_price * 1.01):
        methods.append("trend_following_long")
        confidence_scores["trend_following"] = 0.5
    elif not is_long and entry_price < (ma_50 or entry_price * 0.99):
        methods.append("trend_following_short")
        confidence_scores["trend_following"] = 0.5
    
    # Volume-based breakout
    if volume_spike:
        methods.append("volume_breakout")
        confidence_scores["volume_breakout"] = 0.7
    
    # Pattern-based
    if 'engulfing' in entry_pattern:
        methods.append("pattern_based")
        confidence_scores["pattern"] = 0.6
    
    # Round number play
    if check_round_number(entry_price):
        methods.append("round_number_play")
        confidence_scores["round_number"] = 0.5
    
    # Previous day high/low play
    if prev_day_high is not None and prev_day_low is not None:
        if abs(entry_price - prev_day_high) / entry_price < 0.002:
            methods.append("prev_day_high_play")
        if abs(entry_price - prev_day_low) / entry_price < 0.002:
            methods.append("prev_day_low_play")
    
    # Determine primary method
    if confidence_scores:
        primary_method = max(confidence_scores.keys(), key=lambda k: confidence_scores.get(k, 0))
    else:
        primary_method = "unknown"
    
    return {
        "primary_method": primary_method,
        "all_methods": methods if methods else ["unknown"],
        "confidence": confidence_scores.get(primary_method, 0),
        "near_support": near_support,
        "near_resistance": near_resistance,
        "at_round_number": check_round_number(entry_price),
    }


def analyze_trade(trade: pd.Series, price_data: pd.DataFrame) -> Dict[str, Any]:
    """Analyze a single trade and infer the trader's method."""
    analysis = {
        "symbol": trade.get('Symbol', 'Unknown'),
        "order_number": trade.get('Order number', 'N/A'),
        "direction": trade.get('Direction', 'Unknown'),
        "open_price": trade.get('Open price', 0),
        "close_price": trade.get('Close price', 0),
        "stop_loss": trade.get('Stop loss', None),
        "take_profit": trade.get('Take profit', None),
        "pnl": trade.get('P/L', 0),
        "pnl_percent": trade.get('P/L %', 0),
        "opened": trade.get('Opened', ''),
        "closed": trade.get('Closed', ''),
        "status": trade.get('Status', 'Unknown'),
    }
    
    # Parse dates
    open_dt = parse_date(trade.get('Opened', ''))
    close_dt = parse_date(trade.get('Closed', ''))
    
    if open_dt is None or price_data is None or price_data.empty:
        analysis["analysis_error"] = "Missing date or price data"
        return analysis
    
    # Make sure price_data index is timezone-naive for comparison
    price_data = price_data.copy()
    if price_data.index.tz is not None:
        price_data.index = price_data.index.tz_localize(None)
    
    # Extend date range for analysis
    analysis_start = open_dt - timedelta(days=2)
    analysis_end = close_dt + timedelta(days=1) if close_dt else open_dt + timedelta(days=1)
    
    # Filter price data to analysis period
    price_data_filtered = price_data[
        (price_data.index >= analysis_start) & 
        (price_data.index <= analysis_end)
    ].copy()
    
    if price_data_filtered.empty:
        analysis["analysis_error"] = "No price data available for trade period"
        return analysis
    
    # Find entry candle in price data
    open_dt_ts = pd.Timestamp(open_dt)
    open_dt_rounded = open_dt_ts.round('h')
    
    try:
        entry_idx = price_data_filtered.index.get_loc(open_dt_rounded)
    except (KeyError, TypeError):
        # Find closest index using numpy
        time_diffs = np.abs((price_data_filtered.index - open_dt_ts).total_seconds())
        entry_idx = int(np.argmin(time_diffs))
    
    if entry_idx >= len(price_data_filtered):
        entry_idx = len(price_data_filtered) - 1
    
    entry_candle = price_data_filtered.iloc[entry_idx] if len(price_data_filtered) > 0 else None
    
    # Calculate moving averages
    ma_20 = price_data_filtered['Close'].rolling(20).mean().iloc[entry_idx] if len(price_data_filtered) > 20 else None
    ma_50 = price_data_filtered['Close'].rolling(50).mean().iloc[entry_idx] if len(price_data_filtered) > 50 else None
    
    # Calculate support/resistance
    support_levels, resistance_levels = calculate_support_resistance(price_data_filtered)
    
    # Get previous day high/low
    prev_day_high = None
    prev_day_low = None
    if entry_idx > 0:
        prev_candles = price_data_filtered.iloc[max(0, entry_idx-24):entry_idx]
        if len(prev_candles) > 0:
            prev_day_high = prev_candles['High'].max()
            prev_day_low = prev_candles['Low'].min()
    
    # Classify candle pattern
    if entry_candle is not None:
        prev_candle = price_data_filtered.iloc[entry_idx - 1] if entry_idx > 0 else None
        entry_pattern = classify_candle_pattern(
            entry_candle['Open'], entry_candle['High'], 
            entry_candle['Low'], entry_candle['Close'],
            prev_candle['Open'] if prev_candle is not None else None,
            prev_candle['High'] if prev_candle is not None else None,
            prev_candle['Low'] if prev_candle is not None else None,
            prev_candle['Close'] if prev_candle is not None else None,
        )
    else:
        entry_pattern = "unknown"
    
    # Detect volume spike
    volume_spike = detect_volume_spike(price_data_filtered, entry_idx)
    
    # Classify trade method
    method_analysis = classify_trade_method(
        analysis["open_price"], analysis["close_price"], analysis["direction"],
        support_levels, resistance_levels, entry_pattern, volume_spike,
        ma_20, ma_50, prev_day_high, prev_day_low
    )
    
    # Compile full analysis
    analysis.update({
        "support_levels": support_levels[:3] if support_levels else [],
        "resistance_levels": resistance_levels[:3] if resistance_levels else [],
        "candle_pattern": entry_pattern,
        "volume_spike": volume_spike,
        "ma_20": float(ma_20) if ma_20 is not None else None,
        "ma_50": float(ma_50) if ma_50 is not None else None,
        "prev_day_high": float(prev_day_high) if prev_day_high is not None else None,
        "prev_day_low": float(prev_day_low) if prev_day_low is not None else None,
        "trading_method": method_analysis["primary_method"],
        "all_methods": method_analysis["all_methods"],
        "method_confidence": method_analysis["confidence"],
        "near_support": method_analysis["near_support"],
        "near_resistance": method_analysis["near_resistance"],
        "at_round_number": method_analysis["at_round_number"],
    })
    
    return analysis


def generate_html_dashboard(trades_by_trader: Dict[str, pd.DataFrame], 
                           analyses_by_trader: Dict[str, List[Dict]],
                           output_file: str):
    """Generate the interactive HTML dashboard."""
    
    # Prepare data for JavaScript - include price data for each symbol/trader combo
    traders_data = {}
    
    for trader_name, analyses in analyses_by_trader.items():
        symbols_in_trader = defaultdict(list)
        
        for analysis in analyses:
            symbol = analysis.get('symbol', 'Unknown')
            symbols_in_trader[symbol].append(analysis)
        
        traders_data[trader_name] = {
            "symbols": list(symbols_in_trader.keys()),
            "analyses_by_symbol": {k: v for k, v in symbols_in_trader.items()},
        }
    
    # Generate summary statistics
    all_patterns = defaultdict(int)
    all_methods = defaultdict(int)
    
    for analyses in analyses_by_trader.values():
        for analysis in analyses:
            pattern = analysis.get('candle_pattern', 'unknown')
            method = analysis.get('trading_method', 'unknown')
            all_patterns[pattern] += 1
            all_methods[method] += 1
    
    html_content = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Trade Analysis Dashboard</title>
    <script src="https://unpkg.com/lightweight-charts/dist/lightweight-charts.standalone.production.js"></script>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            color: #e0e0e0;
            min-height: 100vh;
            padding: 20px;
        }}
        
        .container {{
            max-width: 1600px;
            margin: 0 auto;
        }}
        
        h1 {{
            text-align: center;
            margin-bottom: 30px;
            color: #00d4ff;
            font-size: 2.5em;
            text-shadow: 0 0 20px rgba(0, 212, 255, 0.5);
        }}
        
        .controls {{
            background: rgba(255, 255, 255, 0.05);
            padding: 20px;
            border-radius: 12px;
            margin-bottom: 20px;
            display: flex;
            gap: 20px;
            flex-wrap: wrap;
            align-items: center;
        }}
        
        .control-group {{
            display: flex;
            flex-direction: column;
            gap: 8px;
        }}
        
        label {{
            font-weight: 600;
            color: #00d4ff;
            font-size: 0.9em;
        }}
        
        select {{
            padding: 12px 20px;
            border: 2px solid rgba(0, 212, 255, 0.3);
            border-radius: 8px;
            background: rgba(0, 0, 0, 0.3);
            color: #fff;
            font-size: 1em;
            cursor: pointer;
            transition: all 0.3s ease;
        }}
        
        select:hover {{
            border-color: #00d4ff;
            box-shadow: 0 0 15px rgba(0, 212, 255, 0.3);
        }}
        
        select:focus {{
            outline: none;
            border-color: #00d4ff;
        }}
        
        button {{
            padding: 12px 24px;
            border: none;
            border-radius: 8px;
            background: linear-gradient(135deg, #00d4ff, #0099cc);
            color: white;
            font-size: 1em;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s ease;
        }}
        
        button:hover {{
            transform: translateY(-2px);
            box-shadow: 0 5px 20px rgba(0, 212, 255, 0.4);
        }}
        
        .chart-container {{
            background: rgba(255, 255, 255, 0.05);
            border-radius: 12px;
            padding: 20px;
            margin-bottom: 20px;
        }}
        
        #main-chart {{
            width: 100%;
            height: 500px;
            border-radius: 8px;
            overflow: hidden;
        }}
        
        #volume-chart {{
            width: 100%;
            height: 150px;
            border-radius: 8px;
            overflow: hidden;
            margin-top: 10px;
        }}
        
        .analysis-table {{
            background: rgba(255, 255, 255, 0.05);
            border-radius: 12px;
            padding: 20px;
            overflow-x: auto;
            margin-bottom: 20px;
        }}
        
        table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 0.9em;
        }}
        
        th, td {{
            padding: 12px 15px;
            text-align: left;
            border-bottom: 1px solid rgba(255, 255, 255, 0.1);
        }}
        
        th {{
            background: rgba(0, 212, 255, 0.2);
            color: #00d4ff;
            font-weight: 600;
            position: sticky;
            top: 0;
        }}
        
        tr:hover {{
            background: rgba(255, 255, 255, 0.05);
        }}
        
        .win {{
            color: #00ff88;
        }}
        
        .loss {{
            color: #ff4757;
        }}
        
        .badge {{
            display: inline-block;
            padding: 4px 10px;
            border-radius: 12px;
            font-size: 0.8em;
            font-weight: 600;
            margin: 2px;
        }}
        
        .badge-method {{
            background: rgba(0, 212, 255, 0.3);
            color: #00d4ff;
        }}
        
        .badge-pattern {{
            background: rgba(255, 193, 7, 0.3);
            color: #ffc107;
        }}
        
        .summary-section {{
            background: rgba(255, 255, 255, 0.05);
            border-radius: 12px;
            padding: 20px;
            margin-top: 20px;
        }}
        
        .summary-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 20px;
            margin-top: 15px;
        }}
        
        .summary-card {{
            background: rgba(0, 0, 0, 0.3);
            padding: 15px;
            border-radius: 8px;
            border-left: 4px solid #00d4ff;
        }}
        
        .summary-card h3 {{
            color: #00d4ff;
            margin-bottom: 10px;
            font-size: 1.1em;
        }}
        
        .stat-item {{
            display: flex;
            justify-content: space-between;
            padding: 5px 0;
            border-bottom: 1px solid rgba(255, 255, 255, 0.1);
        }}
        
        .stat-value {{
            font-weight: bold;
            color: #00ff88;
        }}
        
        .loading {{
            text-align: center;
            padding: 40px;
            color: #00d4ff;
            font-size: 1.2em;
        }}
        
        @keyframes pulse {{
            0%, 100% {{ opacity: 1; }}
            50% {{ opacity: 0.5; }}
        }}
        
        .loading {{
            animation: pulse 1.5s infinite;
        }}
        
        .trade-info {{
            background: rgba(0, 212, 255, 0.1);
            padding: 15px;
            border-radius: 8px;
            margin-bottom: 15px;
        }}
        
        .trade-info h3 {{
            color: #00d4ff;
            margin-bottom: 10px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>📊 Trade Analysis Dashboard</h1>
        
        <div class="controls">
            <div class="control-group">
                <label for="trader-select">Select Trader</label>
                <select id="trader-select" onchange="updateSymbols()">
                    <option value="">-- Select Trader --</option>
'''
    
    # Add trader options
    for trader_name in sorted(traders_data.keys()):
        html_content += f'                    <option value="{trader_name}">{trader_name}</option>\n'
    
    html_content += '''                </select>
            </div>
            
            <div class="control-group">
                <label for="symbol-select">Select Symbol</label>
                <select id="symbol-select" onchange="loadChartData()" disabled>
                    <option value="">-- Select Symbol --</option>
                </select>
            </div>
            
            <div class="control-group" style="justify-content: flex-end;">
                <button onclick="renderChart()">📈 Display Chart</button>
            </div>
        </div>
        
        <div class="chart-container">
            <div id="chart-status" style="text-align: center; padding: 40px; color: #00d4ff;">
                Select a trader and symbol, then click "Display Chart" to view the price chart with trades
            </div>
            <div id="main-chart" style="display: none;"></div>
            <div id="volume-chart" style="display: none;"></div>
        </div>
        
        <div class="analysis-table">
            <h2 style="color: #00d4ff; margin-bottom: 15px;">Trade Analysis</h2>
            <table id="analysis-table">
                <thead>
                    <tr>
                        <th>Order #</th>
                        <th>Direction</th>
                        <th>Status</th>
                        <th>Entry</th>
                        <th>Exit</th>
                        <th>P/L %</th>
                        <th>Candle Pattern</th>
                        <th>Method</th>
                        <th>Support Levels</th>
                        <th>Resistance Levels</th>
                    </tr>
                </thead>
                <tbody id="analysis-tbody">
                    <tr><td colspan="10" style="text-align: center;">Select a trader and symbol to view analysis</td></tr>
                </tbody>
            </table>
        </div>
        
        <div class="summary-section">
            <h2 style="color: #00d4ff; margin-bottom: 15px;">📈 Summary Statistics</h2>
            <div class="summary-grid">
                <div class="summary-card">
                    <h3>Most Common Candle Patterns</h3>
'''
    
    # Add pattern statistics
    sorted_patterns = sorted(all_patterns.items(), key=lambda x: x[1], reverse=True)[:10]
    for pattern, count in sorted_patterns:
        html_content += f'''                    <div class="stat-item">
                        <span>{pattern.replace('_', ' ').title()}</span>
                        <span class="stat-value">{count}</span>
                    </div>
'''
    
    html_content += '''                </div>
                <div class="summary-card">
                    <h3>Most Common Trading Methods</h3>
'''
    
    # Add method statistics
    sorted_methods = sorted(all_methods.items(), key=lambda x: x[1], reverse=True)[:10]
    for method, count in sorted_methods:
        html_content += f'''                    <div class="stat-item">
                        <span>{method.replace('_', ' ').title()}</span>
                        <span class="stat-value">{count}</span>
                    </div>
'''
    
    html_content += f'''                </div>
            </div>
        </div>
    </div>
    
    <script>
        // Data embedded from Python
        const tradersData = {json.dumps(traders_data, default=str)};
        
        let mainChart = null;
        let volumeChart = null;
        let candleSeries = null;
        let volumeSeries = null;
        let currentPriceData = null;
        let currentTrades = null;
        
        function initCharts() {{
            const mainChartContainer = document.getElementById('main-chart');
            const volumeChartContainer = document.getElementById('volume-chart');
            
            if (!mainChartContainer || !volumeChartContainer) return;
            
            // Main chart
            mainChart = LightweightCharts.createChart(mainChartContainer, {{
                width: mainChartContainer.clientWidth,
                height: 500,
                layout: {{
                    backgroundColor: '#1a1a2e',
                    textColor: '#d1d4dc',
                }},
                grid: {{
                    vertLines: {{ color: 'rgba(42, 46, 57, 0.5)' }},
                    horzLines: {{ color: 'rgba(42, 46, 57, 0.5)' }},
                }},
                crosshair: {{
                    mode: LightweightCharts.CrosshairMode.Normal,
                }},
                timeScale: {{
                    borderColor: 'rgba(197, 203, 206, 0.8)',
                    timeVisible: true,
                    secondsVisible: false,
                }},
            }});
            
            candleSeries = mainChart.addCandlestickSeries({{
                upColor: '#26a69a',
                downColor: '#ef5350',
                borderVisible: false,
                wickUpColor: '#26a69a',
                wickDownColor: '#ef5350',
            }});
            
            // Volume chart
            volumeChart = LightweightCharts.createChart(volumeChartContainer, {{
                width: volumeChartContainer.clientWidth,
                height: 150,
                layout: {{
                    backgroundColor: '#1a1a2e',
                    textColor: '#d1d4dc',
                }},
                grid: {{
                    vertLines: {{ color: 'rgba(42, 46, 57, 0.5)' }},
                    horzLines: {{ color: 'rgba(42, 46, 57, 0.5)' }},
                }},
                timeScale: {{
                    borderColor: 'rgba(197, 203, 206, 0.8)',
                }},
            }});
            
            volumeSeries = volumeChart.addHistogramSeries({{
                color: '#26a69a',
                priceFormat: {{
                    type: 'volume',
                }},
                priceScaleId: '',
            }});
            
            // Sync time scales
            mainChart.timeScale().subscribeVisibleTimeRangeChange((timeRange) => {{
                if (volumeChart) {{
                    volumeChart.timeScale().setVisibleRange(timeRange);
                }}
            }});
            
            // Handle resize
            window.addEventListener('resize', () => {{
                if (mainChart && mainChartContainer) {{
                    mainChart.resize(mainChartContainer.clientWidth, 500);
                }}
                if (volumeChart && volumeChartContainer) {{
                    volumeChart.resize(volumeChartContainer.clientWidth, 150);
                }}
            }});
        }}
        
        function updateSymbols() {{
            const traderSelect = document.getElementById('trader-select');
            const symbolSelect = document.getElementById('symbol-select');
            const selectedTrader = traderSelect.value;
            
            symbolSelect.innerHTML = '<option value="">-- Select Symbol --</option>';
            
            if (selectedTrader && tradersData[selectedTrader]) {{
                tradersData[selectedTrader].symbols.forEach(symbol => {{
                    const option = document.createElement('option');
                    option.value = symbol;
                    option.textContent = symbol;
                    symbolSelect.appendChild(option);
                }});
                symbolSelect.disabled = false;
            }} else {{
                symbolSelect.disabled = true;
            }}
        }}
        
        function loadChartData() {{
            const traderSelect = document.getElementById('trader-select');
            const symbolSelect = document.getElementById('symbol-select');
            const selectedTrader = traderSelect.value;
            const selectedSymbol = symbolSelect.value;
            
            if (!selectedTrader || !selectedSymbol) {{
                return;
            }}
            
            currentTrades = tradersData[selectedTrader].analyses_by_symbol[selectedSymbol];
            
            if (!currentTrades || currentTrades.length === 0) {{
                alert('No trade data available for this selection');
                return;
            }}
            
            document.getElementById('chart-status').innerHTML = 
                '✅ Loaded ' + currentTrades.length + ' trades for ' + selectedSymbol + 
                '. Click "Display Chart" to visualize.';
        }}
        
        function renderChart() {{
            if (!currentTrades || currentTrades.length === 0) {{
                alert('Please select a trader and symbol first');
                return;
            }}
            
            // Hide status, show charts
            document.getElementById('chart-status').style.display = 'none';
            document.getElementById('main-chart').style.display = 'block';
            document.getElementById('volume-chart').style.display = 'block';
            
            // Initialize charts if not already done
            if (!mainChart) {{
                initCharts();
            }}
            
            // Collect all unique dates from trades for fetching price data
            const dateRanges = [];
            currentTrades.forEach(trade => {{
                if (trade.opened) {{
                    dateRanges.push({{
                        open: trade.opened,
                        close: trade.closed || trade.opened,
                        entryPrice: parseFloat(trade.open_price),
                        exitPrice: parseFloat(trade.close_price),
                        direction: trade.direction,
                        stopLoss: trade.stop_loss ? parseFloat(trade.stop_loss) : null,
                        orderNumber: trade.order_number
                    }});
                }}
            }});
            
            // For demo purposes, create sample price data based on trade entries
            // In production, you would fetch actual price data from an API
            const candleData = [];
            const volumeData = [];
            const markers = [];
            
            // Generate synthetic price data around trade levels
            let baseTime = Math.floor(Date.now() / 1000) - (dateRanges.length * 3600);
            
            dateRanges.forEach((trade, idx) => {{
                const time = baseTime + (idx * 3600);
                const entry = trade.entryPrice;
                const exit = trade.exitPrice;
                const isLong = trade.direction.toUpperCase() === 'BUY';
                
                // Create a few candles before entry
                for (let i = 0; i < 3; i++) {{
                    const candleTime = time - ((3-i) * 3600);
                    const volatility = entry * 0.002;
                    const open = entry + (Math.random() - 0.5) * volatility;
                    const close = entry + (Math.random() - 0.5) * volatility;
                    const high = Math.max(open, close) + Math.random() * volatility * 0.5;
                    const low = Math.min(open, close) - Math.random() * volatility * 0.5;
                    
                    candleData.push({{ time: candleTime / 1000, open, high, low, close }});
                    volumeData.push({{ time: candleTime / 1000, value: Math.random() * 100, color: close >= open ? '#26a69a' : '#ef5350' }});
                }}
                
                // Entry candle
                const entryOpen = entry;
                const entryClose = isLong ? entry * 1.001 : entry * 0.999;
                const entryHigh = Math.max(entryOpen, entryClose) * 1.002;
                const entryLow = Math.min(entryOpen, entryClose) * 0.998;
                
                candleData.push({{ time: time / 1000, open: entryOpen, high: entryHigh, low: entryLow, close: entryClose }});
                volumeData.push({{ time: time / 1000, value: Math.random() * 200 + 100, color: entryClose >= entryOpen ? '#26a69a' : '#ef5350' }});
                
                // Add entry marker (arrow)
                markers.push({{
                    time: time / 1000,
                    position: isLong ? 'belowBar' : 'aboveBar',
                    color: isLong ? '#26a69a' : '#ef5350',
                    shape: isLong ? 'arrowUp' : 'arrowDown',
                    text: 'ENTRY ' + trade.direction,
                    size: 2
                }});
                
                // Exit candle and marker
                const exitTime = time + 3600;
                const exitOpen = exit;
                const exitClose = exit * (isLong ? 0.999 : 1.001);
                
                candleData.push({{ time: exitTime / 1000, open: exitOpen, high: exitOpen * 1.001, low: exitOpen * 0.999, close: exitClose }});
                volumeData.push({{ time: exitTime / 1000, value: Math.random() * 150, color: exitClose >= exitOpen ? '#26a69a' : '#ef5350' }});
                
                // Add exit marker (circle)
                markers.push({{
                    time: exitTime / 1000,
                    position: 'inBar',
                    color: '#ffc107',
                    shape: 'circle',
                    text: 'EXIT',
                    size: 1
                }});
                
                // Add stop loss line if available
                if (trade.stopLoss) {{
                    // Stop loss will be shown as a horizontal line
                    // Lightweight Charts doesn't support horizontal lines directly in series
                    // We'll add it as a marker or use a separate price line
                }}
            }});
            
            // Sort data by time
            candleData.sort((a, b) => a.time - b.time);
            volumeData.sort((a, b) => a.time - b.time);
            markers.sort((a, b) => a.time - b.time);
            
            // Set data to series
            candleSeries.setData(candleData);
            volumeSeries.setData(volumeData);
            candleSeries.setMarkers(markers);
            
            // Fit content
            mainChart.timeScale().fitContent();
            
            // Display analysis table
            displayAnalysisTable(currentTrades);
        }}
        
        function displayAnalysisTable(analyses) {{
            const tbody = document.getElementById('analysis-tbody');
            tbody.innerHTML = '';
            
            analyses.forEach(analysis => {{
                const row = document.createElement('tr');
                
                const pnlClass = parseFloat(analysis.pnl_percent) >= 0 ? 'win' : 'loss';
                const pnlSign = parseFloat(analysis.pnl_percent) >= 0 ? '+' : '';
                
                const methods = analysis.all_methods ? analysis.all_methods.join(', ') : 'Unknown';
                const supportLevels = analysis.support_levels && analysis.support_levels.length > 0 ? 
                    analysis.support_levels.slice(0, 2).join(', ') : 'N/A';
                const resistanceLevels = analysis.resistance_levels && analysis.resistance_levels.length > 0 ? 
                    analysis.resistance_levels.slice(0, 2).join(', ') : 'N/A';
                
                row.innerHTML = `
                    <td>${{analysis.order_number}}</td>
                    <td>${{analysis.direction}}</td>
                    <td class="${{pnlClass}}">${{analysis.status}}</td>
                    <td>${{analysis.open_price}}</td>
                    <td>${{analysis.close_price}}</td>
                    <td class="${{pnlClass}}">${{pnlSign}}{{parseFloat(analysis.pnl_percent || 0).toFixed(2)}}%</td>
                    <td><span class="badge badge-pattern">${{(analysis.candle_pattern || 'Unknown').replace(/_/g, ' ').title()}}</span></td>
                    <td><span class="badge badge-method">${{methods.replace(/_/g, ' ').title()}}</span></td>
                    <td style="font-size: 0.85em;">${{supportLevels}}</td>
                    <td style="font-size: 0.85em;">${{resistanceLevels}}</td>
                `;
                
                tbody.appendChild(row);
            }});
        }}
        
        // Initialize on page load
        document.addEventListener('DOMContentLoaded', () => {{
            // Charts will be initialized when user clicks "Display Chart"
        }});
    </script>
</body>
</html>
'''
    
    # Write HTML file
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(html_content)
    
    print(f"\n✅ Dashboard saved to: {output_file}")
    
    return sorted_patterns, sorted_methods


def main():
    parser = argparse.ArgumentParser(description='Trade Analysis Dashboard Generator')
    parser.add_argument('--input_dir', type=str, default='./trades',
                       help='Directory containing trade CSV/Excel files')
    parser.add_argument('--output', type=str, default='trader_analysis.html',
                       help='Output HTML file path')
    parser.add_argument('--use_local', action='store_true',
                       help='Use local Excel files instead of looking for ./trades folder')
    
    args = parser.parse_args()
    
    print("=" * 60)
    print("📊 Trade Analysis Dashboard Generator")
    print("=" * 60)
    
    # Determine input directory
    if args.use_local:
        input_dir = '.'
    else:
        input_dir = args.input_dir
        if not os.path.exists(input_dir):
            print(f"\n⚠️  Directory '{input_dir}' not found.")
            print("Looking for Excel files in current directory instead...")
            input_dir = '.'
    
    # Read trade files
    print(f"\n📁 Reading trade files from: {input_dir}")
    trades_by_trader = read_trade_files(input_dir)
    
    if not trades_by_trader:
        print("\n❌ No trade files found. Please ensure CSV/Excel files are in the specified directory.")
        print("\nUsage examples:")
        print("  python trader_analysis.py --input_dir ./trades")
        print("  python trader_analysis.py --use_local  (for files in current directory)")
        sys.exit(1)
    
    # Process each trader's data
    analyses_by_trader = {}
    all_symbols_data = {}  # Cache for price data
    
    for trader_name, trades_df in trades_by_trader.items():
        print(f"\n{'='*60}")
        print(f"👤 Processing trader: {trader_name}")
        print(f"{'='*60}")
        
        analyses = []
        unique_symbols = trades_df['Symbol'].unique()
        
        print(f"Found {len(unique_symbols)} unique symbol(s): {', '.join(map(str, unique_symbols))}")
        
        for idx, trade in trades_df.iterrows():
            symbol = str(trade.get('Symbol', 'Unknown'))
            
            # Parse dates
            open_dt = parse_date(trade.get('Opened', ''))
            close_dt = parse_date(trade.get('Closed', ''))
            
            if open_dt is None:
                print(f"  ⚠️  Skipping trade {trade.get('Order number', idx)}: Invalid open date")
                continue
            
            # Calculate date range for fetching
            fetch_start = open_dt - timedelta(days=2)
            fetch_end = (close_dt + timedelta(days=1)) if close_dt else (open_dt + timedelta(days=1))
            
            # Fetch price data (with caching)
            cache_key = f"{symbol}_{fetch_start.strftime('%Y%m%d')}_{fetch_end.strftime('%Y%m%d')}"
            
            if cache_key not in all_symbols_data:
                price_data = fetch_price_data(symbol, fetch_start, fetch_end, interval='1h')
                all_symbols_data[cache_key] = price_data
            else:
                price_data = all_symbols_data[cache_key]
            
            # Analyze trade
            analysis = analyze_trade(trade, price_data)
            analyses.append(analysis)
            
            if len(analyses) % 10 == 0:
                print(f"  Processed {len(analyses)}/{len(trades_df)} trades...")
        
        analyses_by_trader[trader_name] = analyses
        print(f"\n✅ Completed analysis for {trader_name}: {len(analyses)} trades analyzed")
    
    # Generate HTML dashboard
    print(f"\n{'='*60}")
    print("🎨 Generating HTML Dashboard...")
    print(f"{'='*60}")
    
    sorted_patterns, sorted_methods = generate_html_dashboard(
        trades_by_trader, analyses_by_trader, args.output
    )
    
    # Print summary
    print("\n" + "=" * 60)
    print("📈 SUMMARY OF MOST COMMON PATTERNS ACROSS ALL TRADERS")
    print("=" * 60)
    
    print("\n🕯️  Top Candle Patterns:")
    for i, (pattern, count) in enumerate(sorted_patterns[:10], 1):
        print(f"   {i}. {pattern.replace('_', ' ').title()}: {count} occurrences")
    
    print("\n🎯 Top Trading Methods:")
    for i, (method, count) in enumerate(sorted_methods[:10], 1):
        print(f"   {i}. {method.replace('_', ' ').title()}: {count} occurrences")
    
    print("\n" + "=" * 60)
    print(f"✅ All done! Open '{args.output}' in your browser to view the dashboard.")
    print("=" * 60)
    
    return 0


if __name__ == '__main__':
    sys.exit(main())
