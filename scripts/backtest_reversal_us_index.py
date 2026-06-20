"""
backtest_reversal_us_index.py
─────────────────────────────
US equivalent of the Indian index-options Reversal strategy. Runs the red→green
reclaim pattern on the US 'indexes' (SPY = S&P 500, QQQ = Nasdaq-100) and trades it
as ATM weekly CALL-buying — Black-Scholes priced, % trailing stop on the underlying,
EOD square-off, net of spread + per-contract commission. Mirrors
backtest_reversal_5m_options.py but for US ETFs via Alpaca (IEX feed).

Run: PYTHONPATH=. ./venv/bin/python scripts/backtest_reversal_us_index.py [days]
"""
import sys
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

ET = ZoneInfo("America/New_York")

from analysis.indicators import rsi as calc_rsi, relative_volume
from analysis.options_engine import options_engine
from execution.alpaca_broker import alpaca_broker

R_FREE    = 0.045          # US risk-free ~4.5%
SPREAD    = 0.005          # 0.5%/side — SPY/QQQ ATM weeklies are very tight
COMMISSION_PER_CONTRACT = 0.65   # each way
MULT      = 100            # US option contract = 100 shares
STRIKE_STEP = 1.0          # SPY/QQQ have $1 strikes
RSI_LOW, RSI_HIGH, MIN_RVOL, WARMUP = 30.0, 70.0, 1.2, 30
BAR_YEARS = 5 / (60 * 24 * 365)

# symbol -> assumed IV
CFG = {"SPY": 0.14, "QQQ": 0.18}
# % SL / % trail grid on the underlying
GRID = [(0.25, 0.4), (0.35, 0.6), (0.5, 0.9)]


def fetch_us(cl, sym, days):
    end = (datetime.utcnow() - timedelta(days=1)); start = end - timedelta(days=days)
    try:
        bars = cl.get_bars(sym, "5Min", start=start.strftime("%Y-%m-%d"),
                           end=end.strftime("%Y-%m-%d"), feed="iex", limit=1000000)
        rows = [(b.t, b.o, b.h, b.l, b.c, b.v) for b in bars]
    except Exception as e:
        print(f"  {sym}: fetch error {e}"); return None
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(ET)
    df = df[(df["timestamp"].dt.time >= pd.Timestamp("09:30").time()) &
            (df["timestamp"].dt.time < pd.Timestamp("16:00").time())].reset_index(drop=True)
    return df


def dte_days(d):
    """Days to the upcoming Friday weekly expiry (min 1; SPY/QQQ also have nearer, but
    use weekly as a representative theta horizon)."""
    days = (4 - d.weekday()) % 7   # Friday == 4
    return days if days != 0 else 7


def px(spot, strike, dte_y, iv):
    if dte_y <= 0:
        return max(0.0, spot - strike)
    return options_engine.black_scholes(spot, strike, dte_y, R_FREE, iv, "call").price


def sim(df, iv, sl_pct, trail_pct):
    o = df["open"].values; c = df["close"].values; h = df["high"].values; l = df["low"].values
    rsi = calc_rsi(df["close"]).values; rvol = relative_volume(df).values
    vol_present = df["volume"].sum() > 0
    dts = df["timestamp"].dt.date.values
    n = len(df); trades = []; i = WARMUP
    while i < n - 1:
        eod = dts[i + 1] != dts[i]
        bull = (i >= 2 and c[i-1] < o[i-1] and c[i] > o[i] and c[i] > o[i-1]
                and RSI_LOW < rsi[i] < RSI_HIGH and rsi[i] > rsi[i-1]
                and ((not vol_present) or rvol[i] >= MIN_RVOL))
        if bull and not eod:
            spot = c[i]; strike = round(spot / STRIKE_STEP) * STRIKE_STEP
            dte_y = dte_days(dts[i]) / 365.0
            entry_mid = px(spot, strike, dte_y, iv)
            if entry_mid <= 0.05:
                i += 1; continue
            stop = spot * (1 - sl_pct/100); peak = spot
            exit_mid = None; reason = "EOD"; j = i + 1; held = 0
            while j < n and dts[j] == dts[i]:
                held += 1; peak = max(peak, h[j]); stop = max(stop, peak * (1 - trail_pct/100))
                dte_j = max(0.0, dte_y - held * BAR_YEARS)
                if l[j] <= stop:
                    exit_mid = px(stop, strike, dte_j, iv)
                    reason = "TRAIL" if stop > spot*(1-sl_pct/100) else "STOP"; break
                j += 1
            if exit_mid is None:
                last = min(j, n-1); exit_mid = px(c[last], strike, max(0.0, dte_y - held*BAR_YEARS), iv)
            ef = entry_mid * (1 + SPREAD); xf = exit_mid * (1 - SPREAD)
            pnl = (xf - ef) * MULT - 2 * COMMISSION_PER_CONTRACT
            trades.append({"pnl": round(pnl, 2), "reason": reason})
            i = j
        else:
            i += 1
    return trades


def rep(label, trades):
    if not trades:
        print(f"  {label}: no trades"); return
    wins = [t for t in trades if t["pnl"] > 0]
    gw = sum(t["pnl"] for t in wins); gl = -sum(t["pnl"] for t in trades if t["pnl"] <= 0)
    pf = gw / gl if gl > 0 else float("inf")
    print(f"  {label:14} n={len(trades):4} win={len(wins)/len(trades):.0%} PF={pf:.2f} "
          f"exp=${sum(t['pnl'] for t in trades)/len(trades):+,.0f} tot=${sum(t['pnl'] for t in trades):+,.0f} "
          f"{dict(Counter(t['reason'] for t in trades))}")


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 80
    alpaca_broker.initialise()
    cl = alpaca_broker._client
    print(f"=== Reversal on US INDEX ETFs (ATM call-buying) — {days}d 5m, IEX, spread={SPREAD:.1%}/side ===")
    pooled = {g: [] for g in GRID}
    for sym, iv in CFG.items():
        df = fetch_us(cl, sym, days)
        if df is None or len(df) < 60:
            print(f"{sym}: no data"); continue
        print(f"\n{sym}  bars={len(df)}  [{df['timestamp'].iloc[0].date()}→{df['timestamp'].iloc[-1].date()}]  IV={iv:.0%}")
        for sl, tr in GRID:
            t = sim(df, iv, sl, tr); pooled[(sl, tr)] += t
            rep(f"SL{sl}/trail{tr}", t)
    print(f"\n=== POOLED (SPY+QQQ, per contract) ===")
    for g in GRID:
        rep(f"SL{g[0]}/trail{g[1]}", pooled[g])


if __name__ == "__main__":
    main()
