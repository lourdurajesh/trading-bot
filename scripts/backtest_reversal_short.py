"""
backtest_reversal_short.py
──────────────────────────
Explore the REVERSE pattern for Reversal5m — both as a long EXIT and as a SHORT
entry — on 5m index data, priced as option-buying (ATM call for longs, ATM put for
shorts), BS-repriced, net cost_model fees + spread, with the points-based trailing
stop.

Patterns (mirror of each other):
  BULLISH (long entry): red candle then green; green close > red open;
                        RSI in (30,70) and RISING; volume OK.
  BEARISH (short entry / long exit): green candle then red; red close < green open;
                        RSI in (30,70) and FALLING; volume OK.

Sims (90d, per index, points = the 5m-tuned init-SL/trail):
  A) LONG + trail                         (baseline)
  B) LONG + trail + reverse-pattern exit  (also exit a call when BEARISH prints)
  C) SHORT (put) + downside trail         (new direction)

Run: PYTHONPATH=. ./venv/bin/python scripts/backtest_reversal_short.py [days]
"""
import sys
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

IST = ZoneInfo("Asia/Kolkata")

from analysis.cost_model import single_option_cost
from analysis.indicators import rsi as calc_rsi, relative_volume
from analysis.options_engine import options_engine
from execution.fyers_broker import fyers_broker
from execution.options_executor import options_executor

# RESOLUTION + BAR_MIN come from argv (default 5-minute) so the same study runs
# across 3m / 5m / 15m. BAR_YEARS (for option theta) tracks the bar size.
RESOLUTION = sys.argv[2] if len(sys.argv) > 2 else "5"
BAR_MIN    = int(sys.argv[3]) if len(sys.argv) > 3 else 5
R_FREE     = 0.065
SPREAD_PCT = 0.010
WARMUP     = 30
BAR_YEARS  = BAR_MIN / (60 * 24 * 365)
RSI_LOW, RSI_HIGH, MIN_RVOL = 30.0, 70.0, 1.2

