"""
backtest_reversal_tf.py
───────────────────────
Backtest the Reversal (red→green reclaim) strategy on ANY timeframe — parametrized
so we can compare 5m vs 1H. Runs it as call-buying (ATM weekly call, BS-priced,
net theta + cost_model fees + spread) with a points-based trailing stop on the
underlying, plus EOD square-off.

Also prints the index-POINT MFE/MAE excursion so the SL/trail points can be sized
to the timeframe (5m levels are far too tight for 1H).

Run: PYTHONPATH=. ./venv/bin/python scripts/backtest_reversal_tf.py [resolution] [tf_label] [bar_minutes] [days]
  e.g.  ... 60 1H 60 250      (1-hour)
        ... 5  5m 5  90       (5-minute, matches the 5m study)
"""
import sys
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

IST = ZoneInfo("Asia/Kolkata")

from analysis.cost_model import single_option_cost
from analysis.options_engine import options_engine
from data.data_store import DataStore
from execution.fyers_broker import fyers_broker
from execution.options_executor import options_executor
from strategies.reversal_5m import Reversal5mStrategy

R_FREE     = 0.065
SPREAD_PCT = 0.010
LOOKBACK   = 300
WARMUP     = 30
IV_BY_INDEX = {"NSE:NIFTY50-INDEX": 0.12, "NSE:NIFTYBANK-INDEX": 0.15, "NSE:FINNIFTY-INDEX": 0.14}

# Per-index init-SL / trail point grids, scaled per timeframe.
GRIDS = {
    "5m": {
        "NSE:NIFTY50-INDEX":   ([50, 70, 90],    [40, 60, 90]),
        "NSE:NIFTYBANK-INDEX": ([120, 150, 200], [100, 150, 220]),
        "NSE:FINNIFTY-INDEX":  ([60, 80, 110],   [70, 100, 140]),
    },
    "1H": {
        "NSE:NIFTY50-INDEX":   ([100, 150, 200], [120, 180, 260]),
        "NSE:NIFTYBANK-INDEX": ([250, 350, 500], [300, 450, 650]),
        "NSE:FINNIFTY-INDEX":  ([120, 180, 250], [150, 230, 320]),
    },
}


def fetch(cl, symbol, resolution, days):
    # Fyers caps intraday history range per request (~100d) → fetch in 85d chunks.
    end = datetime.now(tz=IST)
    frames = []
    remaining = days
    chunk_end = end
    while remaining > 0:
        span = min(85, remaining)
        chunk_start = chunk_end - timedelta(days=span)
        r = cl.history(data={"symbol": symbol, "resolution": resolution, "date_format": "1",
                             "range_from": chunk_start.strftime("%Y-%m-%d"),
                             "range_to": chunk_end.strftime("%Y-%m-%d"), "cont_flag": "0"})
        if isinstance(r, dict) and r.get("s") == "ok" and r.get("candles"):
            frames.append(pd.DataFrame(r["candles"], columns=["timestamp", "open", "high", "low", "close", "volume"]))
        else:
            print(f"  {symbol}: chunk {chunk_start.date()}→{chunk_end.date()} failed "
                  f"({r.get('message') if isinstance(r, dict) else r})")
        chunk_end = chunk_start - timedelta(days=1)
        remaining -= span
    if not frames:
        return None
    df = pd.concat(frames, ignore_index=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.tz_convert(IST)
    df = df.drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)
    return df


def _signal_at(strategy, symbol, tf_label, records, ltp):
    import data.data_store as ds_module
    import strategies.base_strategy as base_mod
    bt = DataStore()
    bt._candles[symbol][tf_label] = records
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
    days = (3 - d.weekday()) % 7
    return days if days != 0 else 7


def _px(spot, strike, dte_y, iv):
    if dte_y <= 0:
        return max(0.0, spot - strike)
    return options_engine.black_scholes(spot, strike, dte_y, R_FREE, iv, "call").price


def gen_entries(symbol, df, tf_label, iv, bar_minutes):
    strategy = Reversal5mStrategy()
    strategy.timeframe = tf_label                    # run the same logic on this TF
    recs = df.to_dict("records")
    step = options_executor.get_strike_step(symbol)
    qty  = options_executor.get_lot_size(symbol)
    bar_years = bar_minutes / (60 * 24 * 365)
    n = len(df)
    entries = []
    i = WARMUP
    while i < n:
        bar = recs[i]
        date_i = bar["timestamp"].date()
        eod_now = (i + 1 >= n) or (recs[i + 1]["timestamp"].date() != date_i)
        if eod_now:
            i += 1; continue
        lo_w = max(0, i + 1 - LOOKBACK)
        sig = _signal_at(strategy, symbol, tf_label, recs[lo_w:i + 1], float(bar["close"]))
        if sig and sig.is_valid() and sig.direction.value == "LONG":
            spot   = sig.entry
            strike = round(spot / step) * step
            dte_y  = _dte_days(date_i) / 365.0
            entry_mid = _px(spot, strike, dte_y, iv)
            if entry_mid <= 0.5:
                i += 1; continue
            path = []
            j = i + 1
            while j < n and recs[j]["timestamp"].date() == date_i:
                path.append((float(recs[j]["high"]), float(recs[j]["low"]), float(recs[j]["close"]), j - i))
                j += 1
            if path:
                entries.append({"spot": spot, "strike": strike, "dte_y": dte_y, "iv": iv,
                                "entry_mid": entry_mid, "qty": qty, "path": path,
                                "bar_years": bar_years})
            i = j
        else:
            i += 1
    return entries


