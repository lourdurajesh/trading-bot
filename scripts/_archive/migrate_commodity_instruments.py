"""
scripts/migrate_commodity_instruments.py
─────────────────────────────────────────
One-time migration: replaces commodity_instruments table with the
full 23-instrument MCX universe from the dashboard reference sheet.

Min/Max prices are set to wide ranges so no valid spot is ever
blocked by the bounds check. Tighten them via the dashboard later
if you want to restrict a specific instrument.

Run once on prod before (or after) restarting the bot:
    python scripts/migrate_commodity_instruments.py

The script is idempotent — safe to re-run.
"""

import json
import os
import sqlite3
import sys

# Locate DB relative to this script
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "db", "trades.db")


# ── Master instrument table ──────────────────────────────────────────
# (name, display_name, lot_size, strike_step, typical_iv, price_unit,
#  min_price, max_price, rollover_buffer, valid_months)
#
# valid_months: [] = All Months  |  [3,5,7,9,12] = Mar/May/Jul/Sep/Dec etc.
# min/max_price: wide ranges — tighten per instrument via dashboard if needed.

INSTRUMENTS = [
    # ── Precious Metals ─────────────────────────────────────────────
    ("GOLD",        "Gold",             1,    500, 0.18, "INR/10gm",   80000, 300000,  7, [2,4,6,8,10,12]),
    ("GOLDM",       "Gold Mini",      100,    500, 0.18, "INR/10gm",   80000, 300000,  7, [2,4,6,8,10,12]),
    ("GOLDGUINEA",  "Gold Guinea",      8,    500, 0.18, "INR/8gm",    64000, 240000,  7, [2,4,6,8,10,12]),
    ("GOLDPETAL",   "Gold Petal",       1,    100, 0.20, "INR/1gm",     8000,  40000,  5, []),
    ("SILVER",      "Silver",          30,   1000, 0.28, "INR/kg",     40000, 200000,  7, [3,5,7,9,12]),
    ("SILVERM",     "Silver Mini",      5,   1000, 0.30, "INR/kg",     40000, 200000,  7, [3,5,7,9,12]),
    ("SILVERMIC",   "Silver Micro",     1,    500, 0.32, "INR/kg",     40000, 200000,  5, []),

    # ── Energy ──────────────────────────────────────────────────────
    ("CRUDEOIL",    "Crude Oil",      100,    100, 0.35, "INR/bbl",     2000,  20000,  3, []),
    ("CRUDEOILM",   "Crude Oil Mini",  10,    100, 0.38, "INR/bbl",     2000,  20000,  3, []),
    ("NATURALGAS",  "Natural Gas",   1250,      5, 0.45, "INR/mmBtu",    100,   2000,  3, []),
    ("NATGASMINI",  "Natural Gas Mini", 250,    5, 0.48, "INR/mmBtu",    100,   2000,  3, []),

    # ── Base Metals ─────────────────────────────────────────────────
    ("COPPER",      "Copper",           1,     10, 0.25, "INR/kg",       500,   5000,  5, []),
    ("COPPERM",     "Copper Mini",      1,     10, 0.28, "INR/kg",       500,   5000,  5, []),
    ("ZINC",        "Zinc",             5,      5, 0.22, "INR/kg",       100,   1000,  5, []),
    ("ZINCMINI",    "Zinc Mini",        1,      5, 0.24, "INR/kg",       100,   1000,  5, []),
    ("ALUMINIUM",   "Aluminium",        5,      2, 0.20, "INR/kg",       100,    600,  5, []),
    ("ALUMINI",     "Aluminium Mini",   1,      2, 0.22, "INR/kg",       100,    600,  5, []),
    ("LEAD",        "Lead",             5,      5, 0.24, "INR/kg",        80,    500,  5, []),
    ("LEADMINI",    "Lead Mini",        1,      5, 0.26, "INR/kg",        80,    500,  5, []),
    ("NICKEL",      "Nickel",         250,     20, 0.35, "INR/kg",       500,   8000,  5, []),
    ("NICKELM",     "Nickel Mini",     50,     20, 0.38, "INR/kg",       500,   8000,  5, []),

    # ── Agricultural ────────────────────────────────────────────────
    ("COTTON",      "Cotton",          25,    200, 0.30, "INR/bale",   30000, 120000,  7, []),
    ("MENTHAOIL",   "Mentha Oil",     360,     10, 0.40, "INR/kg",       300,   3000,  5, []),
]

