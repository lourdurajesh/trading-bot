"""
backtest_reversal_5m_options.py
───────────────────────────────
Test Reversal5m as REAL CALL-BUYING instead of trading the index directly.

For every Reversal5m LONG signal we simulate buying an ATM weekly call:
  • entry premium  = Black-Scholes(spot_entry, ATM strike, DTE, IV)
  • the trade is managed on the UNDERLYING (same exit policies as the index test:
    2R target / ATR-trailing, plus intraday EOD square-off)
  • exit premium   = Black-Scholes(spot_exit, same strike, DTE − hold, IV)
    → captures delta + gamma (convex winners) AND theta (decay while held)
  • costs          = analysis.cost_model.single_option_cost (NSE_OPT, the project's
                     single source of truth) + a bid/ask spread on the premium.

Why this matters vs the index proxy: an option BUYER's loss is capped at the
premium (no 1R full-notional stop), and winners are convex — a very different P&L
shape than buying the index with equity costs.

Pricing assumptions are explicit + tunable (IV per index, DTE model, spread).

Run: PYTHONPATH=. ./venv/bin/python scripts/backtest_reversal_5m_options.py [days] [atr_mult]
"""
import sys
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

IST = ZoneInfo("Asia/Kolkata")

from analysis.cost_model import single_option_cost
from analysis.options_engine import options_engine
from data.data_store import DataStore
from execution.fyers_broker import fyers_broker
from execution.options_executor import options_executor
from strategies.reversal_5m import Reversal5mStrategy

INDICES    = ["NSE:NIFTY50-INDEX", "NSE:NIFTYBANK-INDEX", "NSE:FINNIFTY-INDEX"]
RESOLUTION = "5"
LOOKBACK   = 300
WARMUP     = 30
R_FREE     = 0.065

# Explicit pricing assumptions (tunable). IV per index ≈ typical ATM weekly level.
IV_BY_INDEX = {"NSE:NIFTY50-INDEX": 0.12, "NSE:NIFTYBANK-INDEX": 0.15, "NSE:FINNIFTY-INDEX": 0.14}
SPREAD_PCT  = 0.010   # bid/ask: pay +1% on entry, receive −1% on exit (ATM weekly)
IV_MULT     = 1.0     # scale all IVs (stress-test cheaper/richer premiums)
LOTS        = 1       # 1 lot per trade (report per-lot economics)


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


def _dte_days(d):
    """Days to the upcoming weekly (Thursday) expiry; roll to next week on expiry day."""
    days = (3 - d.weekday()) % 7   # Thursday == 3
    return days if days != 0 else 7


def _call_price(spot, strike, dte_years, iv):
    if dte_years <= 0:
        return max(0.0, spot - strike)     # at expiry: intrinsic
    return options_engine.black_scholes(spot, strike, dte_years, R_FREE, iv, "call").price


