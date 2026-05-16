"""
trade_decision_audit.py
────────────────────────
Spec TASK 1 + TASK 2: Trade Decision Audit + Trade Outcome Metrics.

Logs every strategy evaluation (TRADE / NO_TRADE / REJECTED / DISABLED_BY_REGIME)
with full market context snapshot, and captures outcome attribution at close.

Feature flag: TRADE_AUDIT_ENABLED (default True).
If the DB write fails the main bot continues — this layer is never blocking.
"""

import json
import logging
import os
import sqlite3
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)

ENABLED = os.getenv("TRADE_AUDIT_ENABLED", "true").lower() != "false"

try:
    from config.settings import DB_PATH
except Exception:
    DB_PATH = "db/trades.db"


# ── Decision / Exit enums ────────────────────────────────────────
class Decision(str, Enum):
    TRADE              = "TRADE"
    NO_TRADE           = "NO_TRADE"
    REJECTED_BY_FILTER = "REJECTED_BY_FILTER"
    DISABLED_BY_REGIME = "DISABLED_BY_REGIME"


class ExitReason(str, Enum):
    TARGET      = "TARGET"
    STOPLOSS    = "STOPLOSS"
    TIME_EXIT   = "TIME_EXIT"
    MANUAL_EXIT = "MANUAL_EXIT"
    RISK_ENGINE = "RISK_ENGINE"
    EXPIRY_EXIT = "EXPIRY_EXIT"


# ── Data containers ──────────────────────────────────────────────
@dataclass
class AuditEntry:
    """Full context snapshot at the moment of a trade decision."""
    symbol:              str
    strategy:            str
    decision:            Decision
    rejection_reason:    str   = ""
    conviction_score:    float = 0.0
    threshold:           float = 0.0
    market_regime:       str   = ""
    iv:                  float = 0.0
    iv_percentile:       float = 0.0
    pcr:                 float = 0.0
    vix:                 float = 0.0
    breadth_score:       float = 0.0
    vwap_position:       str   = ""
    opening_range_state: str   = ""
    data_snapshot:       dict  = field(default_factory=dict)


@dataclass
class OutcomeEntry:
    """Greeks and attribution captured when a trade closes."""
    trade_id:        str
    entry_time:      str
    exit_time:       str   = ""
    pnl:             float = 0.0
    pnl_r_multiple:  float = 0.0
    mfe:             float = 0.0
    mae:             float = 0.0
    theta_impact:    float = 0.0
    iv_change:       float = 0.0
    delta_entry:     float = 0.0
    gamma_entry:     float = 0.0
    vega_entry:      float = 0.0
    spread_slippage: float = 0.0
    exit_reason:     str   = ""
    strategy:        str   = ""
    symbol:          str   = ""
    net_debit:       float = 0.0
    max_profit:      float = 0.0
    spread_quality_score: float = 0.0


