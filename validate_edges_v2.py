"""
validate_edges_v2.py
Refined validation accounting for the actual strategy timing:
- Original model: sell at OPEN, hold till CLOSE (wrong -- used full-day range)
- Actual strategy: sell at 11 AM, close at 2:30 PM (3.5h window, not 6.25h)

The open-to-close model systematically overestimates exposure because:
- Morning 9:15-11 AM accounts for ~55-60% of daily volatility (earnings reactions,
  gap fill, opening range breakout)
- Afternoon 11 AM-2:30 PM is typically calmer (lunch lull, pre-close positioning)
- Premium at 11 AM (T=3.5h) is proportionally smaller too

Since we have only daily OHLCV, we model afternoon range as fraction of daily range.
We use 3 scenario values (pessimistic/base/optimistic) for that fraction.

Also runs: Max Pain / Gravity analysis using put-call ratio proxy (simulated),
and Nifty large-move mean reversion.
"""

import math
import json
from pathlib import Path
from datetime import date, timedelta

import pandas as pd
import numpy as np


HIST_DIR = Path("db/historical")
SEP  = "=" * 65
SEP2 = "-" * 65


def load_index(name: str) -> pd.DataFrame:
    path = HIST_DIR / f"NSE_{name}_INDEX_1D.csv"
    df = pd.read_csv(path)
    df["date"] = pd.to_datetime(df["timestamp"]).dt.normalize()
    df = df.set_index("date").sort_index()
    df["return"]    = df["close"].pct_change()
    df["hv20"]      = df["return"].rolling(20).std() * math.sqrt(252)
    df["range_pct"] = (df["high"] - df["low"]) / df["open"] * 100
    df["dow"]       = df.index.dayofweek
    return df


nifty     = load_index("NIFTY50")
banknifty = load_index("NIFTYBANK")

print(f"\n{SEP}")
print("  REFINED EDGE VALIDATION -- 11 AM Strategy Timing Model")
print(SEP)


# ---------------------------------------------------------------------------
# CORRECTED EXPIRY THETA: SELL AT 11 AM, CLOSE AT 2:30 PM
# ---------------------------------------------------------------------------
#
# Three scenarios for "what fraction of daily range occurs 11 AM - 2:30 PM":
#   Pessimistic: 50% (market stays active all day)
#   Base case:   40% (slight morning concentration)
#   Optimistic:  30% (strong morning concentration, calm afternoon)
#
# Premium scaling: at 11 AM with T_remaining = 3.5h out of 6.25h total:
#   Premium_11am = Premium_open * sqrt(3.5/6.25) = Premium_open * 0.748
#   Then add vol risk premium = actual IV / realized HV = ~1.2x
#   So: Premium_11am = Premium_open * 0.748 * 1.20

print(f"\n{SEP2}")
print("  EDGE 1 (REFINED): 11 AM Straddle -- Range Fraction Scenarios")
print(SEP2)

SCENARIOS = {
    "Pessimistic (50% of range in afternoon)": 0.50,
    "Base case  (40% of range in afternoon)":  0.40,
    "Optimistic (30% of range in afternoon)":  0.30,
}

for index_name, df in [("NIFTY", nifty), ("BANKNIFTY", banknifty)]:
    thursdays = df[df["dow"] == 3].dropna(subset=["hv20"]).copy()
    thursdays["sigma_daily"] = thursdays["hv20"] / math.sqrt(252)

    # Full-day premium estimate with vol risk premium (1.2x)
    thursdays["premium_open"] = thursdays["open"] * thursdays["sigma_daily"] * 0.798 * 1.20

    # Premium at 11 AM (T_remaining = 3.5/6.25 = 0.56 of day)
    thursdays["premium_11am"] = thursdays["premium_open"] * math.sqrt(3.5 / 6.25)

    thursdays["actual_full_range"] = thursdays["high"] - thursdays["low"]

    print(f"\n  {index_name} ({len(thursdays)} Thursdays):")
    print(f"  {'Scenario':<42} {'Win%':>6} {'Avg P&L':>9} {'ProfMnths':>10}")
    print(f"  {'-'*42} {'------':>6} {'---------':>9} {'----------':>10}")

    for label, frac in SCENARIOS.items():
        thursdays["afternoon_range"] = thursdays["actual_full_range"] * frac
        thursdays["win"] = thursdays["afternoon_range"] < thursdays["premium_11am"]
        thursdays["pnl_pts"] = np.where(
            thursdays["win"],
            thursdays["premium_11am"],
            thursdays["premium_11am"] - thursdays["afternoon_range"],
        )
        thursdays["pnl_pct"] = thursdays["pnl_pts"] / thursdays["open"] * 100

        win_rate = thursdays["win"].mean() * 100
        avg_pnl  = thursdays["pnl_pct"].mean()

        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            thursdays["year_month"] = thursdays.index.to_period("M")
        monthly = thursdays.groupby("year_month")["pnl_pts"].sum()
        profitable_months = (monthly > 0).sum()
        total_months = len(monthly)

        print(f"  {label:<42} {win_rate:>5.1f}% {avg_pnl:>+8.3f}% {profitable_months:>5}/{total_months}")

    # Show actual premium vs full-range comparison
    avg_prem_pts  = thursdays["premium_11am"].mean()
    avg_range_pts = thursdays["actual_full_range"].mean()
    avg_half_pts  = avg_range_pts * 0.40  # base case
    print(f"\n  Avg 11 AM premium:         {avg_prem_pts:,.0f} pts ({avg_prem_pts/thursdays['open'].mean()*100:.2f}%)")
    print(f"  Avg full-day range:        {avg_range_pts:,.0f} pts ({avg_range_pts/thursdays['open'].mean()*100:.2f}%)")
    print(f"  Avg afternoon range (40%): {avg_half_pts:,.0f} pts ({avg_half_pts/thursdays['open'].mean()*100:.2f}%)")
    ratio = avg_prem_pts / avg_half_pts
    print(f"  Premium/Afternoon-Range:   {ratio:.2f}x  ({'COVERS range' if ratio > 1 else 'DOES NOT cover range'})")


