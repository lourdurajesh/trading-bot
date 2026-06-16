"""
cleanup_index_trades.py
───────────────────────
Removes invalid index trades from all three tables in db/trades.db.

These trades were generated due to a bug where the bot traded directly on
INDEX symbols (NIFTY50, NIFTYBANK, FINNIFTY) instead of their options contracts.
The bug was fully fixed in commit c254f1e on 2026-05-08 11:35 IST.

Scope:
  • trades            — 5 index trades (DirectionalOptions strategy)
  • learning_trades   — 12 index trades (SimpleRSI, SimpleMomentum, DirectionalOptions_LRN)
  • paper_trades      — 7 index trades (MeanReversion, DirectionalOptions, DirectionalOptions_LRN)

Safety:
  • DRY RUN by default — pass --execute to actually delete
  • Creates a timestamped backup before any deletion
  • Only deletes trades with entry_time BEFORE the bug fix cutoff (2026-05-08 11:35:00)
    so any legitimate post-fix trades on similar symbols are never touched

Usage:
  python scripts/cleanup_index_trades.py           # dry run — preview only
  python scripts/cleanup_index_trades.py --execute # run for real
"""

import argparse
import os
import shutil
import sqlite3
from datetime import datetime

# ── Config ──────────────────────────────────────────────────────────────────
DB_PATH     = os.path.join(os.path.dirname(__file__), "..", "db", "trades.db")
BUG_FIX_AT  = "2026-05-08T11:35:00"   # last fix commit timestamp (IST)

# All symbol patterns that represent invalid index direct-trades.
# Using SQL LIKE patterns — covers NSE:NIFTY50-INDEX, NSE:NIFTYBANK-INDEX, NSE:FINNIFTY-INDEX
INDEX_PATTERNS = [
    "%-INDEX",
]

TABLES = ["trades", "learning_trades", "paper_trades"]

# ── Helpers ─────────────────────────────────────────────────────────────────

def build_where(table: str) -> str:
    sym_clauses = " OR ".join(f"symbol LIKE '{p}'" for p in INDEX_PATTERNS)
    return f"({sym_clauses}) AND entry_time < '{BUG_FIX_AT}'"


def preview(conn: sqlite3.Connection) -> dict:
    """Return {table: [rows]} of what would be deleted."""
    result = {}
    for table in TABLES:
        try:
            where = build_where(table)
            rows = conn.execute(
                f"SELECT id, symbol, strategy, direction, status, entry_price, entry_time "
                f"FROM {table} WHERE {where} ORDER BY entry_time"
            ).fetchall()
            result[table] = rows
        except sqlite3.OperationalError as e:
            print(f"  [SKIP] {table}: {e}")
            result[table] = []
    return result


def print_preview(rows_by_table: dict):
    total = sum(len(v) for v in rows_by_table.values())
    print(f"\n{'-'*60}")
    print(f"  Trades marked for deletion: {total}")
    print(f"{'-'*60}")
    for table, rows in rows_by_table.items():
        if not rows:
            print(f"\n  {table}: nothing to delete")
            continue
        print(f"\n  {table} ({len(rows)} row{'s' if len(rows)!=1 else ''}):")
        for r in rows:
            print(f"    {r[0]:<28} {r[1]:<30} {r[2]:<25} {r[4]:<8} {r[6][:16]}")
    print(f"\n{'-'*60}")


def backup_db(db_path: str) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = db_path.replace(".db", f"_backup_pre_cleanup_{ts}.db")
    shutil.copy2(db_path, backup_path)
    return backup_path


def delete(conn: sqlite3.Connection) -> dict:
    """Delete the invalid rows. Returns {table: rowcount}."""
    deleted = {}
    for table in TABLES:
        try:
            where = build_where(table)
            cur = conn.execute(f"DELETE FROM {table} WHERE {where}")
            deleted[table] = cur.rowcount
        except sqlite3.OperationalError as e:
            print(f"  [SKIP] {table}: {e}")
            deleted[table] = 0
    conn.commit()
    return deleted


def post_stats(conn: sqlite3.Connection):
    """Print clean record counts after deletion."""
    print("\n  Post-cleanup counts:")
    for table in TABLES:
        try:
            total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            remaining_idx = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {build_where(table)}"
            ).fetchone()[0]
            print(f"    {table:<25} {total} trades  |  index remaining: {remaining_idx}")
        except sqlite3.OperationalError as e:
            print(f"    {table:<25} (skipped: {e})")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Clean up invalid index trades from trading DB")
    parser.add_argument("--execute", action="store_true",
                        help="Actually delete records (default is dry run)")
    args = parser.parse_args()

    db_path = os.path.abspath(DB_PATH)
    if not os.path.exists(db_path):
        print(f"ERROR: Database not found at {db_path}")
        return

    print(f"\n  Database : {db_path}")
    print(f"  Cutoff   : entry_time < {BUG_FIX_AT}  (last bug fix commit 2026-05-08 11:35 IST)")
    print(f"  Mode     : {'EXECUTE — will delete' if args.execute else 'DRY RUN — preview only'}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    rows_by_table = preview(conn)
    print_preview(rows_by_table)

    total = sum(len(v) for v in rows_by_table.values())
    if total == 0:
        print("  Nothing to delete. Database is already clean.")
        conn.close()
        return

    if not args.execute:
        print("  Run with --execute to perform the deletion.\n")
        conn.close()
        return

    # Backup first
    backup_path = backup_db(db_path)
    print(f"\n  Backup   : {backup_path}")

    deleted = delete(conn)
    print(f"\n  Deleted:")
    for table, count in deleted.items():
        print(f"    {table:<25} {count} row{'s' if count!=1 else ''} removed")

    post_stats(conn)
    conn.close()
    print(f"\n  Done. Backup saved at:\n  {backup_path}\n")


if __name__ == "__main__":
    main()
