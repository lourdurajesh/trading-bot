"""
fix_entry_prices.py
───────────────────
One-shot script to correct entry prices on learning trades that were
opened using a Black-Scholes estimate instead of a real chain price.

For each affected trade:
  1. Fetches 1-min Fyers history for the NFO symbol on the entry date
  2. Finds the candle whose timestamp is at or just after the entry time
  3. Uses that candle's close as the real entry price
  4. Recalculates stop_loss (50% of real entry), pnl_pts, pnl_r
  5. Updates the DB row

Run on the server:
    cd /home/ubuntu/trading-bot
    python fix_entry_prices.py [--dry-run]

Pass --dry-run to preview changes without writing to DB.
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
FEES_OPTIONS_FLAT = 105.0
SLIPPAGE          = 0.005   # 0.5% — must match learning_engine.py

parser = argparse.ArgumentParser()
parser.add_argument("--dry-run", action="store_true")
args = parser.parse_args()

# ── Bootstrap Fyers client (reuse the bot's auth) ────────────────────────────
from config.settings import DB_PATH
from execution.fyers_broker import fyers_broker

if not fyers_broker._initialised:
    print("Initialising Fyers broker …")
    if not fyers_broker.initialise():
        print("ERROR: Fyers broker init failed. Check FYERS_ACCESS_TOKEN in .env.")
        sys.exit(1)
    print("Fyers broker ready.\n")

# ── Load affected trades ──────────────────────────────────────────────────────
# Trades are "affected" when they have nse_options instrument type and
# the entry_price doesn't match a real market price.
# We identify them by comparing entry_price to what the 1-min candle shows.
# To keep it safe we let the user confirm before writing.

with sqlite3.connect(DB_PATH) as conn:
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT id, symbol, entry_price, exit_price, stop_loss, target,
               pnl_pts, pnl_r, entry_time, exit_reason, metadata
        FROM learning_trades
        WHERE metadata LIKE '%nse_options%'
        ORDER BY entry_time DESC
        LIMIT 50
    """).fetchall()

trades = [dict(r) for r in rows]
if not trades:
    print("No NSE options learning trades found.")
    sys.exit(0)

print(f"Found {len(trades)} NSE options trade(s) to inspect.\n")

corrections = []

for t in trades:
    meta = {}
    try:
        meta = json.loads(t["metadata"] or "{}")
    except Exception:
        pass

    nfo_symbol = meta.get("nfo_symbol")
    lot_size   = int(meta.get("lot_size", 1)) or 1

    if not nfo_symbol:
        print(f"  SKIP {t['id']} — no nfo_symbol in metadata")
        continue

    # Parse entry time
    try:
        entry_dt = datetime.fromisoformat(t["entry_time"]).astimezone(IST)
    except Exception:
        print(f"  SKIP {t['id']} — bad entry_time: {t['entry_time']}")
        continue

    # Fetch 1-min history for that symbol (2 days back to ensure coverage)
    print(f"  Fetching 1-min history for {nfo_symbol} …", end=" ", flush=True)
    try:
        from datetime import timedelta as td
        end_date   = entry_dt + td(days=1)
        start_date = entry_dt - td(days=1)
        resp = fyers_broker._client.history(data={
            "symbol":      nfo_symbol,
            "resolution":  "1",
            "date_format": "1",
            "range_from":  start_date.strftime("%Y-%m-%d"),
            "range_to":    end_date.strftime("%Y-%m-%d"),
            "cont_flag":   "0",
        })
    except Exception as e:
        print(f"FAILED ({e})")
        continue

    if resp.get("s") != "ok" or not resp.get("candles"):
        print(f"no data returned ({resp.get('message', '')})")
        continue

    import pandas as pd
    candles = resp["candles"]
    df = pd.DataFrame(candles, columns=["ts", "open", "high", "low", "close", "volume"])
    df["dt"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")

    # Find candle at or just after entry time (within 5 minutes)
    window = df[(df["dt"] >= entry_dt) & (df["dt"] <= entry_dt + td(minutes=5))]
    if window.empty:
        # Fall back: nearest candle within 15 min
        window = df[(df["dt"] >= entry_dt - td(minutes=2)) & (df["dt"] <= entry_dt + td(minutes=15))]

    if window.empty:
        print(f"no candle near entry time {entry_dt.strftime('%H:%M:%S')}")
        continue

    real_ltp = float(window.iloc[0]["close"])
    real_entry = round(real_ltp * (1 + SLIPPAGE), 2)   # re-apply buy slippage

    old_entry = t["entry_price"]
    diff_pct  = abs(real_entry - old_entry) / old_entry * 100

    # Recalculate stop and targets based on real entry
    # stop_loss = 50% of debit cost (debit_cost ≈ real_entry / 1.005 * 1.0 = approx real_entry)
    new_stop   = round(real_entry * 0.5, 2)
    new_target = round(real_entry + (t["target"] - old_entry), 2)  # preserve the spread width

    # Recalculate pnl if trade is closed
    new_pnl_pts = t["pnl_pts"]
    new_pnl_r   = t["pnl_r"]
    if t["exit_price"] and t["exit_price"] > 0:
        eff_exit    = t["exit_price"]
        new_pnl_pts = round((eff_exit - real_entry) * lot_size - FEES_OPTIONS_FLAT, 2)
        risk        = abs(real_entry - new_stop)
        new_pnl_r   = round((eff_exit - real_entry) / risk, 2) if risk > 0 else 0

    print(f"entry {old_entry:.2f} → {real_entry:.2f} ({diff_pct:.1f}% diff)  "
          f"stop {t['stop_loss']:.2f} → {new_stop:.2f}  "
          f"pnl_pts {t['pnl_pts']:+.2f} → {new_pnl_pts:+.2f}")

    corrections.append({
        "id":         t["id"],
        "entry":      real_entry,
        "stop_loss":  new_stop,
        "target":     new_target,
        "pnl_pts":    new_pnl_pts,
        "pnl_r":      new_pnl_r,
        "real_ltp":   real_ltp,
        "old_entry":  old_entry,
    })

print(f"\n{len(corrections)} trade(s) ready to correct.")

if not corrections:
    sys.exit(0)

if args.dry_run:
    print("DRY RUN — no changes written.")
    sys.exit(0)

confirm = input("\nWrite corrections to DB? [y/N] ")
if confirm.strip().lower() != "y":
    print("Aborted.")
    sys.exit(0)

with sqlite3.connect(DB_PATH) as conn:
    for c in corrections:
        conn.execute("""
            UPDATE learning_trades
            SET entry_price = ?,
                stop_loss   = ?,
                target      = ?,
                pnl_pts     = ?,
                pnl_r       = ?
            WHERE id = ?
        """, (c["entry"], c["stop_loss"], c["target"],
              c["pnl_pts"], c["pnl_r"], c["id"]))
    conn.commit()

print(f"Done — {len(corrections)} trade(s) corrected.")
