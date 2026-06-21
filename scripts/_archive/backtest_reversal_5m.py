"""
backtest_reversal_5m.py
───────────────────────
One-off backtest for the Reversal5m strategy on ~3 months of 5-minute INDEX data.

Fetches 5m history for NIFTY / BANKNIFTY / FINNIFTY straight from the Fyers history
API (single request covers ~100 days of 5m), replays it through the BacktestEngine,
and prints per-index + pooled performance.

Run:  PYTHONPATH=. ./venv/bin/python scripts/backtest_reversal_5m.py [days]
"""
import sys
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

IST = ZoneInfo("Asia/Kolkata")

from execution.fyers_broker import fyers_broker
from backtesting.backtest_engine import BacktestEngine
from backtesting.performance import compute_metrics
from strategies.reversal_5m import Reversal5mStrategy

INDICES = ["NSE:NIFTY50-INDEX", "NSE:NIFTYBANK-INDEX", "NSE:FINNIFTY-INDEX"]
RESOLUTION = "5"   # 5-minute


def fetch(cl, symbol: str, days: int):
    end = datetime.now(tz=IST)
    start = end - timedelta(days=days)
    data = {
        "symbol": symbol, "resolution": RESOLUTION, "date_format": "1",
        "range_from": start.strftime("%Y-%m-%d"), "range_to": end.strftime("%Y-%m-%d"),
        "cont_flag": "0",
    }
    r = cl.history(data=data)
    if not isinstance(r, dict) or r.get("s") != "ok" or not r.get("candles"):
        print(f"  {symbol}: fetch failed — {r.get('message') if isinstance(r, dict) else r}")
        return None
    df = pd.DataFrame(r["candles"], columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.tz_convert(IST)
    return df


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 90
    fyers_broker.initialise()
    cl = fyers_broker._client
    engine = BacktestEngine()

    print(f"=== Reversal5m backtest — {days}d of 5m index candles ===")
    pooled = []
    for sym in INDICES:
        df = fetch(cl, sym, days)
        if df is None or len(df) < 60:
            continue
        res = compute_metrics(engine.run(sym, df, Reversal5mStrategy(), timeframe="5m", warmup_bars=30))
        pooled += res.trades
        print(f"\n{sym}  [{res.start_date} → {res.end_date}]  bars={len(df)}")
        print(f"  trades={res.total_trades}  win={res.win_rate:.0%}  PF={res.profit_factor:.2f}  "
              f"exp=Rs.{res.expectancy:,.0f}  return={res.total_return_pct:+.1f}%  maxDD={res.max_drawdown_pct:.1f}%")
        if res.trades:
            print(f"  exits: {dict(Counter(t.exit_reason for t in res.trades))}")
            print(f"  avg_winner=Rs.{res.avg_winner:,.0f}  avg_loser=Rs.{res.avg_loser:,.0f}")

    if pooled:
        wins = [t for t in pooled if t.pnl > 0]
        gross_w = sum(t.pnl for t in wins)
        gross_l = -sum(t.pnl for t in pooled if t.pnl <= 0)
        pf = (gross_w / gross_l) if gross_l > 0 else float("inf")
        exp = sum(t.pnl for t in pooled) / len(pooled)
        total = sum(t.pnl for t in pooled)
        print(f"\n=== POOLED (all 3 indices) ===")
        print(f"  trades={len(pooled)}  win={len(wins)/len(pooled):.0%}  PF={pf:.2f}  "
              f"exp=Rs.{exp:,.0f}  total_pnl=Rs.{total:,.0f}")
        print(f"  exits: {dict(Counter(t.exit_reason for t in pooled))}")
    else:
        print("\nNo trades generated.")


if __name__ == "__main__":
    main()
