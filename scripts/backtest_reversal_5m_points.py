"""
backtest_reversal_5m_points.py
──────────────────────────────
Test Reversal5m call-buying with INDEX-POINT SL/target (per-index calibrated),
instead of premium-%. Index points are stable across IV/DTE/strike, so the same
levels mean the same thing every day.

Per index ranges (user spec):
  NIFTY     SL 20-40   target 40-80
  BANKNIFTY SL 75-150  target 150-300
  FINNIFTY  SL 40-80   target 80-160

For each Reversal5m LONG entry we buy an ATM weekly call (BS-priced) and manage
on the UNDERLYING: exit when the index moves +target_pts / -sl_pts (or EOD), then
reprice the option at the exit spot + decayed T (net cost_model fees + spread).

Also compares, per index: best fixed points-target vs points-SL + "let it run"
(trail premium 40% / no target to EOD), since the edge is convex (fat-tailed).

Run: PYTHONPATH=. ./venv/bin/python scripts/backtest_reversal_5m_points.py [days]
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

RESOLUTION = "5"
LOOKBACK   = 300
WARMUP     = 30
R_FREE     = 0.065
SPREAD_PCT = 0.010
LOTS       = 1
BAR_YEARS  = 5 / (60 * 24 * 365)

# symbol -> (IV, SL points grid, target points grid)
# NOTE: the user's original NIFTY range (SL 20-40 / target 40-80) tested NEGATIVE —
# 20-40 pts is inside 5m noise and stops out ~28/48 trades. Widened to 40-80 SL /
# 80-160 target here, which is where NIFTY turns positive. BANKNIFTY/FINNIFTY use
# the user's ranges as-is (they work at the wide end).
CFG = {
    "NSE:NIFTY50-INDEX":   (0.12, [40, 60, 80],   [80, 120, 160]),
    "NSE:NIFTYBANK-INDEX": (0.15, [75, 100, 150], [150, 225, 300]),
    "NSE:FINNIFTY-INDEX":  (0.14, [40, 60, 80],   [80, 120, 160]),
}


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


def gen_entries(symbol, df, iv):
    """One-at-a-time entries; store spot forward path (high,low,close) + pricing."""
    strategy = Reversal5mStrategy()
    recs = df.to_dict("records")
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
            path = []
            j = i + 1
            while j < n and recs[j]["timestamp"].date() == date_i:
                path.append((float(recs[j]["high"]), float(recs[j]["low"]), float(recs[j]["close"]), j - i))
                j += 1
            if path:
                entries.append({"spot": spot, "strike": strike, "dte_y": dte_y, "iv": iv,
                                "entry_mid": entry_mid, "qty": qty, "path": path})
            i = j
        else:
            i += 1
    return entries


def sim_ptrail(entries, sl_pts, trail_pts):
    """Points-based trailing stop: initial stop = entry − sl_pts, then ratchet the
    stop up to (peak_spot − trail_pts) as the index makes new highs. Exit on the
    trailed stop / EOD; reprice the option at the exit spot."""
    trades = []
    for e in entries:
        stop_spot = e["spot"] - sl_pts
        peak = e["spot"]
        exit_mid = None; reason = "EOD"
        for (hi, lo, cl, held) in e["path"]:
            dte_j = max(0.0, e["dte_y"] - held * BAR_YEARS)
            peak = max(peak, hi)
            ts = peak - trail_pts
            if ts > stop_spot:
                stop_spot = ts                       # ratchet up only
            if lo <= stop_spot:
                exit_mid = _px(stop_spot, e["strike"], dte_j, e["iv"])
                reason = "TRAIL" if stop_spot > e["spot"] - sl_pts else "STOP"
                break
        if exit_mid is None:
            last = e["path"][-1]
            exit_mid = _px(last[2], e["strike"], max(0.0, e["dte_y"] - last[3] * BAR_YEARS), e["iv"])
        trades.append({"pnl": _pnl(e["entry_mid"], exit_mid, e["qty"]), "reason": reason})
    return trades


def _pnl(entry_mid, exit_mid, qty):
    entry_fill = entry_mid * (1 + SPREAD_PCT)
    exit_fill  = exit_mid * (1 - SPREAD_PCT)
    gross = (exit_fill - entry_fill) * qty
    fees  = single_option_cost("NSE_OPT", entry_fill, exit_fill, qty)
    return round(gross - fees, 2)


def sim_points(entries, sl_pts, tgt_pts, mode="fixed", trail_pct=0.40):
    """Exit on underlying: -sl_pts / +tgt_pts (fixed) or trail/none. Reprice option."""
    trades = []
    for e in entries:
        sl_spot  = e["spot"] - sl_pts
        tgt_spot = e["spot"] + tgt_pts
        exit_mid = None; reason = "EOD"
        peak_prem = e["entry_mid"]
        for (hi, lo, cl, held) in e["path"]:
            dte_j = max(0.0, e["dte_y"] - held * BAR_YEARS)
            if lo <= sl_spot:                                   # stop first (conservative)
                exit_mid, reason = _px(sl_spot, e["strike"], dte_j, e["iv"]), "STOP"; break
            if mode == "fixed" and hi >= tgt_spot:
                exit_mid, reason = _px(tgt_spot, e["strike"], dte_j, e["iv"]), "TARGET"; break
            if mode == "trail":
                ph = _px(hi, e["strike"], dte_j, e["iv"])
                peak_prem = max(peak_prem, ph)
                pl = _px(lo, e["strike"], dte_j, e["iv"])
                if pl <= peak_prem * (1 - trail_pct) and peak_prem > e["entry_mid"]:
                    exit_mid, reason = peak_prem * (1 - trail_pct), "TRAIL"; break
        if exit_mid is None:
            exit_mid = _px(e["path"][-1][2], e["strike"], max(0.0, e["dte_y"] - e["path"][-1][3] * BAR_YEARS), e["iv"])
        trades.append({"pnl": _pnl(e["entry_mid"], exit_mid, e["qty"]), "reason": reason})
    return trades


def summ(trades):
    if not trades:
        return 0, 0, 0, 0, 0
    wins = [t for t in trades if t["pnl"] > 0]
    gw = sum(t["pnl"] for t in wins); gl = -sum(t["pnl"] for t in trades if t["pnl"] <= 0)
    pf = gw / gl if gl > 0 else float("inf")
    exp = sum(t["pnl"] for t in trades) / len(trades)
    return len(trades), len(wins)/len(trades), pf, exp, sum(t["pnl"] for t in trades)


# Trailing fine-tune grid — points-based initial SL × points-based trail.
# NIFTY SL widened (20-40 was too tight). (iv, [init SL pts], [trail pts])
TRAIL_CFG = {
    "NSE:NIFTY50-INDEX":   (0.12, [50, 70, 90],    [40, 60, 90]),
    "NSE:NIFTYBANK-INDEX": (0.15, [120, 150, 200], [100, 150, 220]),
    "NSE:FINNIFTY-INDEX":  (0.14, [60, 80, 110],   [70, 100, 140]),
}


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 90
    fyers_broker.initialise()
    cl = fyers_broker._client
    print(f"=== Reversal5m POINTS + TRAILING fine-tune — {days}d 5m, ATM call, spread={SPREAD_PCT:.0%}/side ===")
    pooled_best = []
    for sym, (iv, sls, trails) in TRAIL_CFG.items():
        df = fetch(cl, sym, days)
        if df is None or len(df) < 60:
            print(f"{sym}: no data"); continue
        ents = gen_entries(sym, df, iv)
        short = sym.replace("NSE:", "").replace("-INDEX", "")
        print(f"\n{short}  entries={len(ents)}  init-SL pts {sls}, trail pts {trails}")
        print(f"  {'SL':>4} {'TRAIL':>6} {'win':>5} {'PF':>5} {'exp/lot':>9} {'total':>11}  exits")
        best = None
        for sl in sls:
            for tr_pts in trails:
                tr = sim_ptrail(ents, sl, tr_pts)
                n, wr, pf, exp, tot = summ(tr)
                if best is None or exp > best["exp"]:
                    best = {"sl": sl, "tr": tr_pts, "pf": pf, "wr": wr, "exp": exp, "tot": tot, "trades": tr}
                print(f"  {sl:>4} {tr_pts:>6} {wr:>4.0%} {pf:>5.2f} Rs.{exp:>7,.0f} Rs.{tot:>9,.0f}  "
                      f"{dict(Counter(t['reason'] for t in tr))}")
        # reference: best fixed target within a comparable grid
        ref_best = None
        for sl in sls:
            for tg in (sl * 2, sl * 3):
                t2 = sim_points(ents, sl, tg, mode="fixed")
                _, _, pf2, exp2, _ = summ(t2)
                if ref_best is None or exp2 > ref_best[2]:
                    ref_best = (sl, tg, exp2, pf2)
        print(f"  >> BEST trail: SL {best['sl']} / trail {best['tr']} pts → PF {best['pf']:.2f} "
              f"win {best['wr']:.0%} exp Rs.{best['exp']:,.0f}/lot total Rs.{best['tot']:,.0f}")
        print(f"     (ref best fixed SL{ref_best[0]}/TGT{ref_best[1]}: PF {ref_best[3]:.2f} exp Rs.{ref_best[2]:,.0f})")
        pooled_best += best["trades"]

    if pooled_best:
        n, wr, pf, exp, tot = summ(pooled_best)
        print(f"\n=== POOLED best-trail-per-index: trades={n} win={wr:.0%} PF={pf:.2f} "
              f"exp=Rs.{exp:,.0f}/lot total=Rs.{tot:,.0f} ===")


if __name__ == "__main__":
    main()