# ── Audit engine ─────────────────────────────────────────────────
class TradeDecisionAudit:
    """
    Thread-safe, fail-safe audit logger.

    Usage:
        from analysis.trade_decision_audit import trade_decision_audit, AuditEntry, Decision

        trade_decision_audit.log(AuditEntry(
            symbol="CRUDEOIL", strategy="TrendSpread",
            decision=Decision.REJECTED_BY_FILTER,
            rejection_reason="spread quality score 3.2 < 5.0 threshold",
            ...
        ))
    """

    def __init__(self):
        self._lock = threading.Lock()
        if ENABLED:
            self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self) -> None:
        try:
            conn = self._connect()
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trade_decision_audit (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp           TEXT NOT NULL,
                    symbol              TEXT NOT NULL,
                    strategy            TEXT NOT NULL,
                    conviction_score    REAL DEFAULT 0,
                    threshold           REAL DEFAULT 0,
                    market_regime       TEXT DEFAULT '',
                    iv                  REAL DEFAULT 0,
                    iv_percentile       REAL DEFAULT 0,
                    pcr                 REAL DEFAULT 0,
                    vix                 REAL DEFAULT 0,
                    breadth_score       REAL DEFAULT 0,
                    vwap_position       TEXT DEFAULT '',
                    opening_range_state TEXT DEFAULT '',
                    decision            TEXT NOT NULL,
                    rejection_reason    TEXT DEFAULT '',
                    data_snapshot       TEXT DEFAULT '{}'
                )
            """)
            for idx_sql in [
                "CREATE INDEX IF NOT EXISTS idx_tda_symbol   ON trade_decision_audit(symbol)",
                "CREATE INDEX IF NOT EXISTS idx_tda_decision ON trade_decision_audit(decision)",
                "CREATE INDEX IF NOT EXISTS idx_tda_ts       ON trade_decision_audit(timestamp)",
                "CREATE INDEX IF NOT EXISTS idx_tda_strategy ON trade_decision_audit(strategy)",
            ]:
                conn.execute(idx_sql)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS trade_outcome_metrics (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_id        TEXT NOT NULL UNIQUE,
                    entry_time      TEXT NOT NULL,
                    exit_time       TEXT DEFAULT '',
                    pnl             REAL DEFAULT 0,
                    pnl_r_multiple  REAL DEFAULT 0,
                    mfe             REAL DEFAULT 0,
                    mae             REAL DEFAULT 0,
                    theta_impact    REAL DEFAULT 0,
                    iv_change       REAL DEFAULT 0,
                    delta_entry     REAL DEFAULT 0,
                    gamma_entry     REAL DEFAULT 0,
                    vega_entry      REAL DEFAULT 0,
                    spread_slippage REAL DEFAULT 0,
                    exit_reason     TEXT DEFAULT '',
                    strategy        TEXT DEFAULT '',
                    symbol          TEXT DEFAULT '',
                    net_debit       REAL DEFAULT 0,
                    max_profit      REAL DEFAULT 0,
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
        except Exception as e:
            logger.warning(f"[AuditDB] Init failed (non-fatal): {e}")

    # ── Public write API ─────────────────────────────────────────

    def log(self, entry: AuditEntry) -> None:
        """Log a trade decision (called once per strategy evaluation)."""
        if not ENABLED:
            return
        try:
            with self._lock:
                conn = self._connect()
                conn.execute("""
                    INSERT INTO trade_decision_audit (
                        timestamp, symbol, strategy,
                        conviction_score, threshold, market_regime,
                        iv, iv_percentile, pcr, vix, breadth_score,
                        vwap_position, opening_range_state,
                        decision, rejection_reason, data_snapshot
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    datetime.now(tz=IST).isoformat(),
                    entry.symbol, entry.strategy,
                    entry.conviction_score, entry.threshold, entry.market_regime,
                    entry.iv, entry.iv_percentile, entry.pcr, entry.vix, entry.breadth_score,
                    entry.vwap_position, entry.opening_range_state,
                    entry.decision.value, entry.rejection_reason,
                    json.dumps(entry.data_snapshot),
                ))
                conn.commit()
                conn.close()
        except Exception as e:
            logger.debug(f"[AuditDB] log() failed (non-fatal): {e}")

    def log_outcome(self, entry: OutcomeEntry) -> None:
        """
        Record outcome attribution when a trade closes.
        UPSERT — safe to call multiple times for the same trade_id.
        """
        if not ENABLED:
            return
        try:
            with self._lock:
                conn = self._connect()
                conn.execute("""
                    INSERT INTO trade_outcome_metrics (
                        trade_id, entry_time, exit_time, pnl, pnl_r_multiple,
                        mfe, mae, theta_impact, iv_change,
                        delta_entry, gamma_entry, vega_entry,
                        spread_slippage, exit_reason, strategy, symbol,
                        net_debit, max_profit, spread_quality_score
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(trade_id) DO UPDATE SET
                        exit_time=excluded.exit_time,
                        pnl=excluded.pnl,
                        pnl_r_multiple=excluded.pnl_r_multiple,
                        mfe=excluded.mfe, mae=excluded.mae,
                        theta_impact=excluded.theta_impact,
                        iv_change=excluded.iv_change,
                        exit_reason=excluded.exit_reason
                """, (
                    entry.trade_id, entry.entry_time, entry.exit_time,
                    entry.pnl, entry.pnl_r_multiple,
                    entry.mfe, entry.mae, entry.theta_impact, entry.iv_change,
                    entry.delta_entry, entry.gamma_entry, entry.vega_entry,
                    entry.spread_slippage, entry.exit_reason, entry.strategy, entry.symbol,
                    entry.net_debit, entry.max_profit, entry.spread_quality_score,
                ))
                conn.commit()
                conn.close()
        except Exception as e:
            logger.debug(f"[AuditDB] log_outcome() failed (non-fatal): {e}")

    # ── Public read API (dashboard) ───────────────────────────────

    def get_recent_decisions(self, limit: int = 200, decision: Optional[str] = None) -> list[dict]:
        try:
            conn = sqlite3.connect(DB_PATH)
            if decision:
                rows = conn.execute(
                    "SELECT * FROM trade_decision_audit WHERE decision=? ORDER BY id DESC LIMIT ?",
                    (decision, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM trade_decision_audit ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            cols = [d[0] for d in conn.execute(
                "SELECT * FROM trade_decision_audit LIMIT 0"
            ).description or []]
            conn.close()
            return [dict(zip(cols, r)) for r in rows]
        except Exception:
            return []

    def get_decision_stats(self) -> dict:
        """Counts by decision type — for dashboard badge."""
        try:
            conn = sqlite3.connect(DB_PATH)
            total = conn.execute(
                "SELECT COUNT(*) FROM trade_decision_audit"
            ).fetchone()[0]
            by_dec = dict(conn.execute(
                "SELECT decision, COUNT(*) FROM trade_decision_audit GROUP BY decision"
            ).fetchall())
            today_total = conn.execute(
                "SELECT COUNT(*) FROM trade_decision_audit WHERE timestamp >= date('now')"
            ).fetchone()[0]
            conn.close()
            return {
                "total":       total,
                "today":       today_total,
                "by_decision": by_dec,
            }
        except Exception:
            return {}

    def get_recent_outcomes(self, limit: int = 100) -> list[dict]:
        try:
            conn = sqlite3.connect(DB_PATH)
            rows = conn.execute(
                "SELECT * FROM trade_outcome_metrics ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            cols = [d[0] for d in conn.execute(
                "SELECT * FROM trade_outcome_metrics LIMIT 0"
            ).description or []]
            conn.close()
            return [dict(zip(cols, r)) for r in rows]
        except Exception:
            return []


# Singleton
trade_decision_audit = TradeDecisionAudit()
