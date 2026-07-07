"""
ledger.py
─────────
SINGLE trades store for all paper/learning segments (U5-slice-2). Collapses the three
per-engine tables — `learning_trades` (NSE), `commodity_learning_trades` (MCX),
`us_reversal_trades` (US) — into ONE physical table, `ledger`, with a `segment` column.

The guardrail wants "one store + segment column"; the obstacle is that the three schemas
are genuinely different (NSE RSI/EMA metadata, MCX spread legs/greeks, US BS-premium). So
each trade's full row is stored as a JSON **payload** and the segment-specific columns live
in DATA, not in three forked tables. A few columns (id, segment, status, symbol, strategy,
entry/exit_time) are promoted out of the payload for indexed filtering and ordering.

To keep every existing reader working byte-identically, the OLD table names are recreated
as read-only **compatibility VIEWS** over `ledger` (one per segment), projecting the payload
back to the original columns in the original order. So:
  • engine reads  (`SELECT * FROM learning_trades …`)            — unchanged, hit the view
  • external reads (analysis/*, run_analysis, trading-review skill) — unchanged, hit the view
  • engine WRITES  — cut over to `record()` / `update_fields()` here (the one write path)

When U6 lands, its single orchestrator records every trade through `record()`.

Canonical per-segment schemas (column order + default) below are asserted against the live
tables during migration (scripts/migrate_unified_ledger.py) so a transcription drift fails
loudly instead of silently dropping a column.
"""
import json
import math
import sqlite3
from typing import Optional

# Single source for the trades-DB path. config.settings reads $DB_PATH from .env
# (default "db/trades.db"); every engine imports DB_PATH from there too, so the ledger
# always writes/reads the exact file the engines read their views from.
from config.settings import DB_PATH

LEDGER_TABLE = "ledger"

# Promoted (indexed) columns — also live inside the payload; these are the copy used for
# WHERE/ORDER so we never have to parse JSON to filter.
_PROMOTED = ("id", "segment", "status", "symbol", "strategy", "entry_time", "exit_time")

# Canonical schema per segment: ordered list of (column, default). Order MUST match the
# live CREATE TABLE (incl. ALTER-added columns) so `SELECT *` on the view is positionally
# identical to the old table. default=None ⇒ SQL NULL.
SEGMENT_SCHEMA: dict[str, dict] = {
    "nse": {
        "view": "learning_trades",
        "columns": [
            ("id", None), ("symbol", None), ("strategy", None), ("direction", None),
            ("entry_price", None), ("exit_price", 0), ("stop_loss", None), ("target", None),
            ("rr_planned", None), ("pnl_pts", 0), ("pnl_r", 0), ("status", "OPEN"),
            ("exit_reason", ""), ("entry_time", None), ("exit_time", ""), ("metadata", "{}"),
            ("mae_pts", 0), ("mfe_pts", 0), ("fees", 0),
        ],
    },
    "mcx": {
        "view": "commodity_learning_trades",
        "columns": [
            ("id", None), ("symbol", None), ("instrument", None), ("direction", None),
            ("opt_type", None), ("strategy", None), ("spot_at_entry", None),
            ("spot_at_exit", 0), ("atm_strike", None), ("otm_strike", None),
            ("net_debit", None), ("max_profit", None), ("spread_width", None), ("rr", None),
            ("iv_used", None), ("lot_size", None), ("risk_per_lot", None), ("dte", None),
            ("pnl_approx", 0), ("pnl_r", 0), ("status", "OPEN"), ("exit_reason", ""),
            ("data_source", "bs_estimate"), ("entry_time", None), ("exit_time", ""),
            ("metadata", "{}"), ("lots", 1), ("trade_mode", "PAPER"),
            ("pnl_source", "ESTIMATE"), ("fees", 0),
        ],
    },
    "us": {
        "view": "us_reversal_trades",
        "columns": [
            ("id", None), ("symbol", None), ("strategy", None), ("status", None),
            ("entry_time", None), ("entry_spot", None), ("strike", None), ("dte", None),
            ("iv", None), ("entry_premium", None), ("sl_pct", None), ("trail_pct", None),
            ("exit_time", None), ("exit_spot", None), ("exit_premium", None), ("pnl", None),
            ("exit_reason", None), ("peak_spot", None), ("stop_spot", None),
        ],
    },
    # Production book (PAPER/LIVE) — the portfolio_tracker `trades` table. Column order MUST
    # match the live CREATE TABLE + ALTER order so the `trades` compat VIEW's SELECT * is
    # positionally identical and every existing reader (dashboard _collect_book_trades,
    # analysis, scripts) is unchanged. Writes cut over to record()/update_fields() (slice 1b).
    "live": {
        "view": "trades",
        # Order MUST match the LIVE physical table (verified via PRAGMA): target_2 was
        # ALTER-added on the prod DB so it sits AFTER exit_time, not mid-table. Matching the
        # live order keeps the `trades` view's SELECT * positionally identical for migration.
        "columns": [
            ("id", None), ("symbol", None), ("strategy", None), ("direction", None),
            ("signal_type", "EQUITY"), ("entry_price", None), ("exit_price", 0),
            ("stop_loss", None), ("target_1", None),
            ("position_size", None), ("capital_at_risk", None), ("realised_pnl", 0),
            ("status", "OPEN"), ("exit_reason", ""), ("entry_time", None), ("exit_time", None),
            ("target_2", 0),
            ("hold_type", "intraday"), ("original_stop_loss", 0), ("sl_order_id", ""),
            ("options_meta", "{}"), ("t1_hit", 0), ("monitor_symbol", ""),
            # Appended (safe: json_extract-based view, name-addressed by every
            # reader — no positional dependency for new trailing columns).
            # PositionManager tracks MAE/MFE for every book since slice 6c;
            # without these columns update_fields(mae_pts=..., mfe_pts=...)
            # was silently dropped by _full_row's schema projection.
            ("mae_pts", 0), ("mfe_pts", 0),
            # R-multiple, computed once in PortfolioTracker.close_position_by_id/
            # force_close/force_close_by_id (the one shared close path every book
            # uses) — never recomputed per-reader.
            ("pnl_r", 0),
        ],
    },
}


