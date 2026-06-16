"""
migrate_audit_tables.py
────────────────────────
Creates new SQLite tables for the architecture enhancement spec.
Safe to run multiple times (uses IF NOT EXISTS).

Tables created:
  trade_decision_audit   — TASK 1: every strategy evaluation decision
  trade_outcome_metrics  — TASK 2: trade outcome attribution (Greeks, MFE/MAE)

Deploy:
    python scripts/migrate_audit_tables.py
"""

import sqlite3
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config.settings import DB_PATH


def migrate(db_path: str = DB_PATH) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")

    # ── TASK 1: Trade Decision Audit ─────────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trade_decision_audit (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp           TEXT    NOT NULL,
            symbol              TEXT    NOT NULL,
            strategy            TEXT    NOT NULL,
            conviction_score    REAL    DEFAULT 0,
            threshold           REAL    DEFAULT 0,
            market_regime       TEXT    DEFAULT '',
            iv                  REAL    DEFAULT 0,
            iv_percentile       REAL    DEFAULT 0,
            pcr                 REAL    DEFAULT 0,
            vix                 REAL    DEFAULT 0,
            breadth_score       REAL    DEFAULT 0,
            vwap_position       TEXT    DEFAULT '',
            opening_range_state TEXT    DEFAULT '',
            decision            TEXT    NOT NULL,
            rejection_reason    TEXT    DEFAULT '',
            data_snapshot       TEXT    DEFAULT '{}'
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tda_symbol   ON trade_decision_audit(symbol)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tda_decision ON trade_decision_audit(decision)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tda_ts       ON trade_decision_audit(timestamp)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tda_strategy ON trade_decision_audit(strategy)"
    )

    # ── TASK 2: Trade Outcome Metrics ─────────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trade_outcome_metrics (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id        TEXT    NOT NULL UNIQUE,
            entry_time      TEXT    NOT NULL,
            exit_time       TEXT    DEFAULT '',
            pnl             REAL    DEFAULT 0,
            pnl_r_multiple  REAL    DEFAULT 0,
            mfe             REAL    DEFAULT 0,
            mae             REAL    DEFAULT 0,
            theta_impact    REAL    DEFAULT 0,
            iv_change       REAL    DEFAULT 0,
            delta_entry     REAL    DEFAULT 0,
            gamma_entry     REAL    DEFAULT 0,
            vega_entry      REAL    DEFAULT 0,
            spread_slippage REAL    DEFAULT 0,
            exit_reason     TEXT    DEFAULT '',
            strategy        TEXT    DEFAULT '',
            symbol          TEXT    DEFAULT '',
            net_debit       REAL    DEFAULT 0,
            max_profit      REAL    DEFAULT 0,
            spread_quality_score REAL DEFAULT 0
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tom_trade_id ON trade_outcome_metrics(trade_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tom_strategy ON trade_outcome_metrics(strategy)"
    )

    conn.commit()
    conn.close()
    print(f"[migrate] Tables created / verified in {db_path}")
    print("[migrate] Tables: trade_decision_audit, trade_outcome_metrics")


if __name__ == "__main__":
    migrate()
