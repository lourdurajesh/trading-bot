"""
backtest_equity_exit.py
───────────────────────
Test whether 'let winners run' fixes the NSE equity strategies (TrendFollow,
MeanReversion). Generates each strategy's entries (via the real strategy, patched
store) on 1H data, then compares two exits on the STOCK (directional, engine-style
costs):
  FIXED : stop at signal.stop_loss, target at signal.target_1, max-hold timeout.
  TRAIL : stop at signal.stop_loss, then ATR-chandelier trail (no fixed target) —
          let winners run.

Run: PYTHONPATH=. ./venv/bin/python scripts/backtest_equity_exit.py [days] [atr_mult]
"""
import sys
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

IST = ZoneInfo("Asia/Kolkata")

from analysis.indicators import atr as calc_atr
from data.data_store import DataStore
from execution.fyers_broker import fyers_broker
from strategies.trend_follow import TrendFollowStrategy
from strategies.mean_reversion import MeanReversionStrategy

SYMBOLS = ["NSE:RELIANCE-EQ", "NSE:TCS-EQ", "NSE:HDFCBANK-EQ", "NSE:INFY-EQ",
           "NSE:ICICIBANK-EQ", "NSE:SBIN-EQ", "NSE:AXISBANK-EQ", "NSE:LT-EQ",
           "NSE:BHARTIARTL-EQ", "NSE:HCLTECH-EQ"]
SLIP, BROK, STT = 0.05, 0.03, 0.025   # % — engine cost model
WARMUP, LOOKBACK, MAX_HOLD = 60, 300, 70   # 70 1H bars ≈ 10 trading days


def fetch(cl, symbol, days, res="60"):
    end = datetime.now(tz=IST); frames = []; rem = days; ce = end
    while rem > 0:
        span = min(85, rem); cs = ce - timedelta(days=span)
        r = cl.history(data={"symbol": symbol, "resolution": res, "date_format": "1",
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


def gen(strategy, symbol, df):
    """One-at-a-time entries via the real strategy; forward 1H path + entry ATR."""
    import data.data_store as ds_module
    import strategies.base_strategy as base_mod
    import analysis.regime_detector as reg_mod
    recs = df.to_dict("records"); n = len(df); ents = []
    tfs = {strategy.timeframe}
    if hasattr(strategy, "confirm_tf"):
        tfs.add(strategy.confirm_tf)
    i = WARMUP
    while i < n:
        lo = max(0, i + 1 - LOOKBACK)
        win = recs[lo:i + 1]
        bt = DataStore()
        for tf in tfs:
            bt._candles[symbol][tf] = win
        bt._ltp[symbol] = float(recs[i]["close"])
        o_ds, o_b, o_r = ds_module.store, base_mod.store, reg_mod.store
        ds_module.store = bt; base_mod.store = bt; reg_mod.store = bt
        try:
            strategy.backtest_mode = True
            sig = strategy.evaluate(symbol)
        except Exception:
            sig = None
        finally:
            strategy.backtest_mode = False
            ds_module.store = o_ds; base_mod.store = o_b; reg_mod.store = o_r
        if sig and sig.is_valid() and sig.direction.value == "LONG":
            atr_v = float(calc_atr(pd.DataFrame(win)).iloc[-1])
            path = [(float(recs[j]["high"]), float(recs[j]["low"]), float(recs[j]["close"]))
                    for j in range(i + 1, min(n, i + 1 + MAX_HOLD))]
            if path:
                ents.append({"entry": sig.entry, "stop": sig.stop_loss, "tgt": sig.target_1,
                             "atr": atr_v, "path": path})
            # skip ahead to avoid overlapping entries (approx — next bar after a typical hold)
            i += 1
        else:
            i += 1
    return ents


def pnl(entry, exit_px, conf=1.0):
    ef = entry * (1 + SLIP / 100); xf = exit_px * (1 - SLIP / 100)
    size = 1  # per-unit R-multiple reporting; costs as % so size-agnostic
    gross = xf - ef
    cost = (ef + xf) * BROK / 100 + xf * STT / 100
    return gross - cost


def sim(ents, mode, atr_mult):
    out = []
    for e in ents:
        entry, stop, tgt, atrv = e["entry"], e["stop"], e["tgt"], e["atr"]
        risk = entry - stop
        if risk <= 0:
            continue
        peak = entry; cur_stop = stop; exit_px = None; reason = "HOLD_END"
        for (hi, lo, cl) in e["path"]:
            if mode == "trail":
                peak = max(peak, hi); cur_stop = max(cur_stop, peak - atr_mult * atrv)
            if lo <= cur_stop:
                exit_px, reason = cur_stop, ("TRAIL" if cur_stop > stop else "STOP"); break
            if mode == "fixed" and hi >= tgt:
                exit_px, reason = tgt, "TARGET"; break
        if exit_px is None:
            exit_px, reason = e["path"][-1][2], "HOLD_END"
        r_mult = (exit_px - entry) / risk
        out.append({"r": r_mult, "pnl_pct": pnl(entry, exit_px) / entry * 100, "reason": reason})
    return out


def rep(label, trades):
    if not trades:
        print(f"  {label}: no trades"); return
    wins = [t for t in trades if t["r"] > 0]
    gw = sum(t["r"] for t in wins); gl = -sum(t["r"] for t in trades if t["r"] <= 0)
    pf = gw / gl if gl > 0 else float("inf")
    print(f"  {label:6} n={len(trades):3} win={len(wins)/len(trades):.0%} PF={pf:.2f} "
          f"avgR={sum(t['r'] for t in trades)/len(trades):+.2f} totR={sum(t['r'] for t in trades):+.0f} "
          f"{dict(Counter(t['reason'] for t in trades))}")


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 365
    atr_mult = float(sys.argv[2]) if len(sys.argv) > 2 else 3.0
    fyers_broker.initialise()
    cl = fyers_broker._client
    print(f"=== Equity exit test — {days}d 1H, {len(SYMBOLS)} symbols, ATR_trail={atr_mult} (R-multiples) ===")
    for label, cls in (("TrendFollow", TrendFollowStrategy), ("MeanReversion", MeanReversionStrategy)):
        ents = []
        for sym in SYMBOLS:
            df = fetch(cl, sym, days)
            if df is None or len(df) < 80:
                continue
            ents += gen(cls(), sym, df)
        print(f"\n{label}  entries={len(ents)}")
        rep("FIXED", sim(ents, "fixed", atr_mult))
        rep("TRAIL", sim(ents, "trail", atr_mult))


if __name__ == "__main__":
    main()