def _segment(seg: str) -> str:
    s = (seg or "").lower()
    if s not in SEGMENT_SCHEMA:
        raise ValueError(f"unknown ledger segment '{seg}' — expected {list(SEGMENT_SCHEMA)}")
    return s


def _sanitize(v):
    """JSON-safe: NaN/Inf → None (recursively). SQLite can hold NaN in a REAL column but
    JSON cannot, and json_extract on 'NaN' would fail — so we normalise on write."""
    if isinstance(v, float):
        return None if (math.isnan(v) or math.isinf(v)) else v
    if isinstance(v, dict):
        return {k: _sanitize(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_sanitize(x) for x in v]
    return v


def _full_row(seg: str, row: dict) -> dict:
    """Project a (possibly partial) engine row onto the segment's full column set, filling
    defaults for any missing column so the view's `SELECT *` shape never changes."""
    out = {}
    for col, default in SEGMENT_SCHEMA[seg]["columns"]:
        out[col] = _sanitize(row[col]) if col in row else default
    return out


# ── DDL ───────────────────────────────────────────────────────────

def _create_ledger(conn) -> None:
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {LEDGER_TABLE} (
            id          TEXT NOT NULL,
            segment     TEXT NOT NULL,
            status      TEXT DEFAULT 'OPEN',
            symbol      TEXT,
            strategy    TEXT,
            entry_time  TEXT,
            exit_time   TEXT DEFAULT '',
            payload     TEXT NOT NULL DEFAULT '{{}}',
            PRIMARY KEY (segment, id)
        )
    """)
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_ledger_seg_status ON {LEDGER_TABLE}(segment, status)")
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_ledger_seg_entry  ON {LEDGER_TABLE}(segment, entry_time)")


def _view_sql(seg: str) -> str:
    schema = SEGMENT_SCHEMA[seg]
    cols = ",\n            ".join(
        f"json_extract(payload, '$.{c}') AS {c}" for c, _ in schema["columns"]
    )
    return (
        f"CREATE VIEW {schema['view']} AS\n"
        f"        SELECT\n            {cols}\n"
        f"        FROM {LEDGER_TABLE} WHERE segment = '{seg}'"
    )


def create_views(conn) -> None:
    """(Re)create the three compatibility views. Caller is responsible for ensuring no
    real table of the same name exists (the migration drops them first)."""
    for seg, schema in SEGMENT_SCHEMA.items():
        conn.execute(f"DROP VIEW IF EXISTS {schema['view']}")
        conn.execute(_view_sql(seg))


# ── Ledger instance (DB-path injectable) ──────────────────────────

class Ledger:
    """Trades store bound to ONE DB file.

    The default instance (`_default`) uses `settings.DB_PATH` — the live `trades.db` every
    engine reads today, so the module-level functions below behave byte-identically to before.
    A second runtime (the online forward-test harness, slice 6) constructs `Ledger("db/learning.db")`
    so its high-volume bake-off trades live in an isolated file — same code, no schema fork,
    no write contention with the live book. Asset/segment differences stay in DATA (the JSON
    payload + per-segment SEGMENT_SCHEMA), never in forked store logic.
    """

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path

    def init(self) -> None:
        """Ensure the ledger table + compatibility views exist. Safe to call repeatedly and
        from every engine's startup. Only creates a view when no real table shadows the name
        (so a not-yet-migrated DB is left untouched until the migration runs)."""
        with sqlite3.connect(self.db_path) as conn:
            _create_ledger(conn)
            for seg, schema in SEGMENT_SCHEMA.items():
                name = schema["view"]
                existing = conn.execute(
                    "SELECT type FROM sqlite_master WHERE name=?", (name,)
                ).fetchone()
                if existing is None:
                    conn.execute(_view_sql(seg))       # fresh DB → create the view
                # if a real TABLE still shadows the name, the migration hasn't run yet —
                # leave it; engines keep reading the table until cutover.

    # ── Writes (the one write path) ──────────────────────────────
    def record(self, segment: str, row: dict) -> None:
        """Insert or replace a trade. `row` is the engine's column-named dict; missing columns
        are defaulted. Promotes the indexed columns out of the payload."""
        seg = _segment(segment)
        full = _full_row(seg, row)
        payload = json.dumps(full)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO {LEDGER_TABLE} "
                f"(id, segment, status, symbol, strategy, entry_time, exit_time, payload) "
                f"VALUES (?,?,?,?,?,?,?,?)",
                (full.get("id"), seg, full.get("status"), full.get("symbol"),
                 full.get("strategy"), full.get("entry_time"), full.get("exit_time"), payload),
            )

    def update_fields(self, segment: str, trade_id: str, **fields) -> None:
        """Read-modify-write: merge `fields` into the stored payload for (segment, id) and
        refresh any promoted columns that changed. Replaces the old per-table UPDATE … SET …
        statements (close, trailing-stop update, metadata update)."""
        seg = _segment(segment)
        clean = {k: _sanitize(v) for k, v in fields.items()}
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                f"SELECT payload FROM {LEDGER_TABLE} WHERE segment=? AND id=?", (seg, trade_id)
            ).fetchone()
            if cur is None:
                return
            data = json.loads(cur[0])
            data.update(clean)
            data = _full_row(seg, data)            # keep full shape + sanitise
            conn.execute(
                f"UPDATE {LEDGER_TABLE} SET status=?, symbol=?, strategy=?, entry_time=?, "
                f"exit_time=?, payload=? WHERE segment=? AND id=?",
                (data.get("status"), data.get("symbol"), data.get("strategy"),
                 data.get("entry_time"), data.get("exit_time"), json.dumps(data), seg, trade_id),
            )

    # ── Reads (for backfill / verification / future direct use) ──
    def get_rows(self, segment: str, status: Optional[str] = None, limit: Optional[int] = None) -> list:
        """Full row dicts for a segment, newest first — identical keys to the old `SELECT *`."""
        seg = _segment(segment)
        q = f"SELECT payload FROM {LEDGER_TABLE} WHERE segment=?"
        params: list = [seg]
        if status:
            q += " AND status=?"; params.append(status.upper())
        q += " ORDER BY entry_time DESC"
        if limit is not None:
            q += " LIMIT ?"; params.append(int(limit))
        with sqlite3.connect(self.db_path) as conn:
            return [json.loads(r[0]) for r in conn.execute(q, params)]

    def count(self, segment: str, status: Optional[str] = None) -> int:
        seg = _segment(segment)
        q = f"SELECT COUNT(*) FROM {LEDGER_TABLE} WHERE segment=?"
        params: list = [seg]
        if status:
            q += " AND status=?"; params.append(status.upper())
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute(q, params).fetchone()[0]


# ── Default instance + module-level delegators (back-compat) ──────
# Existing callers use `ledger.record(...)`, `ledger.init()`, etc. — unchanged. They route to
# the default instance bound to the live DB_PATH.
_default = Ledger(DB_PATH)


def init() -> None:
    _default.init()


def record(segment: str, row: dict) -> None:
    _default.record(segment, row)


def update_fields(segment: str, trade_id: str, **fields) -> None:
    _default.update_fields(segment, trade_id, **fields)


def get_rows(segment: str, status: Optional[str] = None, limit: Optional[int] = None) -> list:
    return _default.get_rows(segment, status, limit)


def count(segment: str, status: Optional[str] = None) -> int:
    return _default.count(segment, status)
