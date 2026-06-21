"""
backtest_reversal_stocks.py
───────────────────────────
Test the proven Reversal pattern (red→green reclaim, RSI 30-70 rising, volume) on
individual STOCKS — NSE (Fyers) and US (Alpaca IEX) — traded DIRECTIONALLY (buy the
stock; stocks are tradeable, no options needed). Percent-based SL + trailing stop,
EOD square-off. Reports per market so we can see if the index edge carries to names.

Run: PYTHONPATH=. ./venv/bin/python scripts/backtest_reversal_stocks.py [nse_days] [us_days]
"""
import sys
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

IST = ZoneInfo("Asia/Kolkata")
ET  = ZoneInfo("America/New_York")

from analysis.indicators import rsi as calc_rsi, relative_volume
from execution.fyers_broker import fyers_broker
from execution.alpaca_broker import alpaca_broker

NSE = ["NSE:RELIANCE-EQ", "NSE:TCS-EQ", "NSE:HDFCBANK-EQ", "NSE:INFY-EQ",
       "NSE:ICICIBANK-EQ", "NSE:SBIN-EQ", "NSE:AXISBANK-EQ", "NSE:HCLTECH-EQ"]
US  = ["AAPL", "MSFT", "NVDA", "AMD", "TSLA", "AMZN", "META", "GOOGL"]
RSI_LOW, RSI_HIGH, MIN_RVOL, WARMUP = 30.0, 70.0, 1.2, 30
# percent SL / trail grid (of entry price)
GRID = [(0.4, 0.8), (0.5, 1.0), (0.6, 1.4)]


def fetch_nse(cl, sym, days):
    end = datetime.now(tz=IST); start = end - timedelta(days=days)
    r = cl.history(data={"symbol": sym, "resolution": "5", "date_format": "1",
                         "range_from": start.strftime("%Y-%m-%d"), "range_to": end.strftime("%Y-%m-%d"),
                         "cont_flag": "1"})
    if not isinstance(r, dict) or r.get("s") != "ok" or not r.get("candles"):
        return None
    df = pd.DataFrame(r["candles"], columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.tz_convert(IST)
    return df


def fetch_us(cl, sym, days):
    end = (datetime.utcnow() - timedelta(days=1)); start = end - timedelta(days=days)
    try:
        bars = cl.get_bars(sym, "5Min", start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"),
                           feed="iex", limit=100000)
        rows = [(b.t, b.o, b.h, b.l, b.c, b.v) for b in bars]
    except Exception as e:
        print(f"  {sym}: US fetch error {e}"); return None
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(ET)
    # regular session only (09:30–16:00 ET)
    df = df[(df["timestamp"].dt.time >= pd.Timestamp("09:30").time()) &
            (df["timestamp"].dt.time < pd.Timestamp("16:00").time())].reset_index(drop=True)
    return df


def sim(df, sl_pct, trail_pct):
    """Reversal entries on 5m; directional stock trade with % SL + % trailing, EOD."""
    o = df["open"].values; c = df["close"].values; h = df["high"].values; l = df["low"].values
    rsi = calc_rsi(df["close"]).values
    rvol = relative_volume(df).values
    vol_present = df["volume"].sum() > 0
    dates = df["timestamp"].dt.date.values
    n = len(df); trades = []; i = WARMUP
    while i < n - 1:
        eod = dates[i + 1] != dates[i]
        bull = (i >= 2 and c[i-1] < o[i-1] and c[i] > o[i] and c[i] > o[i-1]
                and RSI_LOW < rsi[i] < RSI_HIGH and rsi[i] > rsi[i-1]
                and ((not vol_present) or rvol[i] >= MIN_RVOL))
        if bull and not eod:
            entry = c[i]; stop = entry * (1 - sl_pct/100); peak = entry
            exit_px = None; reason = "EOD"; j = i + 1
            while j < n and dates[j] == dates[i]:
                peak = max(peak, h[j]); stop = max(stop, peak * (1 - trail_pct/100))
                if l[j] <= stop:
                    exit_px, reason = stop, ("TRAIL" if stop > entry*(1-sl_pct/100) else "STOP"); break
                j += 1
            if exit_px is None:
                exit_px = c[min(j, n-1)] if j < n else c[-1]
            ret = (exit_px - entry) / entry * 100 - 0.10   # ~0.10% round-trip cost
            trades.append({"ret": ret, "reason": reason})
            i = j
        else:
            i += 1
    return trades


def rep(label, trades):
    if not trades:
        print(f"  {label}: no trades"); return None
    wins = [t for t in trades if t["ret"] > 0]
    gw = sum(t["ret"] for t in wins); gl = -sum(t["ret"] for t in trades if t["ret"] <= 0)
    pf = gw / gl if gl > 0 else float("inf")
    print(f"  {label:14} n={len(trades):4} win={len(wins)/len(trades):.0%} PF={pf:.2f} "
          f"avg={sum(t['ret'] for t in trades)/len(trades):+.2f}% tot={sum(t['ret'] for t in trades):+.0f}%")
    return pf


def main():
    nse_days = int(sys.argv[1]) if len(sys.argv) > 1 else 90
    us_days  = int(sys.argv[2]) if len(sys.argv) > 2 else 55
    fyers_broker.initialise(); alpaca_broker.initialise()
    fy = fyers_broker._client; al = alpaca_broker._client
    print(f"=== Reversal on STOCKS (directional) — NSE {nse_days}d / US {us_days}d, 5m ===")

    for market, syms, fetcher, client, dd in (("NSE", NSE, fetch_nse, fy, nse_days),
                                              ("US",  US,  fetch_us,  al, us_days)):
        print(f"\n--- {market} ---")
        dfs = {}
        for s in syms:
            d = fetcher(client, s, dd)
            if d is not None and len(d) > 60:
                dfs[s] = d
        print(f"  symbols with data: {len(dfs)}/{len(syms)}")
        for sl, tr in GRID:
            pooled = []
            for d in dfs.values():
                pooled += sim(d, sl, tr)
            rep(f"SL{sl}/trail{tr}", pooled)


if __name__ == "__main__":
    main()
