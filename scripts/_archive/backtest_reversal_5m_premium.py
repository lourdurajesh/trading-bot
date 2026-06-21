"""
backtest_reversal_5m_premium.py
───────────────────────────────
Find DATA-DRIVEN premium SL/target for Reversal5m call-buying, instead of the
arbitrary 30% SL / 55% target (which intraday index options rarely reach, so
trades just drift to the time stop).

Method
──────
1. Generate Reversal5m option entries (ATM weekly call, BS-priced), one position
   at a time, on 5m index data.
2. For each entry, walk the option's intraday premium path bar-by-bar to EOD
   (repricing via Black-Scholes with decaying T) and record:
     • MFE% = max favorable premium excursion (vs entry mid)
     • MAE% = max adverse  premium excursion
   → the distribution shows what's actually achievable.
3. Grid-search fixed premium SL/target pairs over the same entries (exit at
   target / SL / EOD), net of cost_model fees + bid/ask spread, and report the
   expectancy/PF of each → pick the realistic levels.

Run: PYTHONPATH=. ./venv/bin/python scripts/backtest_reversal_5m_premium.py [days]
"""
import sys
from collections import Counter
from datetime import datetime, timedelta
from statistics import median
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

INDICES    = ["NSE:NIFTY50-INDEX", "NSE:NIFTYBANK-INDEX", "NSE:FINNIFTY-INDEX"]
RESOLUTION = "5"
LOOKBACK   = 300
WARMUP     = 30
R_FREE     = 0.065
IV_BY_INDEX = {"NSE:NIFTY50-INDEX": 0.12, "NSE:NIFTYBANK-INDEX": 0.15, "NSE:FINNIFTY-INDEX": 0.14}
SPREAD_PCT = 0.010
LOTS       = 1
BAR_YEARS  = 5 / (60 * 24 * 365)


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
    days = (3 - d.weekday()) % 7
    return days if days != 0 else 7


def _px(spot, strike, dte_y, iv):
    if dte_y <= 0:
        return max(0.0, spot - strike)
    return options_engine.black_scholes(spot, strike, dte_y, R_FREE, iv, "call").price


def gen_entries(symbol, df):
    """One-at-a-time entries; for each, the forward premium path (mid) to EOD."""
    strategy = Reversal5mStrategy()
    recs = df.to_dict("records")
    iv   = IV_BY_INDEX.get(symbol, 0.13)
    step = options_executor.get_strike_step(symbol)
    qty  = options_executor.get_lot_size(symbol) * LOTS
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
        sig = _signal_at(strategy, symbol, recs[lo_w:i + 1], float(bar["close"]))
        if sig and sig.is_valid() and sig.direction.value == "LONG":
            spot   = sig.entry
            strike = round(spot / step) * step
            dte_y  = _dte_days(date_i) / 365.0
            entry_mid = _px(spot, strike, dte_y, iv)
            if entry_mid <= 0.5:
                i += 1; continue
            # Walk forward to EOD, recording premium mid at each bar high/low.
            path = []  # (prem_high, prem_low, prem_close)
            j = i + 1
            while j < n and recs[j]["timestamp"].date() == date_i:
                held = j - i
                dte_j = max(0.0, dte_y - held * BAR_YEARS)
                ph = _px(float(recs[j]["high"]), strike, dte_j, iv)
                pl = _px(float(recs[j]["low"]),  strike, dte_j, iv)
                pc = _px(float(recs[j]["close"]),strike, dte_j, iv)
                path.append((ph, pl, pc))
                j += 1
            if path:
                entries.append({"entry_mid": entry_mid, "qty": qty, "path": path})
            i = j   # resume after EOD of this trade
        else:
            i += 1
    return entries


def excursion_stats(entries):
    mfe, mae, eod = [], [], []
    for e in entries:
        em = e["entry_mid"]
        highs = [p[0] for p in e["path"]]
        lows  = [p[1] for p in e["path"]]
        mfe.append((max(highs) - em) / em * 100)
        mae.append((min(lows) - em) / em * 100)
        eod.append((e["path"][-1][2] - em) / em * 100)
    return mfe, mae, eod


def pct(arr, p):
    return float(np.percentile(arr, p)) if arr else 0.0


def sim_premium_exit(entries, sl_pct, tgt_pct):
    """Exit at +tgt_pct / -sl_pct of entry mid, else EOD. Net of spread + fees."""
    trades = []
    for e in entries:
        em, qty = e["entry_mid"], e["qty"]
        entry_fill = em * (1 + SPREAD_PCT)
        tgt_lvl = em * (1 + tgt_pct)
        sl_lvl  = em * (1 - sl_pct)
        exit_mid = None; reason = "EOD"
        for (ph, pl, pc) in e["path"]:
            if pl <= sl_lvl:                 # adverse checked first (conservative)
                exit_mid, reason = sl_lvl, "SL"; break
            if ph >= tgt_lvl:
                exit_mid, reason = tgt_lvl, "TARGET"; break
        if exit_mid is None:
            exit_mid = e["path"][-1][2]      # EOD close mid
        exit_fill = exit_mid * (1 - SPREAD_PCT)
        gross = (exit_fill - entry_fill) * qty
        fees  = single_option_cost("NSE_OPT", entry_fill, exit_fill, qty)
        trades.append({"pnl": round(gross - fees, 2), "reason": reason})
    return trades