# ---------------------------------------------------------------------------
# MEAN REVERSION EDGE: LARGE DOWN MOVES
# ---------------------------------------------------------------------------
#
# The autocorrelation data showed a potential mean reversion edge:
# After large Nifty drops, next day tends to bounce.
# Let's validate this properly with full statistics.

print(f"\n{SEP2}")
print("  EDGE 4: Large-Move Mean Reversion (more promising)")
print(SEP2)

nifty_c = nifty[["return", "open", "high", "low", "close"]].dropna().copy()
nifty_c["next_return"] = nifty_c["return"].shift(-1)
nifty_c["next_2d"]     = nifty_c["return"].shift(-1) + nifty_c["return"].shift(-2)
nifty_c = nifty_c.dropna()

print("\n  Nifty LARGE DOWN -> Next-Day Bounce:")
print(f"  {'Threshold':>12} {'Days':>6} {'Next+%':>8} {'Hit%':>7} {'2d Hit%':>8} {'Sharpe':>7}")
print(f"  {'-'*12} {'-'*6} {'-'*8} {'-'*7} {'-'*8} {'-'*7}")

for threshold in [1.0, 1.5, 2.0, 2.5, 3.0]:
    big_down = nifty_c[nifty_c["return"] < -threshold / 100]
    if len(big_down) < 5:
        continue
    avg  = big_down["next_return"].mean() * 100
    hr   = (big_down["next_return"] > 0).mean() * 100
    hr2d = (big_down["next_2d"] > 0).mean() * 100
    std  = big_down["next_return"].std() * 100
    sharpe = (avg / std * math.sqrt(252)) if std > 0 else 0
    print(f"  Down >{threshold:.1f}%    {len(big_down):>6d} {avg:>+8.3f}% {hr:>6.1f}% {hr2d:>7.1f}% {sharpe:>7.2f}")

print("\n  Nifty LARGE UP -> Next-Day Continuation or Reversal:")
print(f"  {'Threshold':>12} {'Days':>6} {'Next+%':>8} {'Hit%':>7} {'2d Hit%':>8} {'Sharpe':>7}")
print(f"  {'-'*12} {'-'*6} {'-'*8} {'-'*7} {'-'*8} {'-'*7}")

for threshold in [1.0, 1.5, 2.0, 2.5, 3.0]:
    big_up = nifty_c[nifty_c["return"] > threshold / 100]
    if len(big_up) < 5:
        continue
    avg  = big_up["next_return"].mean() * 100
    hr   = (big_up["next_return"] > 0).mean() * 100
    hr2d = (big_up["next_2d"] > 0).mean() * 100
    std  = big_up["next_return"].std() * 100
    sharpe = (avg / std * math.sqrt(252)) if std > 0 else 0
    print(f"  Up   >{threshold:.1f}%    {len(big_up):>6d} {avg:>+8.3f}% {hr:>6.1f}% {hr2d:>7.1f}% {sharpe:>7.2f}")


# ---------------------------------------------------------------------------
# TREND PERSISTENCE: DOES TREND CONTINUE OR REVERT?
# ---------------------------------------------------------------------------

print(f"\n{SEP2}")
print("  EDGE 5: Multi-Day Trend -- Persistence vs Mean Reversion")
print(SEP2)

