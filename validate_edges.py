"""
validate_edges.py
Validates three trading edges before building them into the AI agent.

  Edge 1: Expiry-day theta selling on NIFTY/BANKNIFTY
  Edge 2: FII flow -> next-day Nifty direction
  Edge 3: Thursday vs other days volatility comparison

Run from project root:
    python validate_edges.py
"""

import sys
import math
import json
import time
from pathlib import Path
from datetime import datetime, timedelta, date

import pandas as pd
import numpy as np
import requests


# ---------------------------------------------------------------------------
# DATA LOADING
# ---------------------------------------------------------------------------

HIST_DIR = Path("db/historical")


def load_index(name: str) -> pd.DataFrame:
    path = HIST_DIR / f"NSE_{name}_INDEX_1D.csv"
    df = pd.read_csv(path)
    df["date"] = pd.to_datetime(df["timestamp"]).dt.normalize()
    df = df.set_index("date").sort_index()
    df["return"]    = df["close"].pct_change()
    df["hv20"]      = df["return"].rolling(20).std() * math.sqrt(252)
    df["range_pct"] = (df["high"] - df["low"]) / df["open"] * 100
    df["dow"]       = df.index.dayofweek   # 0=Mon, 3=Thu, 4=Fri
    return df


nifty     = load_index("NIFTY50")
banknifty = load_index("NIFTYBANK")

SEP = "=" * 65
SEP2 = "-" * 65

print(f"\n{SEP}")
print("  EDGE VALIDATION -- AI Trading Agent Pre-Build Evidence")
print(f"  Data: {nifty.index[0].date()} to {nifty.index[-1].date()}  ({len(nifty)} trading days)")
print(SEP)


# ---------------------------------------------------------------------------
# EDGE 1: THURSDAY EXPIRY THETA SELLING
# ---------------------------------------------------------------------------
#
# Methodology:
#   - Identify all Thursdays (weekly NIFTY/BANKNIFTY expiry day)
#   - Estimate ATM straddle premium using realized HV as IV proxy:
#       straddle_pts = spot x (HV_annual / sqrt(252)) x 0.798
#       where 0.798 = sqrt(2/pi)  (standard ATM straddle approximation)
#   - WIN if actual range (H-L) < straddle premium (seller keeps premium)
#   - LOSS if range > premium (seller pays out the excess)
#   - NOTE: real IV includes a ~15-25% vol risk premium over realized HV
#     so actual premium is HIGHER than our estimate -> our win rate is CONSERVATIVE

print(f"\n{SEP2}")
print("  EDGE 1: Expiry Thursday ATM Straddle Selling")
print(SEP2)

results = {}
for index_name, df in [("NIFTY", nifty), ("BANKNIFTY", banknifty)]:
    thursdays = df[df["dow"] == 3].dropna(subset=["hv20"]).copy()

    thursdays["sigma_daily"] = thursdays["hv20"] / math.sqrt(252)
    thursdays["premium_est"] = thursdays["open"] * thursdays["sigma_daily"] * 0.798
    thursdays["actual_range"] = thursdays["high"] - thursdays["low"]
    thursdays["win"] = thursdays["actual_range"] < thursdays["premium_est"]

    thursdays["pnl_pts"] = np.where(
        thursdays["win"],
        thursdays["premium_est"],
        thursdays["premium_est"] - thursdays["actual_range"],
    )
    thursdays["pnl_pct"] = thursdays["pnl_pts"] / thursdays["open"] * 100

    win_rate       = thursdays["win"].mean() * 100
    avg_pnl        = thursdays["pnl_pct"].mean()
    avg_range      = thursdays["range_pct"].mean()
    avg_prem       = (thursdays["premium_est"] / thursdays["open"] * 100).mean()
    median_pnl_pts = thursdays["pnl_pts"].median()
    total_pnl_pts  = thursdays["pnl_pts"].sum()

    results[index_name] = {
        "thursdays_n":    len(thursdays),
        "win_rate":       win_rate,
        "avg_pnl_pct":    avg_pnl,
        "avg_range_pct":  avg_range,
        "avg_premium_pct": avg_prem,
        "total_pnl_pts":  total_pnl_pts,
    }

    thursdays["year_month"] = thursdays.index.to_period("M")
    monthly = thursdays.groupby("year_month").agg(
        wins=("win", "sum"),
        total=("win", "count"),
        pnl_pts=("pnl_pts", "sum"),
    )
    monthly["win_rate"] = monthly["wins"] / monthly["total"] * 100
    profitable_months   = (monthly["pnl_pts"] > 0).sum()

    print(f"\n  {index_name}  ({len(thursdays)} Thursdays across {len(thursdays)//52} years):")
    print(f"    Win rate (range < premium): {win_rate:.1f}%")
    print(f"    Avg premium estimate:       {avg_prem:.2f}% of spot")
    print(f"    Avg actual range:           {avg_range:.2f}% of spot")
    print(f"    Avg P&L per expiry:         {avg_pnl:+.3f}% of spot")
    print(f"    Profitable months:          {profitable_months}/{len(monthly)}")
    print(f"    Cumulative P&L (pts):       {total_pnl_pts:+,.0f}")
    print(f"    Median P&L per expiry (pts):{median_pnl_pts:+.0f}")

    worst = monthly.nsmallest(3, "pnl_pts")
    best  = monthly.nlargest(3,  "pnl_pts")
    print(f"    3 worst months: {list(zip(worst.index.astype(str), worst['pnl_pts'].round(0).astype(int)))}")
    print(f"    3 best months:  {list(zip(best.index.astype(str),  best['pnl_pts'].round(0).astype(int)))}")


