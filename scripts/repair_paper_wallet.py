"""
repair_paper_wallet.py  (one-off)
─────────────────────────────────
Repairs paper_trades corrupted by the index-as-premium close bug (fixed in
paper_trading._record_exit / close_order), then rebuilds the paper wallet
balance from the corrected ledger.

Corruption signature: an nse_options trade closed at exit_price > 20× its entry
premium — the index spot was booked as the option exit price, producing a
catastrophic phantom P&L (e.g. (24136 − 9.13) × 260 = +₹62.7L).

Correction rule per corrupt row:
  • if a learning sibling (LRN-<suffix>) exists → use its true premium move
    (corrected exit = entry + pnl_pts), the real outcome.
  • else (orphan leg) → exit = entry, i.e. ≈₹0 P&L (minus brokerage). Honest:
    we don't know the true exit and the recorded one is garbage.

Then: wallet = STARTING + Σ realised_pnl(CLOSED) − Σ capital_deployed(OPEN).

Run:   python scripts/repair_paper_wallet.py            # apply
       python scripts/repair_paper_wallet.py --check     # report only
Back up db/trades.db first.
"""
import sqlite3
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from config.settings import DB_PATH

IST = ZoneInfo("Asia/Kolkata")
STARTING = 500_000.0
BROKERAGE_PCT = 0.03   # must match paper_trading.BROKERAGE_PCT


def _brokerage(entry, exit_, size):
    return (entry + exit_) * size * BROKERAGE_PCT / 100


def find_corrupt(conn):
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT * FROM paper_trades WHERE status='CLOSED' AND instrument_type='nse_options' "
        "AND exit_price > entry_price*20"
    ).fetchall()


def learning_pnl_pts(conn, paper_id):
    """True premium move from the learning sibling, or None if no sibling."""
    lrn_id = "LRN-" + paper_id[-8:]
    row = conn.execute(
        "SELECT pnl_pts FROM learning_trades WHERE id=?", (lrn_id,)
    ).fetchone()
    return float(row[0]) if row and row[0] is not None else None


def corrected_pnl(conn, row):
    entry = float(row["entry_price"]); size = int(row["position_size"])
    direction = row["direction"]
    pts = learning_pnl_pts(conn, row["id"])
    if pts is not None:
        # learning pnl_pts is the favourable-direction premium move already
        new_exit = round(entry + pts, 2) if direction == "LONG" else round(entry - pts, 2)
    else:
        new_exit = entry  # orphan — unknown true exit, book ≈0
    if direction == "LONG":
        gross = (new_exit - entry) * size
    else:
        gross = (entry - new_exit) * size
    pnl = round(gross - _brokerage(entry, new_exit, size), 2)
    return new_exit, pnl


def rebuild_wallet(conn):
    realised = conn.execute(
        "SELECT COALESCE(SUM(realised_pnl),0) FROM paper_trades WHERE status='CLOSED'"
    ).fetchone()[0]
    deployed = conn.execute(
        "SELECT COALESCE(SUM(capital_deployed),0) FROM paper_trades WHERE status='OPEN'"
    ).fetchone()[0]
    return round(STARTING + float(realised) - float(deployed), 2), float(realised), float(deployed)


def main():
    check = "--check" in sys.argv
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        before_bal = conn.execute("SELECT balance FROM paper_wallet WHERE id=1").fetchone()
        before_bal = float(before_bal[0]) if before_bal else None
        corrupt = find_corrupt(conn)
        print(f"current wallet balance : ₹{before_bal:,.2f}")
        print(f"corrupt option rows    : {len(corrupt)}")
        for r in corrupt:
            new_exit, pnl = corrected_pnl(conn, r)
            print(f"  {r['id']}  {r['symbol']}  qty={r['position_size']}  "
                  f"entry={r['entry_price']:.2f}  exit {r['exit_price']:.2f}→{new_exit:.2f}  "
                  f"pnl {float(r['realised_pnl']):,.0f}→{pnl:,.0f}")

        rb_check, realised_now, _ = rebuild_wallet(conn)
        if check:
            print(f"\n[check] realised(CLOSED, uncorrected)=₹{realised_now:,.0f}")
            print("[check] no changes written.")
            return

        for r in corrupt:
            new_exit, pnl = corrected_pnl(conn, r)
            conn.execute(
                "UPDATE paper_trades SET exit_price=?, realised_pnl=?, "
                "exit_reason=exit_reason||'_REPAIRED' WHERE id=?",
                (new_exit, pnl, r["id"]),
            )
        new_bal, realised, deployed = rebuild_wallet(conn)
        conn.execute(
            "UPDATE paper_wallet SET balance=?, updated_at=? WHERE id=1",
            (new_bal, datetime.now(tz=IST).isoformat()),
        )
        conn.commit()
        print(f"\nrealised(CLOSED, corrected) : ₹{realised:,.2f}")
        print(f"capital deployed (OPEN)     : ₹{deployed:,.2f}")
        print(f"wallet balance  ₹{before_bal:,.2f} → ₹{new_bal:,.2f}")
        print("✓ repair complete.")


if __name__ == "__main__":
    main()
