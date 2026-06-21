"""
diagnose_equity.py
──────────────────
Diagnose WHY the NSE equity strategies (TrendFollow, MeanReversion) lose, by
running them through the BacktestEngine on ~180d of 1H data and dumping the trade
anatomy: exit-reason distribution, avg P&L per reason, avg winner vs avg loser,
holding time. This reveals the flaw (false breakouts? stops too tight? targets
never hit? timeouts dominate?) behind the B3 'negative expectancy' result.

Run: PYTHONPATH=. ./venv/bin/python scripts/diagnose_equity.py [days]
"""
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

IST = ZoneInfo("Asia/Kolkata")

from execution.fyers_broker import fyers_broker
from backtesting.backtest_engine import BacktestEngine
from strategies.trend_follow import TrendFollowStrategy
from strategies.mean_reversion import MeanReversionStrategy

SYMBOLS = ["NSE:RELIANCE-EQ", "NSE:TCS-EQ", "NSE:HDFCBANK-EQ", "NSE:INFY-EQ",
           "NSE:ICICIBANK-EQ", "NSE:SBIN-EQ", "NSE:AXISBANK-EQ", "NSE:LT-EQ",
           "NSE:BHARTIARTL-EQ", "NSE:HCLTECH-EQ", "NSE:MARUTI-EQ", "NSE:BAJFINANCE-EQ"]


def fetch(cl, symbol, days, resolution="60"):
    end = datetime.now(tz=IST); frames = []; rem = days; ce = end
    while rem > 0:
        span = min(85, rem); cs = ce - timedelta(days=span)
        r = cl.history(data={"symbol": symbol, "resolution": resolution, "date_format": "1",
                             "range_from": cs.strftime("%Y-%m-%d"), "range_to": ce.strftime("%Y-%m-%d"),
                             "cont_flag": "1"})
        if isinstance(r, dict) and r.get("s") == "ok" and r.get("candles"):
            frames.append(pd.DataFrame(r["candles"], columns=["timestamp", "open", "high", "low", "close", "volume"]))
        ce = cs - timedelta(days=1); rem -= span
    if not frames:
        return None
    df = pd.concat(frames, ignore_index=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.tz_convert(IST)
    return df.drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)


def anatomy(name, trades):
    if not trades:
        print(f"\n{name}: 0 trades"); return
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    gw = sum(t.pnl for t in wins); gl = -sum(t.pnl for t in losses)
    pf = gw / gl if gl > 0 else float("inf")
    avg_w = gw / len(wins) if wins else 0
    avg_l = -gl / len(losses) if losses else 0
    print(f"\n=== {name}: {len(trades)} trades | win {len(wins)/len(trades):.0%} | PF {pf:.2f} "
          f"| expectancy Rs.{sum(t.pnl for t in trades)/len(trades):,.0f} ===")
    print(f"  avg winner Rs.{avg_w:,.0f}  avg loser Rs.{avg_l:,.0f}  (win/loss ratio {abs(avg_w/avg_l) if avg_l else 0:.2f})")
    print(f"  avg holding days: {sum(t.holding_days for t in trades)/len(trades):.1f}")
    by = defaultdict(list)
    for t in trades:
        by[t.exit_reason].append(t.pnl)
    print(f"  exits by reason (count | total P&L | avg):")
    for r, pnls in sorted(by.items(), key=lambda kv: -len(kv[1])):
        print(f"    {r:12} {len(pnls):4} | Rs.{sum(pnls):>10,.0f} | avg Rs.{sum(pnls)/len(pnls):>8,.0f}")


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 180
    fyers_broker.initialise()
    cl = fyers_broker._client
    eng = BacktestEngine()
    print(f"=== Equity loss diagnostic — {days}d 1H, {len(SYMBOLS)} symbols ===")
    for label, cls in (("TrendFollow", TrendFollowStrategy), ("MeanReversion", MeanReversionStrategy)):
        all_trades = []
        for sym in SYMBOLS:
            df = fetch(cl, sym, days)
            if df is None or len(df) < 80:
                continue
            res = eng.run(sym, df, cls(), timeframe="1H", warmup_bars=60)
            all_trades += res.trades
        anatomy(label, all_trades)


if __name__ == "__main__":
    main()