# ---------------------------------------------------------------------------
# EDGE 3: THURSDAY vs OTHER DAYS RANGE
# ---------------------------------------------------------------------------

print(f"\n{SEP2}")
print("  EDGE 3: Thursday vs Other Days -- Range Comparison")
print(SEP2)

for index_name, df in [("NIFTY", nifty), ("BANKNIFTY", banknifty)]:
    thu  = df[df["dow"] == 3]["range_pct"].dropna()
    rest = df[df["dow"] != 3]["range_pct"].dropna()
    var_thu  = thu.var()  / len(thu)
    var_rest = rest.var() / len(rest)
    t_val = (thu.mean() - rest.mean()) / math.sqrt(var_thu + var_rest)
    pct_under_1_thu  = (thu  < 1.0).mean() * 100
    pct_under_1_rest = (rest < 1.0).mean() * 100

    print(f"\n  {index_name}:")
    print(f"    Thursday avg range:      {thu.mean():.2f}%")
    print(f"    Other days avg range:    {rest.mean():.2f}%")
    print(f"    Difference:              {thu.mean()-rest.mean():+.3f}%  (t={t_val:.2f})")
    print(f"    Thursdays range < 1%:    {pct_under_1_thu:.1f}%")
    print(f"    Other days range < 1%:   {pct_under_1_rest:.1f}%")


# ---------------------------------------------------------------------------
# EDGE 2: FII FLOW -> NEXT-DAY NIFTY
# ---------------------------------------------------------------------------
#
# Fetch NSE FII data in 90-day chunks using session cookie.
# Cache result to db/fii_history.json so subsequent runs are instant.

print(f"\n{SEP2}")
print("  EDGE 2: FII Flow -> Next-Day Nifty Direction")
print(SEP2)

FII_CACHE = Path("db/fii_history.json")


def fetch_fii_nse() -> list[dict]:
    session = requests.Session()
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    headers = {
        "User-Agent": ua,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/market-data/fii-dii-trading-activity",
    }

    # Seed session cookie
    try:
        session.get("https://www.nseindia.com", headers={"User-Agent": ua}, timeout=12)
        time.sleep(1.5)
    except Exception as e:
        print(f"  [FII] Session init failed: {e}")
        return []

    records = []
    start = date(2023, 5, 1)
    end   = date(2026, 4, 30)
    cur   = start

    while cur < end:
        chunk_end = min(cur + timedelta(days=89), end)
        url = (
            "https://www.nseindia.com/api/fiidiiTradeReact"
            f"?startDate={cur.strftime('%d-%m-%Y')}"
            f"&endDate={chunk_end.strftime('%d-%m-%Y')}"
        )
        try:
            r = session.get(url, headers=headers, timeout=12)
            r.raise_for_status()
            data = r.json()

            for row in data:
                cat = row.get("category", "")
                if "FII" not in cat.upper():
                    continue
                try:
                    net = float(str(row.get("netPurchasesSales", "0")).replace(",", ""))
                    dt  = row.get("date", row.get("tradeDate", ""))
                    if dt:
                        records.append({"date": dt, "fii_net": net})
                except (ValueError, KeyError):
                    continue

            time.sleep(0.6)
        except Exception as e:
            print(f"  [FII] Chunk {cur} -> {chunk_end} failed: {e}")

        cur = chunk_end + timedelta(days=1)

    return records


# Load or fetch
fii_df = None
if FII_CACHE.exists():
    try:
        raw    = json.loads(FII_CACHE.read_text())
        fii_df = pd.DataFrame(raw)
        fii_df["date"] = pd.to_datetime(fii_df["date"], dayfirst=True, errors="coerce")
        fii_df = fii_df.dropna(subset=["date"]).set_index("date").sort_index()
        print(f"  Loaded FII cache: {len(fii_df)} rows ({fii_df.index[0].date()} to {fii_df.index[-1].date()})")
    except Exception:
        fii_df = None