NEW_NAMES = {row[0] for row in INSTRUMENTS}


def migrate(db_path: str) -> None:
    print(f"DB: {db_path}")
    if not os.path.exists(db_path):
        print("ERROR: DB file not found. Run the bot at least once to create it.")
        sys.exit(1)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row

        # ── Snapshot current state ───────────────────────────────────
        existing = {
            r["name"]: dict(r)
            for r in conn.execute("SELECT * FROM commodity_instruments").fetchall()
        }
        print(f"\nExisting instruments ({len(existing)}): {sorted(existing)}")
        print(f"New instruments     ({len(INSTRUMENTS)}): {sorted(NEW_NAMES)}")

        to_remove = sorted(existing.keys() - NEW_NAMES)
        to_add    = sorted(NEW_NAMES - existing.keys())
        to_update = sorted(NEW_NAMES & existing.keys())

        print(f"\nWill remove  : {to_remove or '(none)'}")
        print(f"Will add     : {to_add     or '(none)'}")
        print(f"Will update  : {to_update}")

        # ── Safety check: warn if removing instruments with open trades ──
        open_warning = []
        for name in to_remove:
            row = conn.execute(
                "SELECT COUNT(*) FROM commodity_learning_trades WHERE status='OPEN' AND instrument=?",
                (name,),
            ).fetchone()[0]
            if row:
                open_warning.append(f"  {name}: {row} open trade(s)")
        if open_warning:
            print("\nWARNING — open trades on instruments being removed:")
            print("\n".join(open_warning))
            print("These positions will remain in the DB but the instrument definition will be deleted.")

        # ── Confirm ──────────────────────────────────────────────────
        answer = input("\nProceed? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted.")
            sys.exit(0)

        # ── Remove stale instruments ─────────────────────────────────
        for name in to_remove:
            conn.execute("DELETE FROM commodity_instruments WHERE name=?", (name,))
            print(f"  removed: {name}")

        # ── Ensure display_name column exists (added later) ─────────
        try:
            conn.execute("ALTER TABLE commodity_instruments ADD COLUMN display_name TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass

        # ── Upsert all new instruments ───────────────────────────────
        for (name, dn, lot, step, iv, unit, mn, mx, buf, vm) in INSTRUMENTS:
            conn.execute(
                """
                INSERT INTO commodity_instruments
                    (name, display_name, lot_size, strike_step, typical_iv, price_unit,
                     min_price, max_price, rollover_buffer, valid_months, enabled)
                VALUES (?,?,?,?,?,?,?,?,?,?,1)
                ON CONFLICT(name) DO UPDATE SET
                    display_name   = excluded.display_name,
                    lot_size       = excluded.lot_size,
                    strike_step    = excluded.strike_step,
                    typical_iv     = excluded.typical_iv,
                    price_unit     = excluded.price_unit,
                    min_price      = excluded.min_price,
                    max_price      = excluded.max_price,
                    rollover_buffer= excluded.rollover_buffer,
                    valid_months   = excluded.valid_months,
                    enabled        = 1
                """,
                (name, dn, lot, step, iv, unit, mn, mx, buf, json.dumps(vm)),
            )
            action = "added  " if name in to_add else "updated"
            print(f"  {action}: {name:12s}  ({dn})  lot={lot:5}  buf={buf}  months={vm or 'all'}")

        print(f"\nDone — {len(INSTRUMENTS)} instruments in commodity_instruments.")

        # ── Verify final state ───────────────────────────────────────
        final = conn.execute(
            "SELECT name, lot_size, rollover_buffer, valid_months, enabled "
            "FROM commodity_instruments ORDER BY name"
        ).fetchall()
        print("\nFinal table:")
        print(f"  {'Name':<14} {'Lot':>6}  {'Buffer':>6}  {'Valid Months'}")
        print(f"  {'-'*14} {'-'*6}  {'-'*6}  {'-'*20}")
        for r in final:
            months = json.loads(r["valid_months"] or "[]") or "all"
            print(f"  {r['name']:<14} {r['lot_size']:>6}  {r['rollover_buffer']:>6}  {months}")


if __name__ == "__main__":
    migrate(DB_PATH)
