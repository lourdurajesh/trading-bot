"""
learning_engine.py
──────────────────
Runs simple learning paper trades independently of the main strategy loop.

What it does every cycle:
  1. Evaluates SimpleRSI + SimpleMomentum on the learning watchlist
  2. Opens paper positions when a signal fires (if not already in one)
  3. Monitors open positions against stop/target
  4. Logs everything to learning_trades table (rich metadata for review)

The learning trades are PAPER ONLY and completely isolated from the
production risk manager and order manager.

Access results via:
  GET /learning/trades   — all trades (open + closed)
  GET /learning/stats    — win rate, avg R, top patterns
  GET /learning/review   — grouped by outcome with metadata
"""

import json
import logging
import sqlite3
import uuid
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

IST    = ZoneInfo("Asia/Kolkata")
DB_PATH = "db/trades.db"

logger = logging.getLogger(__name__)


class LearningEngine:

    def __init__(self):
        self._open_positions: dict[str, dict] = {}  # symbol+strategy → trade
        self._init_db()
        self._restore_open_positions()

    # ─────────────────────────────────────────────────────────────
    # PUBLIC — called from main loop
    # ─────────────────────────────────────────────────────────────

    def run_cycle(self) -> None:
        """Evaluate learning strategies and manage open positions."""
        from config.learning_watchlist import ALL_LEARNING_SYMBOLS, COMMODITY_SYMBOLS
        from strategies.simple_rsi       import SimpleRSIStrategy
        from strategies.simple_momentum  import SimpleMomentumStrategy
        from data.data_store              import store

        strategies = [SimpleRSIStrategy(), SimpleMomentumStrategy()]

        # ── 1. Monitor existing open positions ───────────────────
        self._check_exits(store)

        # ── 2. Look for new entries ───────────────────────────────
        for symbol in ALL_LEARNING_SYMBOLS:
            for strat in strategies:
                key = f"{symbol}:{strat.name}"
                if key in self._open_positions:
                    continue  # already in this trade

                try:
                    signal = strat.evaluate(symbol)
                except Exception as exc:
                    logger.debug(f"[Learning] {strat.name}/{symbol} error: {exc}")
                    continue

                if signal:
                    self._open_trade(signal)

    # ─────────────────────────────────────────────────────────────
    # TRADE MANAGEMENT
    # ─────────────────────────────────────────────────────────────

    def _open_trade(self, signal: dict) -> None:
        symbol   = signal["symbol"]
        strategy = signal["strategy"]
        key      = f"{symbol}:{strategy}"

        trade_id = f"LRN-{uuid.uuid4().hex[:8].upper()}"
        now_str  = datetime.now(tz=IST).isoformat()

        trade = {
            "id":           trade_id,
            "symbol":       symbol,
            "strategy":     strategy,
            "direction":    signal["direction"],
            "entry_price":  signal["entry_price"],
            "stop_loss":    signal["stop_loss"],
            "target":       signal["target"],
            "rr":           signal["rr"],
            "metadata":     signal.get("metadata", {}),
            "entry_time":   now_str,
            "status":       "OPEN",
            "mae_pts":      0.0,
            "mfe_pts":      0.0,
        }

        self._open_positions[key] = trade
        self._db_insert(trade)
        logger.info(
            f"[Learning] OPEN {trade_id} | {strategy} {signal['direction']} {symbol} "
            f"@ {signal['entry_price']:.2f} | SL {signal['stop_loss']:.2f} "
            f"T {signal['target']:.2f} | R:R {signal['rr']:.1f}"
        )

    def _check_exits(self, store) -> None:
        closed_keys = []

        for key, trade in list(self._open_positions.items()):
            symbol = trade["symbol"]
            ltp    = store.get_ltp(symbol)
            if not ltp:
                continue

            direction  = trade["direction"]
            stop       = trade["stop_loss"]
            target     = trade["target"]
            entry      = trade["entry_price"]
            exit_reason = None
            exit_price  = None

            # Update MAE/MFE using current LTP
            if direction == "LONG":
                adverse    = entry - ltp
                favourable = ltp - entry
            else:
                adverse    = ltp - entry
                favourable = entry - ltp
            trade["mae_pts"] = max(trade["mae_pts"], adverse)
            trade["mfe_pts"] = max(trade["mfe_pts"], favourable)

            if direction == "LONG":
                if ltp <= stop:
                    exit_reason, exit_price = "STOP",   ltp
                elif ltp >= target:
                    exit_reason, exit_price = "TARGET", ltp
            else:
                if ltp >= stop:
                    exit_reason, exit_price = "STOP",   ltp
                elif ltp <= target:
                    exit_reason, exit_price = "TARGET", ltp

            # EOD forced close at 15:20 IST
            now_time = datetime.now(tz=IST).time()
            from datetime import time as dtime
            if now_time >= dtime(15, 20) and exit_reason is None:
                exit_reason = "EOD"
                exit_price  = ltp

            if exit_reason:
                pnl_pts = (exit_price - entry) if direction == "LONG" else (entry - exit_price)
                pnl_r   = round(pnl_pts / abs(entry - stop), 2) if abs(entry - stop) > 0 else 0
                self._db_close(
                    trade["id"], exit_price, exit_reason, pnl_pts, pnl_r,
                    trade["mae_pts"], trade["mfe_pts"],
                )
                closed_keys.append(key)
                logger.info(
                    f"[Learning] CLOSE {trade['id']} | {exit_reason} @ {exit_price:.2f} "
                    f"| PnL {pnl_pts:+.2f} pts ({pnl_r:+.1f}R)"
                )

        for k in closed_keys:
            del self._open_positions[k]

    # ─────────────────────────────────────────────────────────────
    # DB
    # ─────────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS learning_trades (
                    id           TEXT PRIMARY KEY,
                    symbol       TEXT,
                    strategy     TEXT,
                    direction    TEXT,
                    entry_price  REAL,
                    exit_price   REAL DEFAULT 0,
                    stop_loss    REAL,
                    target       REAL,
                    rr_planned   REAL,
                    pnl_pts      REAL DEFAULT 0,
                    pnl_r        REAL DEFAULT 0,
                    status       TEXT DEFAULT 'OPEN',
                    exit_reason  TEXT DEFAULT '',
                    entry_time   TEXT,
                    exit_time    TEXT DEFAULT '',
                    metadata     TEXT DEFAULT '{}',
                    mae_pts      REAL DEFAULT 0,
                    mfe_pts      REAL DEFAULT 0
                )
            """)
            # Safe migration for pre-existing tables
            for col in ("mae_pts", "mfe_pts"):
                try:
                    conn.execute(f"ALTER TABLE learning_trades ADD COLUMN {col} REAL DEFAULT 0")
                except Exception:
                    pass
        logger.info("[Learning] DB table ready")

    def _restore_open_positions(self) -> None:
        """Reload OPEN positions from DB into memory — prevents duplicates across restarts."""
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM learning_trades WHERE status='OPEN'"
            ).fetchall()
        for r in rows:
            trade = dict(r)
            try:
                trade["metadata"] = json.loads(trade.get("metadata") or "{}")
            except Exception:
                trade["metadata"] = {}
            trade["mae_pts"] = trade.get("mae_pts") or 0.0
            trade["mfe_pts"] = trade.get("mfe_pts") or 0.0
            key = f"{trade['symbol']}:{trade['strategy']}"
            self._open_positions[key] = trade
        if self._open_positions:
            logger.info(f"[Learning] Restored {len(self._open_positions)} open positions from DB")

    def _db_insert(self, trade: dict) -> None:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                INSERT INTO learning_trades
                (id, symbol, strategy, direction, entry_price, stop_loss,
                 target, rr_planned, status, entry_time, metadata)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (
                trade["id"], trade["symbol"], trade["strategy"],
                trade["direction"], trade["entry_price"], trade["stop_loss"],
                trade["target"], trade["rr"], trade["status"],
                trade["entry_time"],
                json.dumps(trade.get("metadata", {})),
            ))

    def _db_close(
        self, trade_id: str, exit_price: float,
        exit_reason: str, pnl_pts: float, pnl_r: float,
        mae_pts: float = 0.0, mfe_pts: float = 0.0,
    ) -> None:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                UPDATE learning_trades
                SET exit_price=?, exit_reason=?, pnl_pts=?, pnl_r=?,
                    status='CLOSED', exit_time=?, mae_pts=?, mfe_pts=?
                WHERE id=?
            """, (
                exit_price, exit_reason,
                round(pnl_pts, 2), round(pnl_r, 2),
                datetime.now(tz=IST).isoformat(),
                round(mae_pts, 2), round(mfe_pts, 2),
                trade_id,
            ))

    # ─────────────────────────────────────────────────────────────
    # READ API — used by dashboard
    # ─────────────────────────────────────────────────────────────

    def get_trades(self, status: Optional[str] = None, limit: int = 200) -> list[dict]:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            if status:
                rows = conn.execute(
                    "SELECT * FROM learning_trades WHERE status=? ORDER BY entry_time DESC LIMIT ?",
                    (status.upper(), limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM learning_trades ORDER BY entry_time DESC LIMIT ?",
                    (limit,),
                ).fetchall()

        result = []
        for r in rows:
            d = dict(r)
            try:
                d["metadata"] = json.loads(d.get("metadata") or "{}")
            except Exception:
                d["metadata"] = {}
            result.append(d)
        return result

    def get_stats(self) -> dict:
        """Win rate, avg R, best/worst trades, breakdown by strategy."""
        trades = self.get_trades(status="CLOSED", limit=1000)
        if not trades:
            return {"total_closed": 0, "message": "No closed learning trades yet."}

        wins   = [t for t in trades if t["pnl_r"] > 0]
        losses = [t for t in trades if t["pnl_r"] <= 0]

        by_strategy: dict[str, dict] = {}
        for t in trades:
            s = t["strategy"]
            if s not in by_strategy:
                by_strategy[s] = {"total": 0, "wins": 0, "total_r": 0.0}
            by_strategy[s]["total"]   += 1
            by_strategy[s]["total_r"] += t["pnl_r"]
            if t["pnl_r"] > 0:
                by_strategy[s]["wins"] += 1

        for s, d in by_strategy.items():
            d["win_rate"] = round(d["wins"] / d["total"] * 100, 1) if d["total"] else 0
            d["avg_r"]    = round(d["total_r"] / d["total"], 2) if d["total"] else 0

        all_r = [t["pnl_r"] for t in trades]
        return {
            "total_closed":  len(trades),
            "total_open":    len(self.get_trades(status="OPEN")),
            "win_rate_pct":  round(len(wins) / len(trades) * 100, 1),
            "avg_r":         round(sum(all_r) / len(all_r), 2),
            "total_r":       round(sum(all_r), 2),
            "best_trade_r":  round(max(all_r), 2),
            "worst_trade_r": round(min(all_r), 2),
            "by_strategy":   by_strategy,
            "exit_reasons":  _count_field(trades, "exit_reason"),
            "directions":    _count_field(trades, "direction"),
        }

    def get_review(self, strategy: Optional[str] = None) -> list[dict]:
        """Returns closed trades grouped by outcome bucket for review."""
        trades = self.get_trades(status="CLOSED", limit=500)
        if strategy:
            trades = [t for t in trades if t["strategy"] == strategy]

        def bucket(r):
            if r >= 2.0:   return "strong_win"
            if r > 0:      return "small_win"
            if r >= -0.5:  return "scratch"
            if r >= -1.0:  return "small_loss"
            return "large_loss"

        for t in trades:
            t["outcome_bucket"] = bucket(t["pnl_r"])
        return trades


def _count_field(trades: list[dict], field: str) -> dict:
    c: dict = {}
    for t in trades:
        v = t.get(field, "unknown")
        c[v] = c.get(v, 0) + 1
    return c


# Module-level singleton
learning_engine = LearningEngine()