def sim_premium_trail(entries, init_sl, trail_pct):
    """Let winners run: hard SL at -init_sl, then trail a stop trail_pct below the
    peak premium once in profit. Exit at the trailed stop / EOD. Net spread+fees."""
    trades = []
    for e in entries:
        em, qty = e["entry_mid"], e["qty"]
        entry_fill = em * (1 + SPREAD_PCT)
        stop_lvl = em * (1 - init_sl)
        peak = em
        exit_mid = None; reason = "EOD"
        for (ph, pl, pc) in e["path"]:
            # trail up off the bar high, then test the bar low against the stop
            peak = max(peak, ph)
            trail = peak * (1 - trail_pct)
            if trail > stop_lvl:
                stop_lvl = trail
            if pl <= stop_lvl:
                exit_mid, reason = stop_lvl, ("TRAIL" if stop_lvl > em * (1 - init_sl) else "SL")
                break
        if exit_mid is None:
            exit_mid = e["path"][-1][2]
        exit_fill = exit_mid * (1 - SPREAD_PCT)
        gross = (exit_fill - entry_fill) * qty
        fees  = single_option_cost("NSE_OPT", entry_fill, exit_fill, qty)
        trades.append({"pnl": round(gross - fees, 2), "reason": reason})
    return trades


def summarize(trades):
    wins = [t for t in trades if t["pnl"] > 0]
    gw = sum(t["pnl"] for t in wins); gl = -sum(t["pnl"] for t in trades if t["pnl"] <= 0)
    pf = gw / gl if gl > 0 else float("inf")
    exp = sum(t["pnl"] for t in trades) / len(trades) if trades else 0
    return len(trades), (len(wins)/len(trades) if trades else 0), pf, exp, sum(t["pnl"] for t in trades)


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 90
    fyers_broker.initialise()
    cl = fyers_broker._client
    print(f"=== Reversal5m premium SL/target study — {days}d 5m, ATM call, spread={SPREAD_PCT:.0%}/side ===")

    all_entries = []
    for sym in INDICES:
        df = fetch(cl, sym, days)
        if df is None or len(df) < 60:
            print(f"{sym}: no data"); continue
        e = gen_entries(sym, df)
        all_entries += e
        print(f"  {sym}: {len(e)} entries")

    if not all_entries:
        print("No entries."); return

    mfe, mae, eod = excursion_stats(all_entries)
    print(f"\n--- Intraday premium excursion (% of entry premium, net theta), n={len(all_entries)} ---")
    print(f"  MFE  p90={pct(mfe,90):+.0f}  p75={pct(mfe,75):+.0f}  p50={pct(mfe,50):+.0f}  p25={pct(mfe,25):+.0f}")
    print(f"  MAE  p10={pct(mae,10):+.0f}  p25={pct(mae,25):+.0f}  p50={pct(mae,50):+.0f}  p75={pct(mae,75):+.0f}")
    for lvl in (15, 20, 25, 30, 40, 50, 55):
        print(f"   reach +{lvl}% MFE: {sum(1 for x in mfe if x>=lvl)/len(mfe):.0%}   "
              f"dip -{lvl}% MAE: {sum(1 for x in mae if x<=-lvl)/len(mae):.0%}")

    print(f"\n--- Grid search premium SL/target (pooled, net spread+fees) ---")
    print(f"  {'SL%':>4} {'TGT%':>5} {'trades':>7} {'win':>5} {'PF':>5} {'exp/lot':>9} {'total':>11}  exits")
    best = None
    # 999 = "no fixed target" (SL + EOD only — let it run to the close)
    for sl in (25, 30, 40, 50):
        for tgt in (40, 50, 60, 70, 80, 999):
            trades = sim_premium_exit(all_entries, sl/100, tgt/100)
            n, wr, pf, exp, tot = summarize(trades)
            exits = dict(Counter(t["reason"] for t in trades))
            if best is None or exp > best[0]:
                best = (exp, f"SL{sl}/TGT{'none' if tgt==999 else tgt}", pf, wr, tot)
            tlabel = "none" if tgt == 999 else str(tgt)
            print(f"  {sl:>4} {tlabel:>5} {n:>7} {wr:>4.0%} {pf:>5.2f} Rs.{exp:>7,.0f} Rs.{tot:>9,.0f}  {exits}")

    print(f"\n--- Premium TRAIL (init SL, then trail % below peak premium) ---")
    print(f"  {'SL%':>4} {'TRL%':>5} {'trades':>7} {'win':>5} {'PF':>5} {'exp/lot':>9} {'total':>11}  exits")
    for sl in (25, 30, 40):
        for trl in (25, 35, 50):
            trades = sim_premium_trail(all_entries, sl/100, trl/100)
            n, wr, pf, exp, tot = summarize(trades)
            if exp > best[0]:
                best = (exp, f"SL{sl}/TRAIL{trl}", pf, wr, tot)
            print(f"  {sl:>4} {trl:>5} {n:>7} {wr:>4.0%} {pf:>5.2f} Rs.{exp:>7,.0f} Rs.{tot:>9,.0f}  "
                  f"{dict(Counter(t['reason'] for t in trades))}")

    print(f"\n  BEST by expectancy: {best[1]}  → PF={best[2]:.2f} win={best[3]:.0%} "
          f"exp=Rs.{best[0]:,.0f}/lot total=Rs.{best[4]:,.0f}")
    ref = sim_premium_exit(all_entries, 0.30, 0.55)
    n, wr, pf, exp, tot = summarize(ref)
    print(f"  REFERENCE (live 30/55):  PF={pf:.2f} win={wr:.0%} exp=Rs.{exp:,.0f}/lot total=Rs.{tot:,.0f}")


if __name__ == "__main__":
    main()