def point_excursion(entries):
    """Index-point favorable/adverse move after entry (to EOD)."""
    mfe, mae = [], []
    for e in entries:
        highs = [p[0] for p in e["path"]]; lows = [p[1] for p in e["path"]]
        mfe.append(max(highs) - e["spot"])
        mae.append(min(lows) - e["spot"])
    return mfe, mae


def sim_ptrail(entries, sl_pts, trail_pts):
    trades = []
    for e in entries:
        stop_spot = e["spot"] - sl_pts
        peak = e["spot"]; by = e["bar_years"]
        exit_mid = None; reason = "EOD"
        for (hi, lo, cl, held) in e["path"]:
            dte_j = max(0.0, e["dte_y"] - held * by)
            peak = max(peak, hi)
            stop_spot = max(stop_spot, peak - trail_pts)
            if lo <= stop_spot:
                exit_mid = _px(stop_spot, e["strike"], dte_j, e["iv"])
                reason = "TRAIL" if stop_spot > e["spot"] - sl_pts else "STOP"
                break
        if exit_mid is None:
            last = e["path"][-1]
            exit_mid = _px(last[2], e["strike"], max(0.0, e["dte_y"] - last[3] * by), e["iv"])
        entry_fill = e["entry_mid"] * (1 + SPREAD_PCT)
        exit_fill  = exit_mid * (1 - SPREAD_PCT)
        gross = (exit_fill - entry_fill) * e["qty"]
        fees  = single_option_cost("NSE_OPT", entry_fill, exit_fill, e["qty"])
        trades.append({"pnl": round(gross - fees, 2), "reason": reason})
    return trades


def summ(trades):
    if not trades:
        return 0, 0, 0, 0, 0
    wins = [t for t in trades if t["pnl"] > 0]
    gw = sum(t["pnl"] for t in wins); gl = -sum(t["pnl"] for t in trades if t["pnl"] <= 0)
    pf = gw / gl if gl > 0 else float("inf")
    return len(trades), len(wins)/len(trades), pf, sum(t["pnl"] for t in trades)/len(trades), sum(t["pnl"] for t in trades)


def main():
    resolution = sys.argv[1] if len(sys.argv) > 1 else "60"
    tf_label   = sys.argv[2] if len(sys.argv) > 2 else "1H"
    bar_min    = int(sys.argv[3]) if len(sys.argv) > 3 else 60
    days       = int(sys.argv[4]) if len(sys.argv) > 4 else 250
    grids = GRIDS.get(tf_label, GRIDS["1H"])
    fyers_broker.initialise()
    cl = fyers_broker._client
    print(f"=== Reversal {tf_label} ({resolution}m) — {days}d, ATM call, spread={SPREAD_PCT:.0%}/side ===")
    pooled = []
    for sym, (sls, trails) in grids.items():
        df = fetch(cl, sym, resolution, days)
        if df is None or len(df) < 60:
            continue
        iv = IV_BY_INDEX.get(sym, 0.13)
        ents = gen_entries(sym, df, tf_label, iv, bar_min)
        short = sym.replace("NSE:", "").replace("-INDEX", "")
        mfe, mae = point_excursion(ents)
        print(f"\n{short}  entries={len(ents)}  bars={len(df)}  [{df['timestamp'].iloc[0].date()}→{df['timestamp'].iloc[-1].date()}]")
        if ents:
            print(f"  index-pt MFE p75={np.percentile(mfe,75):+.0f} p50={np.percentile(mfe,50):+.0f} | "
                  f"MAE p50={np.percentile(mae,50):+.0f} p25={np.percentile(mae,25):+.0f}")
        print(f"  {'SL':>5} {'TRAIL':>6} {'win':>5} {'PF':>5} {'exp/lot':>9} {'total':>11}  exits")
        best = None
        for sl in sls:
            for tr_pts in trails:
                tr = sim_ptrail(ents, sl, tr_pts)
                n, wr, pf, exp, tot = summ(tr)
                if best is None or exp > best["exp"]:
                    best = {"sl": sl, "tr": tr_pts, "pf": pf, "wr": wr, "exp": exp, "tot": tot, "trades": tr}
                print(f"  {sl:>5} {tr_pts:>6} {wr:>4.0%} {pf:>5.2f} Rs.{exp:>7,.0f} Rs.{tot:>9,.0f}  "
                      f"{dict(Counter(t['reason'] for t in tr))}")
        if best:
            print(f"  >> BEST: SL {best['sl']} / trail {best['tr']} → PF {best['pf']:.2f} "
                  f"win {best['wr']:.0%} exp Rs.{best['exp']:,.0f}/lot total Rs.{best['tot']:,.0f}")
            pooled += best["trades"]
    if pooled:
        n, wr, pf, exp, tot = summ(pooled)
        print(f"\n=== POOLED best-per-index ({tf_label}): trades={n} win={wr:.0%} PF={pf:.2f} "
              f"exp=Rs.{exp:,.0f}/lot total=Rs.{tot:,.0f} ===")


if __name__ == "__main__":
    main()