def run_option_policy(symbol, df, policy, atr_mult=2.5):
    strategy = Reversal5mStrategy()
    recs = df.to_dict("records")
    iv   = IV_BY_INDEX.get(symbol, 0.13) * IV_MULT
    step = options_executor.get_strike_step(symbol)
    qty  = options_executor.get_lot_size(symbol) * LOTS
    trades = []
    pos = None
    n = len(df)
    BAR_YEARS = 5 / (60 * 24 * 365)   # one 5m bar in years (for theta)

    for i in range(WARMUP, n):
        bar = recs[i]
        date_i = bar["timestamp"].date()
        next_date = recs[i + 1]["timestamp"].date() if i + 1 < n else None
        eod = (next_date != date_i)

        if pos:
            hi, lo, cl = float(bar["high"]), float(bar["low"]), float(bar["close"])
            pos["peak"] = max(pos["peak"], hi)
            pos["held"] += 1
            exit_spot = exit_reason = None
            if lo <= pos["stop"]:
                exit_spot, exit_reason = pos["stop"], "STOP"
            elif policy == "2R" and hi >= pos["target"]:
                exit_spot, exit_reason = pos["target"], "TARGET2R"
            elif policy == "TRAIL":
                trail = pos["peak"] - atr_mult * pos["atr"]
                if trail > pos["stop"]:
                    pos["stop"] = trail
                if lo <= pos["stop"]:
                    exit_spot, exit_reason = pos["stop"], "TRAIL"
            if exit_spot is None and eod:
                exit_spot, exit_reason = cl, "EOD"

            if exit_spot is not None:
                dte_exit = max(0.0, pos["dte_y"] - pos["held"] * BAR_YEARS)
                exit_prem_mid = _call_price(exit_spot, pos["strike"], dte_exit, iv)
                exit_fill = exit_prem_mid * (1 - SPREAD_PCT)        # sell at bid
                gross = (exit_fill - pos["entry_fill"]) * qty
                fees  = single_option_cost("NSE_OPT", pos["entry_fill"], exit_fill, qty)
                pnl   = round(gross - fees, 2)
                ret_pct = (exit_fill - pos["entry_fill"]) / pos["entry_fill"] if pos["entry_fill"] > 0 else 0
                trades.append({"pnl": pnl, "ret": ret_pct, "reason": exit_reason,
                               "entry_prem": pos["entry_fill"], "exit_prem": exit_fill})
                pos = None

        if pos is None and not eod:
            lo_w = max(0, i + 1 - LOOKBACK)
            sig = _signal_at(strategy, symbol, recs[lo_w:i + 1], float(bar["close"]))
            if sig and sig.is_valid() and sig.direction.value == "LONG":
                spot = sig.entry
                risk = spot - sig.stop_loss
                if risk <= 0:
                    continue
                strike = round(spot / step) * step
                dte_y  = _dte_days(date_i) / 365.0
                entry_mid  = _call_price(spot, strike, dte_y, iv)
                if entry_mid <= 0.5:
                    continue
                entry_fill = entry_mid * (1 + SPREAD_PCT)           # buy at ask
                pos = {"strike": strike, "entry_fill": entry_fill, "dte_y": dte_y,
                       "stop": sig.stop_loss, "target": spot + 2 * risk,
                       "atr": sig.meta.get("atr", risk), "peak": spot, "held": 0}
    return trades


def report(label, trades):
    if not trades:
        print(f"  {label}: no trades"); return
    wins = [t for t in trades if t["pnl"] > 0]
    gw = sum(t["pnl"] for t in wins); gl = -sum(t["pnl"] for t in trades if t["pnl"] <= 0)
    pf = gw / gl if gl > 0 else float("inf")
    exp = sum(t["pnl"] for t in trades) / len(trades)
    avg_w = (sum(t["pnl"] for t in wins) / len(wins)) if wins else 0
    losers = [t for t in trades if t["pnl"] <= 0]
    avg_l = (sum(t["pnl"] for t in losers) / len(losers)) if losers else 0
    print(f"  {label:6} trades={len(trades):3} win={len(wins)/len(trades):.0%} PF={pf:.2f} "
          f"exp=Rs.{exp:>8,.0f} total=Rs.{sum(t['pnl'] for t in trades):>11,.0f} "
          f"avgW=Rs.{avg_w:,.0f} avgL=Rs.{avg_l:,.0f}")
    print(f"         exits={dict(Counter(t['reason'] for t in trades))}")


def main():
    global SPREAD_PCT, IV_MULT
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 90
    atr_mult = float(sys.argv[2]) if len(sys.argv) > 2 else 2.5
    if len(sys.argv) > 3:
        SPREAD_PCT = float(sys.argv[3])
    if len(sys.argv) > 4:
        IV_MULT = float(sys.argv[4])
    fyers_broker.initialise()
    cl = fyers_broker._client
    print(f"=== Reversal5m as ATM call-buying — {days}d 5m | IV(N/BN/FN)=12/15/14% "
          f"spread={SPREAD_PCT:.0%}/side lots={LOTS} trail_mult={atr_mult} ===")
    pooled = {"2R": [], "TRAIL": []}
    for sym in INDICES:
        df = fetch(cl, sym, days)
        if df is None or len(df) < 60:
            print(f"{sym}: no data"); continue
        print(f"\n{sym}  bars={len(df)}  step={options_executor.get_strike_step(sym)}  lot={options_executor.get_lot_size(sym)}")
        for policy in ("2R", "TRAIL"):
            tr = run_option_policy(sym, df, policy, atr_mult)
            pooled[policy] += tr
            report(policy, tr)
    print("\n=== POOLED (per-lot, net of BS theta + cost_model fees + spread) ===")
    for policy in ("2R", "TRAIL"):
        report(policy, pooled[policy])


if __name__ == "__main__":
    main()
