"""
resize_open_learning_unified.py  (Stage 2, one-off)
───────────────────────────────────────────────────
Resize OPEN learning paper-mirror trades to the unified sizing rule so the
dashboard immediately reflects Stage 2 (one sizer, one risk budget). New trades
already size this way via mirror_learning_open; this just brings the currently
open sandbox positions onto the same rule.

Rule (identical to live): qty = shares_to_fit(entry, ORIGINAL stop, risk_budget)
for equity, lots_to_fit for options, with risk_budget = learning_context().risk_budget
(= TOTAL_CAPITAL × RISK_PER_TRADE_PCT/100). Original stop comes from the learning
sibling's metadata (the trailed stop_loss must NOT be used for sizing).

Then rebuild the paper wallet = STARTING + Σrealised(CLOSED) − Σcapital_deployed(OPEN),
so the wallet stays consistent with the new deployed amounts.

Run:  python scripts/resize_open_learning_unified.py [--check]
Back up db/trades.db first.
"""
import json
import sqlite3
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from config.settings import DB_PATH
from execution import ledger
from execution.run_context import learning_context
from execution.sizing import shares_to_fit, lots_to_fit

IST = ZoneInfo("Asia/Kolkata")
STARTING = 500_000.0
MAX_LOTS = 10


def _orig_stop(conn, paper_id, fallback):
    """Original (entry-time) stop from the learning sibling metadata; fallback to given."""
    lrn = "LRN-" + paper_id[-8:]
    row = conn.execute("SELECT metadata FROM learning_trades WHERE id=?", (lrn,)).fetchone()
    if row and row[0]:
        try:
            m = json.loads(row[0])
            return float(m.get("original_stop") or fallback), lrn, m
        except Exception:
            pass
    return float(fallback), lrn, None


def main():
    check = "--check" in sys.argv
    rb = learning_context().risk_budget
    print(f"risk_budget = ₹{rb:,.0f}")

    # ── 1. Read + compute the resize plan (no open write transaction) ──
    plan = []
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM paper_trades WHERE status='OPEN' AND id LIKE 'PAPER-LRN-%'"
        ).fetchall()
        print(f"open learning mirrors: {len(rows)}")
        for r in rows:
            entry = float(r["entry_price"]); itype = r["instrument_type"] or ""
            old_qty = int(r["position_size"] or 0)
            ostop, lrn_id, meta = _orig_stop(conn, r["id"], r["stop_loss"])
            if itype == "nse_options":
                try:
                    lot = int((meta or {}).get("lot_size") or 1) or 1
                except Exception:
                    lot = 1
                lots = max(1, lots_to_fit(entry * lot, rb, rb, MAX_LOTS))
                qty, capdep = lots * lot, lots * lot * entry
                car = capdep
            else:
                qty, car, reason = shares_to_fit(entry, ostop, rb)
                if qty <= 0:
                    print(f"  SKIP {r['id']} {r['symbol']}: {reason}")
                    continue
                capdep = qty * entry * 0.25
            print(f"  {r['id']} {r['symbol']:22} qty {old_qty} → {qty}  capdep ₹{capdep:,.0f}")
            plan.append((r["id"], qty, round(capdep, 2), round(car, 2), lrn_id, meta))

    if check:
        print("[check] no changes written."); return

    # ── 2. Apply paper_trades resize + rebuild wallet (one tx, then commit) ──
    with sqlite3.connect(DB_PATH) as conn:
        for pid, qty, capdep, car, _lrn, _meta in plan:
            conn.execute(
                "UPDATE paper_trades SET position_size=?, capital_deployed=?, capital_at_risk=? WHERE id=?",
                (qty, capdep, car, pid),
            )
        realised = conn.execute(
            "SELECT COALESCE(SUM(realised_pnl),0) FROM paper_trades WHERE status='CLOSED'"
        ).fetchone()[0]
        deployed = conn.execute(
            "SELECT COALESCE(SUM(capital_deployed),0) FROM paper_trades WHERE status='OPEN'"
        ).fetchone()[0]
        new_bal = round(STARTING + float(realised) - float(deployed), 2)
        conn.execute("UPDATE paper_wallet SET balance=?, updated_at=? WHERE id=1",
                     (new_bal, datetime.now(tz=IST).isoformat()))
        conn.commit()

    # ── 3. Update learning metadata via the ledger (separate connections) ──
    for pid, qty, _capdep, _car, lrn_id, meta in plan:
        if meta is not None:
            meta["position_size"] = qty
            ledger.update_fields("nse", lrn_id, metadata=json.dumps(meta))

    print(f"wallet rebuilt → ₹{new_bal:,.2f}  (realised ₹{float(realised):,.0f}, deployed ₹{float(deployed):,.0f})")
    print("✓ resize complete.")


if __name__ == "__main__":
    main()
