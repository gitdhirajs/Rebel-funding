"""
Rebel Funding Trading Engine
=============================
Market State → Setup Rules → Signal Generation → Execution

Core modules:
  market_state.py   — reads candles, computes S/R, trend, session, news proximity
  setup_miner.py    — clusters winning trader contexts into tradeable rules
  signal_engine.py  — matches live market against rules, generates signals
  orchestrator.py   — ties everything together, runs on schedule

Usage:
  from engine import SignalEngine
  se = SignalEngine()
  signals = se.scan(["XAU/USD", "EUR/USD"])
"""
