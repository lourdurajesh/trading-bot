"""
seed_test_trades.py  (sandbox-only)
───────────────────────────────────
Insert representative TEST trades across every book/segment/status so the unified
dashboard (Phase V) can be validated against real-shaped data, then removed. All
ids are prefixed TEST- for surgical cleanup.

Run:  PYTHONPATH=. venv/bin/python scripts/seed_test_trades.py          # insert
      PYTHONPATH=. venv/bin/python scripts/seed_test_trades.py --clean   # remove
"""
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from config.settings import DB_PATH
from execution import ledger

IST = ZoneInfo("Asia/Kolkata")
now = datetime.now(tz=IST)
def _t(mins): return (now - timedelta(minutes=mins)).isoformat()


def clean():
    with sqlite3.connect(DB_PATH) as c:
        for tbl in ("ledger", "trades", "paper_trades"):
            try:
                c.execute(f"DELETE FROM {tbl} WHERE id LIKE 'TEST-%'")
            except Exception as e:
                print(f"  {tbl}: {e}")
    print("✓ TEST-* rows removed")


def insert():
    ledger.init()
    # ── LEARNING NSE — equity (open + closed) ──
    ledger.record("nse", {
        "id": "TEST-LRN-EQ-OPEN", "symbol": "NSE:ABCAPITAL-EQ", "strategy": "TrendFollow",
        "direction": "LONG", "entry_price": 381.79, "stop_loss": 376.87, "target": 395.0,
        "pnl_pts": 0, "pnl_r": 0, "status": "OPEN", "entry_time": _t(120),
        "metadata": json.dumps({"instrument_type": "", "risk_pts": 4.92,
                                "original_stop": 376.87, "position_size": 1524}),
    })
    ledger.record("nse", {
        "id": "TEST-LRN-EQ-CLOSED", "symbol": "NSE:RELIANCE-EQ", "strategy": "SimpleRSI",
        "direction": "LONG", "entry_price": 2900, "exit_price": 2882, "stop_loss": 2882,
        "target": 2950, "pnl_pts": -18, "pnl_r": -0.9, "status": "CLOSED",
        "exit_reason": "STOP", "entry_time": _t(300), "exit_time": _t(180), "fees": 40,
        "metadata": json.dumps({"instrument_type": "", "risk_pts": 20, "position_size": 375}),
    })
    # ── LEARNING NSE — index options (open + closed) ──
    ledger.record("nse", {
        "id": "TEST-LRN-OPT-OPEN", "symbol": "NSE:NIFTY50-INDEX", "strategy": "DirectionalOptions",
        "direction": "LONG", "entry_price": 150.0, "stop_loss": 110.0, "target": 230.0,
        "status": "OPEN", "entry_time": _t(90),
        "metadata": json.dumps({"instrument_type": "nse_options", "nfo_symbol": "NSE:NIFTY24100CE",
                                "lot_size": 75, "underlying": "NSE:NIFTY50-INDEX",
                                "entry_spot": 24000, "sl_pts": 40, "trail_pts": 25,
                                "exit_mode": "underlying_trail", "position_size": 75}),
    })
    ledger.record("nse", {
        "id": "TEST-LRN-OPT-CLOSED", "symbol": "NSE:FINNIFTY-INDEX", "strategy": "Reversal3m",
        "direction": "LONG", "entry_price": 198.0, "exit_price": 240.0, "target": 240.0,
        "pnl_pts": 42.0, "pnl_r": 1.4, "status": "CLOSED", "exit_reason": "TARGET",
        "entry_time": _t(400), "exit_time": _t(260), "fees": 50,
        "metadata": json.dumps({"instrument_type": "nse_options", "nfo_symbol": "NSE:FINNIFTY26650CE",
                                "lot_size": 60, "position_size": 60}),
    })
    # ── LEARNING MCX — spread (open + closed) ──
    ledger.record("mcx", {
        "id": "TEST-LRN-MCX-OPEN", "symbol": "MCX:CRUDEOIL26JULFUT", "instrument": "CRUDEOIL",
        "strategy": "TrendSpread", "direction": "LONG", "spot_at_entry": 5800,
        "net_debit": 120, "lots": 2, "lot_size": 100, "rr": 1.8, "status": "OPEN",
        "entry_time": _t(75), "pnl_approx": 0, "pnl_r": 0,
    })
    ledger.record("mcx", {
        "id": "TEST-LRN-MCX-CLOSED", "symbol": "MCX:NATURALGAS26JUNFUT", "instrument": "NATURALGAS",
        "strategy": "BreakoutSpread", "direction": "LONG", "spot_at_entry": 250,
        "net_debit": 90, "lots": 1, "lot_size": 1250, "status": "CLOSED",
        "exit_reason": "STOP", "pnl_approx": -11250, "pnl_r": -1.0,
        "entry_time": _t(500), "exit_time": _t(350), "fees": 60,
    })
    # ── LEARNING US (closed) ──
    ledger.record("us", {
        "id": "TEST-LRN-US-CLOSED", "symbol": "SPY", "strategy": "Reversal", "status": "CLOSED",
        "entry_time": _t(800), "entry_spot": 540, "strike": 540, "entry_premium": 5.0,
        "exit_time": _t(700), "exit_spot": 543, "exit_premium": 6.2, "pnl": 120,
        "exit_reason": "TARGET",
    })
    # ── MAIN BOOK (LIVE/PAPER) — trades table (open + closed) ──
    with sqlite3.connect(DB_PATH) as c:
        c.execute("""INSERT OR REPLACE INTO trades
            (id,symbol,strategy,direction,signal_type,hold_type,entry_price,exit_price,
             stop_loss,target_1,target_2,position_size,capital_at_risk,realised_pnl,
             status,exit_reason,entry_time,exit_time) VALUES
            ('TEST-MAIN-EQ-OPEN','NSE:TATAMOTORS-EQ','TrendFollow','LONG','EQUITY','intraday',
             950,0,938,980,0,100,1200,0,'OPEN','',?, ''),
            ('TEST-MAIN-EQ-CLOSED','NSE:INFY-EQ','TrendFollow','LONG','EQUITY','intraday',
             1500,1530,1485,1530,0,80,1200,2360,'CLOSED','TARGET',?,?)""",
            (_t(100), _t(600), _t(420)))
    print("✓ inserted TEST trades across nse(eq/opt), mcx, us, main book")


if __name__ == "__main__":
    if "--clean" in sys.argv:
        clean()
    else:
        insert()
