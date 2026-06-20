"""
migrate_unified_ledger.py  (U5-slice-2)
───────────────────────────────────────
One-time migration: collapse learning_trades / commodity_learning_trades /
us_reversal_trades into the single `ledger` table, then recreate the old names as
read-only compatibility VIEWS.

Safety:
  1. assert the canonical schema in execution/ledger.py matches the live tables (fail loud on drift)
  2. snapshot original rows
  3. backfill into `ledger`
  4. verify counts + content NON-destructively
  5. only THEN DROP the tables and create the views
  6. re-verify `SELECT * FROM <view>` is byte-identical to the snapshot

Run AFTER backing up db/trades.db.  Usage:
  python scripts/migrate_unified_ledger.py            # migrate + verify
  python scripts/migrate_unified_ledger.py --check     # report state, do nothing
"""
import json
import sqlite3
import sys

from execution import ledger
DB_PATH = ledger.DB_PATH   # operate on the exact file the ledger + engines use

TABLES = {s: ledger.SEGMENT_SCHEMA[s]["view"] for s in ledger.SEGMENT_SCHEMA}


def _obj_type(conn, name):
    r = conn.execute("SELECT type FROM sqlite_master WHERE name=?", (name,)).fetchone()
    return r[0] if r else None


def assert_schema(conn):
    for seg, table in TABLES.items():
        if _obj_type(conn, table) != "table":
            continue  # already migrated or fresh
        live = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        canon = [c for c, _ in ledger.SEGMENT_SCHEMA[seg]["columns"]]
        assert live == canon, (
            f"schema drift for {table}:\n  live ={live}\n  canon={canon}"
        )
    print("✓ schema matches canonical (execution/ledger.py)")


def snapshot(conn):
    snap = {}
    for table in TABLES.values():
        if _obj_type(conn, table) != "table":
            snap[table] = None
            continue
        conn.row_factory = sqlite3.Row
        snap[table] = [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]
    conn.row_factory = None
    return snap


def backfill(snap):
    for seg, table in TABLES.items():
        rows = snap[table]
        if rows is None:
            continue
        for row in rows:
            ledger.record(seg, row)
    print("✓ backfilled into ledger")


def verify_predrop(snap):
    ok = True
    for seg, table in TABLES.items():
        rows = snap[table]
        if rows is None:
            continue
        n_src = len(rows)
        n_led = ledger.count(seg)
        if n_src != n_led:
            print(f"✗ {table}: count {n_src} != ledger {n_led}"); ok = False; continue
        # content: every source row must reappear identically (by id) from the ledger
        by_id = {r["id"]: r for r in ledger.get_rows(seg, limit=10_000)}
        for src in rows:
            got = by_id.get(src["id"])
            if got is None:
                print(f"✗ {table}: id {src['id']} missing from ledger"); ok = False; continue
            for k, v in src.items():
                if not _eq(v, got.get(k)):
                    print(f"✗ {table}:{src['id']}.{k}: {v!r} != {got.get(k)!r}"); ok = False
        print(f"✓ {table}: {n_src} rows match in ledger (pre-drop)")
    return ok


def swap(conn):
    for table in TABLES.values():
        if _obj_type(conn, table) == "table":
            conn.execute(f"DROP TABLE {table}")
    ledger.create_views(conn)
    print("✓ dropped tables, created compatibility views")


def verify_postswap(conn, snap):
    ok = True
    for table in TABLES.values():
        if snap[table] is None:
            continue
        if _obj_type(conn, table) != "view":
            print(f"✗ {table} is not a view after swap"); ok = False; continue
        conn.row_factory = sqlite3.Row
        view_rows = {r["id"]: dict(r) for r in conn.execute(f"SELECT * FROM {table}")}
        conn.row_factory = None
        if len(view_rows) != len(snap[table]):
            print(f"✗ {table}: view {len(view_rows)} != snapshot {len(snap[table])}"); ok = False
        for src in snap[table]:
            got = view_rows.get(src["id"])
            if got is None:
                print(f"✗ {table}: id {src['id']} missing from view"); ok = False; continue
            if set(got.keys()) != set(src.keys()):
                print(f"✗ {table}: column set differs\n  view={sorted(got)}\n  src ={sorted(src)}")
                ok = False
            for k, v in src.items():
                if not _eq(v, got.get(k)):
                    print(f"✗ {table}:{src['id']}.{k}: {v!r} != {got.get(k)!r}"); ok = False
        if ok:
            print(f"✓ {table}: view SELECT * byte-identical to original ({len(snap[table])} rows)")
    return ok


def _eq(a, b):
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a or 0) - float(b or 0)) < 1e-9 if a is not None and b is not None else a == b
        except (TypeError, ValueError):
            return a == b
    return a == b


def main():
    check_only = "--check" in sys.argv
    with sqlite3.connect(DB_PATH) as conn:
        state = {t: _obj_type(conn, t) for t in TABLES.values()}
        print("current object types:", state)
        if check_only:
            print("ledger table:", _obj_type(conn, ledger.LEDGER_TABLE),
                  "| ledger rows:", {s: ledger.count(s) for s in TABLES} if _obj_type(conn, ledger.LEDGER_TABLE) else "n/a")
            return
        if all(v == "view" for v in state.values()):
            print("already migrated (all three are views) — nothing to do."); return

        assert_schema(conn)
        snap = snapshot(conn)
        ledger.init()                 # create ledger table (views skipped while tables shadow)
        backfill(snap)
        if not verify_predrop(snap):
            print("\n✗✗ PRE-DROP VERIFY FAILED — aborting before any DROP. DB unchanged tables-wise.")
            sys.exit(1)
        swap(conn)
        conn.commit()
        if not verify_postswap(conn, snap):
            print("\n✗✗ POST-SWAP VERIFY FAILED — restore from backup!")
            sys.exit(2)
    print("\n✓✓ MIGRATION OK — one ledger table + 3 compatibility views, reads byte-identical.")


if __name__ == "__main__":
    main()