if fii_df is None or len(fii_df) < 100:
    print("  Fetching FII data from NSE (30-60 seconds)...")
    records = fetch_fii_nse()
    if records:
        fii_df = pd.DataFrame(records)
        fii_df["date"] = pd.to_datetime(fii_df["date"], dayfirst=True, errors="coerce")
        fii_df = fii_df.dropna(subset=["date"]).set_index("date").sort_index()
        FII_CACHE.write_text(json.dumps(records, default=str))
        print(f"  Fetched {len(fii_df)} FII records -> cached to {FII_CACHE}")
    else:
        print("  NSE fetch blocked -- using large Nifty move autocorrelation as proxy:")
        fii_df = None


if fii_df is not None and len(fii_df) >= 100:
    merged = nifty[["return"]].copy()
    merged["next_return"] = merged["return"].shift(-1)
    merged = merged.join(fii_df[["fii_net"]], how="inner").dropna()

    THRESHOLD = 500  # crore

    bullish = merged[merged["fii_net"] >  THRESHOLD]
    bearish = merged[merged["fii_net"] < -THRESHOLD]
    neutral = merged[merged["fii_net"].between(-THRESHOLD, THRESHOLD)]

    print(f"\n  Threshold: FII net +/-Rs.{THRESHOLD} Cr  |  Days analyzed: {len(merged)}")

    for label, subset, direction in [
        (f"FII Buying  (>{THRESHOLD} Cr)", bullish, "LONG"),
        (f"FII Selling (<{-THRESHOLD} Cr)", bearish, "SHORT"),
        ("FII Neutral",                    neutral,  None),
    ]:
        n = len(subset)
        if n == 0:
            continue
        avg = subset["next_return"].mean() * 100
        std = subset["next_return"].std()   * 100
        if direction == "LONG":
            hr = (subset["next_return"] > 0).mean() * 100
        elif direction == "SHORT":
            hr = (subset["next_return"] < 0).mean() * 100
        else:
            hr = 50.0

        sharpe = (avg / std * math.sqrt(252)) if std > 0 else 0.0
        print(f"\n  {label} ({n} days):")
        print(f"    Next-day avg return:  {avg:+.3f}%")
        print(f"    Directional hit rate: {hr:.1f}%")
        print(f"    Annualized Sharpe:    {sharpe:.2f}")

    corr = merged["fii_net"].corr(merged["next_return"])
    print(f"\n  Pearson correlation (FII net -> next-day Nifty return): {corr:+.3f}")
    print("  Tradeable edge threshold: corr > 0.10 AND hit rate > 55%")

else:
    # Fallback without FII data
    nifty_c = nifty[["return"]].dropna().copy()
    nifty_c["next_return"] = nifty_c["return"].shift(-1)
    nifty_c = nifty_c.dropna()

    print("\n  Large-move autocorrelation proxy (replaces FII analysis):")
    for threshold in [0.5, 1.0, 1.5]:
        big_up   = nifty_c[nifty_c["return"] >  threshold / 100]
        big_down = nifty_c[nifty_c["return"] < -threshold / 100]

        if len(big_up) >= 10:
            hr = (big_up["next_return"] > 0).mean() * 100
            avg = big_up["next_return"].mean() * 100
            print(f"    After Nifty up   >{threshold}% ({len(big_up):3d} days): next-day positive {hr:.1f}%  avg {avg:+.3f}%")

        if len(big_down) >= 10:
            hr = (big_down["next_return"] < 0).mean() * 100
            avg = big_down["next_return"].mean() * 100
            print(f"    After Nifty down >{threshold}% ({len(big_down):3d} days): next-day negative {hr:.1f}%  avg {avg:+.3f}%")


# ---------------------------------------------------------------------------
# FINAL VERDICT
# ---------------------------------------------------------------------------

print(f"\n{SEP}")
print("  VERDICT SUMMARY")
print(SEP)

for index_name, r in results.items():
    wr = r["win_rate"]
    if wr >= 60:
        verdict = "STRONG EDGE -- BUILD IT"
    elif wr >= 52:
        verdict = "EDGE EXISTS -- VIABLE"
    else:
        verdict = "WEAK / NO EDGE"
    print(f"\n  {index_name} Expiry Theta Selling:")
    print(f"    Win rate:  {wr:.1f}%  ->  {verdict}")
    print(f"    Premium:   {r['avg_premium_pct']:.2f}% of spot  vs  Actual range: {r['avg_range_pct']:.2f}% of spot")

print("""
  NOTE: These win rates are CONSERVATIVE because:
    - Real IV = Realized HV + volatility risk premium (15-25% higher premium)
    - Actual straddle price on expiry day is higher than our HV estimate
    - Real premium = bigger cushion = higher win rate in practice

  Next build priority:
    Win rate >= 60% -> module 1: expiry-day delta-hedged straddle
    FII corr > 0.10 -> module 2: FII flow signal filter
    Both -> multi-layer AI agent with measurable edge
""")
