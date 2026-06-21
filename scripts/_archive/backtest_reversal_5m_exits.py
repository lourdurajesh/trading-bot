"""
backtest_reversal_5m_exits.py
─────────────────────────────
"Let winners run" exit experiment for Reversal5m. Reuses the SAME strategy for
entries (single source of truth) but replaces the shared BacktestEngine's
half-book-then-breakeven exit with two alternatives, to see if the ~54% hit-rate
edge can be turned positive by capturing more on winners:

  A) 2R  — single hard target at entry + 2R, hard stop, intraday EOD square-off.
  B) TRAIL — ATR chandelier trailing stop (let winners run), EOD square-off.

Same slippage / brokerage / STT / risk-based sizing as backtest_engine.py so the
numbers are comparable to the baseline run.

Run:  PYTHONPATH=. ./venv/bin/python scripts/backtest_reversal_5m_exits.py [days] [atr_mult]
"""
import sys
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

IST = ZoneInfo("Asia/Kolkata")

from config.settings import RISK_PER_TRADE_PCT, TOTAL_CAPITAL
from data.data_store import DataStore
from execution.fyers_broker import fyers_broker
from strategies.reversal_5m import Reversal5mStrategy

INDICES    = ["NSE:NIFTY50-INDEX", "NSE:NIFTYBANK-INDEX", "NSE:FINNIFTY-INDEX"]
RESOLUTION = "5"
LOOKBACK   = 300
WARMUP     = 30

# Cost model — identical to backtesting/backtest_engine.py
SLIPPAGE_PCT  = 0.05
BROKERAGE_PCT = 0.03
STT_PCT       = 0.025


def fetch(cl, symbol, days):
    end = datetime.now(tz=IST); start = end - timedelta(days=days)
    r = cl.history(data={"symbol": symbol, "resolution": RESOLUTION, "date_format": "1",
                         "range_from": start.strftime("%Y-%m-%d"), "range_to": end.strftime("%Y-%m-%d"),
                         "cont_flag": "0"})
    if not isinstance(r, dict) or r.get("s") != "ok" or not r.get("candles"):
        return None
    df = pd.DataFrame(r["candles"], columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.tz_convert(IST)
    return df


def _signal_at(strategy, symbol, records, ltp):
    """Run the real strategy against a bounded window via a patched store."""
    import data.data_store as ds_module
    import strategies.base_strategy as base_mod
    bt = DataStore()
    bt._candles[symbol]["5m"] = records
    bt._ltp[symbol] = ltp
    orig_ds, orig_base = ds_module.store, base_mod.store
    ds_module.store = bt; base_mod.store = bt
    try:
        strategy.backtest_mode = True
        return strategy.evaluate(symbol)
    except Exception:
        return None
    finally:
        strategy.backtest_mode = False
        ds_module.store = orig_ds; base_mod.store = orig_base


def _close(entry_fill, exit_px, size, direction_long=True):
    """Rupee P&L with slippage already applied to fills, plus brokerage + STT."""
    exit_fill = exit_px * (1 - SLIPPAGE_PCT / 100)   # long sell side
    gross = (exit_fill - entry_fill) * size
    brokerage = (entry_fill + exit_fill) * size * BROKERAGE_PCT / 100
    stt = exit_fill * size * STT_PCT / 100
    return round(gross - brokerage - stt, 2), exit_fill


def run_policy(symbol, df, policy, atr_mult=2.5):
    strategy = Reversal5mStrategy()
    recs = df.to_dict("records")
    capital = TOTAL_CAPITAL
    trades = []
    pos = None
    n = len(df)
    for i in range(WARMUP, n):
        bar = recs[i]
        date_i = bar["timestamp"].date()
        next_date = recs[i + 1]["timestamp"].date() if i + 1 < n else None
        eod = (next_date != date_i)   # last bar of the trading day

        if pos:
            hi, lo, cl = float(bar["high"]), float(bar["low"]), float(bar["close"])
            pos["peak"] = max(pos["peak"], hi)
            exit_px = exit_reason = None
            # hard stop first (gap-safe: use stop level)
            if lo <= pos["stop"]:
                exit_px, exit_reason = pos["stop"], "STOP"
            elif policy == "2R" and hi >= pos["target"]:
                exit_px, exit_reason = pos["target"], "TARGET2R"
            elif policy == "TRAIL":
                trail = pos["peak"] - atr_mult * pos["atr"]
                if trail > pos["stop"]:
                    pos["stop"] = trail            # ratchet up only
                if lo <= pos["stop"]:
                    exit_px, exit_reason = pos["stop"], "TRAIL"
            if exit_px is None and eod:
                exit_px, exit_reason = cl, "EOD"
            if exit_px is not None:
                pnl, _ = _close(pos["entry_fill"], exit_px, pos["size"])
                R = (exit_px - pos["entry"]) / pos["risk"] if pos["risk"] else 0
                capital += pnl
                trades.append({"pnl": pnl, "R": R, "reason": exit_reason})
                pos = None

        if pos is None and not eod:
            lo_w = max(0, i + 1 - LOOKBACK)
            sig = _signal_at(strategy, symbol, recs[lo_w:i + 1], float(bar["close"]))
            if sig and sig.is_valid() and sig.direction.value == "LONG":
                entry = sig.entry
                stop  = sig.stop_loss
                risk  = entry - stop
                if risk <= 0:
                    continue
                entry_fill = entry * (1 + SLIPPAGE_PCT / 100)
                risk_amt = capital * (RISK_PER_TRADE_PCT / 100) * getattr(sig, "confidence", 1.0)
                size = int(risk_amt / (entry_fill - stop)) if (entry_fill - stop) > 0 else 0
                if size <= 0:
                    continue
                pos = {"entry": entry, "entry_fill": entry_fill, "stop": stop, "risk": risk,
                       "target": entry + 2 * risk, "atr": sig.meta.get("atr", risk),
                       "peak": entry, "size": size}
    return trades


def report(label, trades):
    if not trades:
        print(f"  {label}: no trades"); return
    wins = [t for t in trades if t["pnl"] > 0]
    gw = sum(t["pnl"] for t in wins); gl = -sum(t["pnl"] for t in trades if t["pnl"] <= 0)
    pf = gw / gl if gl > 0 else float("inf")
    avg_R = sum(t["R"] for t in trades) / len(trades)
    print(f"  {label:6} trades={len(trades):3} win={len(wins)/len(trades):.0%} "
          f"PF={pf:.2f} avgR={avg_R:+.2f} totalR={sum(t['R'] for t in trades):+.1f} "
          f"pnl=Rs.{sum(t['pnl'] for t in trades):>11,.0f}  exits={dict(Counter(t['reason'] for t in trades))}")


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 90
    atr_mult = float(sys.argv[2]) if len(sys.argv) > 2 else 2.5
    fyers_broker.initialise()
    cl = fyers_broker._client
    print(f"=== Reversal5m EXIT experiment — {days}d 5m, ATR_trail_mult={atr_mult} ===")
    pooled = {"2R": [], "TRAIL": []}
    for sym in INDICES:
        df = fetch(cl, sym, days)
        if df is None or len(df) < 60:
            print(f"{sym}: no data"); continue
        print(f"\n{sym}  bars={len(df)}")
        for policy in ("2R", "TRAIL"):
            tr = run_policy(sym, df, policy, atr_mult)
            pooled[policy] += tr
            report(policy, tr)
    print("\n=== POOLED ===")
    for policy in ("2R", "TRAIL"):
        report(policy, pooled[policy])


if __name__ == "__main__":
    main()
