"""Quick smoke test — run once before bot launch."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config import Config
from alpaca_client import AlpacaClient
from risk_manager import RiskManager, Targets
from scanner import PremarketScanner, CatalystChecker
from strategy import mark_premarket_levels, run_setup_detectors
from trade_logger import TradeLogger, PerformanceTracker
from news_evaluator import TradingAI
from edgar import summarize as edgar_summarize
import run_bot
import api

print("All modules import OK")

cfg = Config()
rm = RiskManager(cfg)
t = rm.calculate_targets(10.0, 9.0)
assert isinstance(t, Targets), "Targets type mismatch"
assert t.t1 == rm.r1_price(10.0), "t1 should equal r1_price"
print(f"calculate_targets OK: t1={t.t1}")

client = AlpacaClient(cfg)
acct = client.get_account()
equity = float(acct.equity)
bp = float(acct.buying_power)
print(f"Alpaca account OK: equity=${equity:,.2f}  buying_power=${bp:,.2f}")

print("All checks PASSED — ready to run bot")