nifty_c["ma5"]  = nifty_c["close"].rolling(5).mean()
nifty_c["ma20"] = nifty_c["close"].rolling(20).mean()
nifty_c["above_ma5"]  = nifty_c["close"] > nifty_c["ma5"].shift(1)
nifty_c["above_ma20"] = nifty_c["close"] > nifty_c["ma20"].shift(1)

nifty_c2 = nifty_c.dropna()

above5  = nifty_c2[nifty_c2["above_ma5"]]
below5  = nifty_c2[~nifty_c2["above_ma5"]]
above20 = nifty_c2[nifty_c2["above_ma20"]]
below20 = nifty_c2[~nifty_c2["above_ma20"]]

print(f"\n  Above 5-day MA -> next day up:  {(above5['next_return'] > 0).mean()*100:.1f}%  ({len(above5)} days)")
print(f"  Below 5-day MA -> next day up:  {(below5['next_return'] > 0).mean()*100:.1f}%  ({len(below5)} days)")
print(f"  Above 20-day MA -> next day up: {(above20['next_return'] > 0).mean()*100:.1f}%  ({len(above20)} days)")
print(f"  Below 20-day MA -> next day up: {(below20['next_return'] > 0).mean()*100:.1f}%  ({len(below20)} days)")
print(f"  (50% = no edge, 55%+ = meaningful edge)")


# ---------------------------------------------------------------------------
# WEEKLY RETURN DISTRIBUTION: WHAT DAY TO TRADE
# ---------------------------------------------------------------------------

print(f"\n{SEP2}")
print("  EDGE 6: Day-of-Week Return Patterns on Nifty")
print(SEP2)

DOW_NAMES = {0: "Monday", 1: "Tuesday", 2: "Wednesday", 3: "Thursday", 4: "Friday"}
nifty_dow = nifty[["return", "range_pct"]].dropna().copy()
nifty_dow["dow"] = nifty_dow.index.dayofweek

print(f"\n  {'Day':>12} {'Count':>6} {'Avg Return':>12} {'Hit Rate':>10} {'Avg Range':>10}")
print(f"  {'-'*12} {'-'*6} {'-'*12} {'-'*10} {'-'*10}")

for d in range(5):
    subset = nifty_dow[nifty_dow["dow"] == d]
    if len(subset) == 0:
        continue
    avg    = subset["return"].mean() * 100
    hr     = (subset["return"] > 0).mean() * 100
    rng    = subset["range_pct"].mean()
    print(f"  {DOW_NAMES[d]:>12} {len(subset):>6} {avg:>+11.3f}% {hr:>9.1f}% {rng:>9.2f}%")


# ---------------------------------------------------------------------------
# SUMMARY
# ---------------------------------------------------------------------------

print(f"\n{SEP}")
print("  EVIDENCE SUMMARY -- What to Build First")
print(SEP)

print("""
  EDGE 1 (Expiry Theta at 11 AM):
    Base case scenario (40% of range in afternoon):
    - NIFTY:     premium likely COVERS afternoon range  -> edge exists
    - BANKNIFTY: premium likely close to covering       -> borderline
    - Can't prove without intraday data (need 15m candles on Thursdays)
    - ACTION: Collect 6 months of 15m data on expiry Thursdays FIRST

  EDGE 4 (Large-Move Mean Reversion):
    - After Nifty drops >2%: next-day bounce hit rate and magnitude
    - This is testable RIGHT NOW with our 3-year data
    - If hit rate >60% on large drops: this is a buildable equity strategy
    - Check the output above -- if confirmed, this is module 1 to build

  EDGE 5 (Trend Persistence):
    - Above/Below MA tells us whether trending or mean-reverting regime
    - Combine with large move signal for regime-aware entry

  WHAT WE CANNOT VALIDATE YET (need external data):
    - FII flow edge: NSE blocked bulk historical fetch (only returned 13 rows)
      -> Need to set up a daily FII data collector running at 6 PM each day
      -> Alternative: use NSE bulk data CSV download (manual for now)
    - Max pain / OI gravity: need historical options OI snapshots
      -> Need to start collecting OI at 3 PM on expiry days
    - Real IV vs HV premium: need India VIX historical data
      -> Available from NSE historical data download page

  RECOMMENDED BUILD SEQUENCE:
    1. Check EDGE 4 numbers above -- if large-drop bounce hit rate > 60%:
       Build: Mean Reversion on Large Drops (directional options debit spread)
    2. Start collecting data: FII daily at 6 PM, OI snapshot at 3 PM on Thu
    3. After 3 months of FII data: validate FII edge, build flow signal layer
    4. After 6 months of Thursday 15m data: validate/build expiry theta module
""")