# symbol -> (iv, sl_pts, trail_pts)  [5m-tuned]
CFG = {
    "NSE:NIFTY50-INDEX":   (0.12, 70,  90),
    "NSE:NIFTYBANK-INDEX": (0.15, 150, 220),
    "NSE:FINNIFTY-INDEX":  (0.14, 80,  140),
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


def flags(df):
    """Per-bar pattern booleans.
      bull       — long entry: red then green, green close > red open, RSI rising + vol.
      bear       — mirror reversal: green then red, red close < green open, RSI falling + vol.
      bear_break — user's 'bear breakdown' exit: red candle closing below prior bar's low.
    """
    o = df["open"].values; c = df["close"].values; l = df["low"].values
    rsi = calc_rsi(df["close"]).values
    rvol = relative_volume(df).values
    vol_present = df["volume"].sum() > 0
    n = len(df)
    bull = [False] * n; bear = [False] * n; bear_break = [False] * n
    for i in range(2, n):
        vol_ok = (not vol_present) or (rvol[i] >= MIN_RVOL)
        in_band = RSI_LOW < rsi[i] < RSI_HIGH
        if (c[i-1] < o[i-1] and c[i] > o[i] and c[i] > o[i-1]
                and in_band and rsi[i] > rsi[i-1] and vol_ok):
            bull[i] = True
        if (c[i-1] > o[i-1] and c[i] < o[i] and c[i] < o[i-1]
                and in_band and rsi[i] < rsi[i-1] and vol_ok):
            bear[i] = True
        # User's breakdown: a red candle that closes below the previous candle's low.
        if c[i] < o[i] and c[i] < l[i-1]:
            bear_break[i] = True
    return bull, bear, bear_break


def px(spot, strike, dte_y, iv, kind):
    if dte_y <= 0:
        return max(0.0, (spot - strike) if kind == "call" else (strike - spot))
    return options_engine.black_scholes(spot, strike, dte_y, R_FREE, iv, kind).price


def gen(df, signal_flags, sym, iv, kind):
    """One-at-a-time entries on the given pattern; forward path (hi,lo,cl,held,idx) to EOD."""
    recs = df.to_dict("records")
    step = options_executor.get_strike_step(sym)
    qty  = options_executor.get_lot_size(sym)
    n = len(df)
    ents = []
    i = WARMUP
    while i < n:
        date_i = recs[i]["timestamp"].date()
        eod = (i + 1 >= n) or (recs[i + 1]["timestamp"].date() != date_i)
        if not eod and signal_flags[i]:
            spot = float(recs[i]["close"])
            strike = round(spot / step) * step
            dte_y = (lambda d: (d if d != 0 else 7))((3 - date_i.weekday()) % 7) / 365.0
            entry_mid = px(spot, strike, dte_y, iv, kind)
            if entry_mid <= 0.5:
                i += 1; continue
            path = []
            j = i + 1
            while j < n and recs[j]["timestamp"].date() == date_i:
                path.append((float(recs[j]["high"]), float(recs[j]["low"]),
                             float(recs[j]["close"]), j - i, j))
                j += 1
            if path:
                ents.append({"spot": spot, "strike": strike, "dte_y": dte_y, "iv": iv,
                             "kind": kind, "entry_mid": entry_mid, "qty": qty, "path": path})
            i = j
        else:
            i += 1
    return ents


def _close(entry_mid, exit_mid, qty):
    ef = entry_mid * (1 + SPREAD_PCT); xf = exit_mid * (1 - SPREAD_PCT)
    return round((xf - ef) * qty - single_option_cost("NSE_OPT", ef, xf, qty), 2)


def sim(ents, sl, trail, exit_flags=None, use_trail=True, flag_reason="REVERSE"):
    """Underlying-trailing option exit (call=trail up / put=trail down). Always keeps
    the initial hard stop. If exit_flags given, also exit at a forward bar's close
    when the flag fires (e.g. bear-breakdown). use_trail=False → hard SL + flag only."""
    trades = []
    for e in ents:
        call = e["kind"] == "call"
        if call:
            stop = e["spot"] - sl; peak = e["spot"]
        else:
            stop = e["spot"] + sl; trough = e["spot"]
        exit_mid = None; reason = "EOD"
        for (hi, lo, cl, held, idx) in e["path"]:
            dte_j = max(0.0, e["dte_y"] - held * BAR_YEARS)
            if call:
                if use_trail:
                    peak = max(peak, hi); stop = max(stop, peak - trail)
                if lo <= stop:
                    exit_mid = px(stop, e["strike"], dte_j, e["iv"], "call")
                    reason = "TRAIL" if stop > e["spot"] - sl else "STOP"; break
            else:
                if use_trail:
                    trough = min(trough, lo); stop = min(stop, trough + trail)
                if hi >= stop:
                    exit_mid = px(stop, e["strike"], dte_j, e["iv"], "put")
                    reason = "TRAIL" if stop < e["spot"] + sl else "STOP"; break
            if exit_flags is not None and exit_flags[idx]:
                exit_mid = px(cl, e["strike"], dte_j, e["iv"], e["kind"])
                reason = flag_reason; break
        if exit_mid is None:
            last = e["path"][-1]
            exit_mid = px(last[2], e["strike"], max(0.0, e["dte_y"] - last[3] * BAR_YEARS), e["iv"], e["kind"])
        trades.append({"pnl": _close(e["entry_mid"], exit_mid, e["qty"]), "reason": reason})
    return trades


def summ(trades):
    if not trades:
        return "no trades"
    wins = [t for t in trades if t["pnl"] > 0]
    gw = sum(t["pnl"] for t in wins); gl = -sum(t["pnl"] for t in trades if t["pnl"] <= 0)
    pf = gw / gl if gl > 0 else float("inf")
    exp = sum(t["pnl"] for t in trades) / len(trades)
    return (f"n={len(trades):3} win={len(wins)/len(trades):.0%} PF={pf:.2f} "
            f"exp=Rs.{exp:>7,.0f} total=Rs.{sum(t['pnl'] for t in trades):>10,.0f} "
            f"{dict(Counter(t['reason'] for t in trades))}")


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 90
    fyers_broker.initialise()
    cl = fyers_broker._client
    print(f"=== Reversal EXIT study + SHORTS — {days}d {RESOLUTION}m candles, ATM call/put, spread={SPREAD_PCT:.0%}/side ===")
    pool = {k: [] for k in ("A", "B", "C", "D", "S")}
    for sym, (iv, sl, trail) in CFG.items():
        df = fetch(cl, sym, days)
        if df is None or len(df) < 60:
            print(f"{sym}: no data"); continue
        bull, bear, bear_break = flags(df)
        longs  = gen(df, bull, sym, iv, "call")
        shorts = gen(df, bear, sym, iv, "put")
        short = sym.replace("NSE:", "").replace("-INDEX", "")
        A = sim(longs, sl, trail)                                               # trail only
        B = sim(longs, sl, trail, exit_flags=bear,       flag_reason="REVERSE") # trail + mirror reversal
        C = sim(longs, sl, trail, exit_flags=bear_break, flag_reason="BREAK")   # trail + bear-breakdown
        D = sim(longs, sl, 0, exit_flags=bear_break, use_trail=False, flag_reason="BREAK")  # hard SL + breakdown only
        S = sim(shorts, sl, trail)                                              # short puts + downside trail
        for k, v in (("A", A), ("B", B), ("C", C), ("D", D), ("S", S)):
            pool[k] += v
        print(f"\n{short}  long-entries={len(longs)}  short-entries={len(shorts)}")
        print(f"  A long trail-only        : {summ(A)}")
        print(f"  B long trail+mirrorRev   : {summ(B)}")
        print(f"  C long trail+bearBreak   : {summ(C)}")
        print(f"  D long hardSL+bearBreak  : {summ(D)}")
        print(f"  S short(put)+trail       : {summ(S)}")
    print(f"\n=== POOLED ===")
    print(f"  A long trail-only        : {summ(pool['A'])}")
    print(f"  B long trail+mirrorRev   : {summ(pool['B'])}")
    print(f"  C long trail+bearBreak   : {summ(pool['C'])}")
    print(f"  D long hardSL+bearBreak  : {summ(pool['D'])}")
    print(f"  S short(put)+trail       : {summ(pool['S'])}")
    print(f"  best-long + S (long&short): see best long row + S")


if __name__ == "__main__":
    main()
