"""
portfolio_tracker.py
────────────────────
Tracks all open and closed positions, calculates live P&L,
drawdown, win rate and other portfolio metrics.

Persists trade history to SQLite so nothing is lost on restart.
Feeds the dashboard API with real-time stats.
"""

import logging
import os
import sqlite3
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

from config.settings import DB_PATH, TOTAL_CAPITAL
from data.data_store import store
from strategies.base_strategy import Direction, Signal

logger = logging.getLogger(__name__)


@dataclass
class Position:
    """Represents an open or closed trade position."""
    id:              str             # unique trade id
    symbol:          str
    strategy:        str
    direction:       str             # LONG / SHORT
    signal_type:     str             # EQUITY / OPTIONS
    entry_price:     float
    stop_loss:       float
    target_1:        float
    target_2:        float
    position_size:   int
    capital_at_risk: float
    entry_time:      datetime
    exit_price:      float   = 0.0
    exit_time:       Optional[datetime] = None
    realised_pnl:    float   = 0.0
    status:          str     = "OPEN"   # OPEN | CLOSED | STOPPED | CANCELLED
    exit_reason:     str     = ""
    options_meta:    dict    = field(default_factory=dict)


class PortfolioTracker:
    """
    Manages all positions and provides portfolio metrics.

    Usage:
        from risk.portfolio_tracker import portfolio_tracker

        # Open a position after order fills
        portfolio_tracker.open_position(signal, fill_price)

        # Close when exit triggered
        portfolio_tracker.close_position(symbol, fill_price, reason)

        # Get live stats for dashboard
        stats = portfolio_tracker.get_stats()
    """

    def __init__(self):
        self._open_positions: dict[str, Position] = {}    # symbol → Position
        self._closed_trades:  list[Position]      = []
        self._peak_value      = TOTAL_CAPITAL
        self._trade_counter   = 0
        self._init_db()
        self._load_open_positions()

    # ─────────────────────────────────────────────────────────────
    # PUBLIC — position lifecycle
    # ─────────────────────────────────────────────────────────────

    def open_position(self, signal: Signal, fill_price: float) -> Position:
        """Record a newly filled trade entry."""
        self._trade_counter += 1
        trade_id = f"T{datetime.now(tz=timezone.utc).strftime('%Y%m%d%H%M%S')}-{self._trade_counter:04d}"

        position = Position(
            id              = trade_id,
            symbol          = signal.symbol,
            strategy        = signal.strategy,
            direction       = signal.direction.value,
            signal_type     = signal.signal_type.value,
            entry_price     = fill_price,
            stop_loss       = signal.stop_loss,
            target_1        = signal.target_1,
            target_2        = signal.target_2,
            position_size   = signal.position_size,
            capital_at_risk = signal.capital_at_risk,
            entry_time      = datetime.now(tz=timezone.utc),
            options_meta    = signal.options_meta,
        )

        self._open_positions[signal.symbol] = position
        self._save_position(position)

        logger.info(
            f"[Portfolio] OPENED {position.direction} {position.symbol} | "
            f"Fill: {fill_price:.2f} | Size: {position.position_size} | "
            f"SL: {position.stop_loss:.2f} | T1: {position.target_1:.2f} | "
            f"ID: {trade_id}"
        )
        return position

    def close_position(
        self,
        symbol: str,
        fill_price: float,
        reason: str = "manual",
    ) -> Optional[Position]:
        """Record a position exit and calculate P&L."""
        position = self._open_positions.pop(symbol, None)
        if not position:
            logger.warning(f"[Portfolio] close_position called for {symbol} but no open position found")
            return None

        position.exit_price  = fill_price
        position.exit_time   = datetime.now(tz=timezone.utc)
        position.exit_reason = reason
        position.status      = "CLOSED"

        # Calculate P&L
        if position.direction == "LONG":
            position.realised_pnl = (fill_price - position.entry_price) * position.position_size
        else:
            position.realised_pnl = (position.entry_price - fill_price) * position.position_size

        self._closed_trades.append(position)
        self._update_position_db(position)

        # Notify risk manager of P&L change
        from risk.risk_manager import risk_manager
        risk_manager.update_daily_pnl(position.realised_pnl)

        logger.info(
            f"[Portfolio] CLOSED {position.symbol} | "
            f"P&L: ₹{position.realised_pnl:+,.0f} | "
            f"Exit: {fill_price:.2f} | Reason: {reason}"
        )
        return position

    # ─────────────────────────────────────────────────────────────
    # PUBLIC — queries
    # ─────────────────────────────────────────────────────────────

    def get_open_positions(self) -> list[dict]:
        """Returns all open positions with live unrealised P&L."""
        result = []
        for symbol, pos in self._open_positions.items():
            ltp = store.get_ltp(symbol) or pos.entry_price
            if pos.direction == "LONG":
                unrealised = (ltp - pos.entry_price) * pos.position_size
            else:
                unrealised = (pos.entry_price - ltp) * pos.position_size

            result.append({
                "id":              pos.id,
                "symbol":          pos.symbol,
                "strategy":        pos.strategy,
                "direction":       pos.direction,
                "signal_type":     pos.signal_type,
                "entry_price":     pos.entry_price,
                "ltp":             ltp,
                "stop_loss":       pos.stop_loss,
                "target_1":        pos.target_1,
                "position_size":   pos.position_size,
                "capital_at_risk": pos.capital_at_risk,
                "unrealised_pnl":  round(unrealised, 2),
                "entry_time":      pos.entry_time.isoformat(),
            })
        return result

    def get_stats(self) -> dict:
        """Comprehensive portfolio stats for dashboard."""
        open_pos   = self.get_open_positions()
        closed     = self._closed_trades

        total_unrealised = sum(p["unrealised_pnl"] for p in open_pos)
        total_realised   = sum(p.realised_pnl for p in closed)

        # Win rate
        winners = [p for p in closed if p.realised_pnl > 0]
        losers  = [p for p in closed if p.realised_pnl <= 0]
        win_rate = len(winners) / len(closed) if closed else 0.0

        # Average R:R on closed trades
        avg_winner = sum(p.realised_pnl for p in winners) / len(winners) if winners else 0
        avg_loser  = abs(sum(p.realised_pnl for p in losers) / len(losers)) if losers else 1
        avg_rr     = avg_winner / avg_loser if avg_loser > 0 else 0

        # Drawdown
        portfolio_value = TOTAL_CAPITAL + total_realised + total_unrealised
        self._peak_value = max(self._peak_value, portfolio_value)
        drawdown_pct = ((self._peak_value - portfolio_value) / self._peak_value) * 100

        return {
            "total_capital":       TOTAL_CAPITAL,
            "portfolio_value":     round(portfolio_value, 2),
            "total_realised_pnl":  round(total_realised, 2),
            "total_unrealised_pnl": round(total_unrealised, 2),
            "total_pnl":           round(total_realised + total_unrealised, 2),
            "total_pnl_pct":       round((total_realised + total_unrealised) / TOTAL_CAPITAL * 100, 2),
            "open_positions_count": len(open_pos),
            "total_trades":        len(closed),
            "win_rate":            round(win_rate * 100, 1),
            "avg_rr":              round(avg_rr, 2),
            "drawdown_pct":        round(drawdown_pct, 2),
            "peak_value":          round(self._peak_value, 2),
            "open_positions":      open_pos,
        }

    def has_open_position(self, symbol: str) -> bool:
        return symbol in self._open_positions

    def get_position(self, symbol: str) -> Optional[Position]:
        return self._open_positions.get(symbol)

    # ─────────────────────────────────────────────────────────────
    # INTERNAL — SQLite persistence
    # ─────────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id              TEXT PRIMARY KEY,
                    symbol          TEXT,
                    strategy        TEXT,
                    direction       TEXT,
                    signal_type     TEXT,
                    entry_price     REAL,
                    exit_price      REAL,
                    stop_loss       REAL,
                    target_1        REAL,
                    position_size   INTEGER,
                    capital_at_risk REAL,
                    realised_pnl    REAL,
                    status          TEXT,
                    exit_reason     TEXT,
                    entry_time      TEXT,
                    exit_time       TEXT
                )
            """)
        logger.info(f"[Portfolio] Database initialised at {DB_PATH}")

    def _save_position(self, pos: Position) -> None:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO trades
                (id, symbol, strategy, direction, signal_type, entry_price,
                 exit_price, stop_loss, target_1, position_size, capital_at_risk,
                 realised_pnl, status, exit_reason, entry_time, exit_time)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                pos.id, pos.symbol, pos.strategy, pos.direction, pos.signal_type,
                pos.entry_price, pos.exit_price, pos.stop_loss, pos.target_1,
                pos.position_size, pos.capital_at_risk, pos.realised_pnl,
                pos.status, pos.exit_reason,
                pos.entry_time.isoformat() if pos.entry_time else None,
                pos.exit_time.isoformat()  if pos.exit_time  else None,
            ))

    def _update_position_db(self, pos: Position) -> None:
        self._save_position(pos)

    def _load_open_positions(self) -> None:
        """Reload any OPEN positions from DB on bot restart."""
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM trades WHERE status = 'OPEN'"
                ).fetchall()

            for row in rows:
                pos = Position(
                    id              = row["id"],
                    symbol          = row["symbol"],
                    strategy        = row["strategy"],
                    direction       = row["direction"],
                    signal_type     = row["signal_type"],
                    entry_price     = row["entry_price"],
                    stop_loss       = row["stop_loss"],
                    target_1        = row["target_1"],
                    target_2        = 0.0,
                    position_size   = row["position_size"],
                    capital_at_risk = row["capital_at_risk"],
                    entry_time      = datetime.fromisoformat(row["entry_time"]),
                )
                self._open_positions[pos.symbol] = pos
                logger.info(f"[Portfolio] Restored open position: {pos.symbol}")

        except Exception as e:
            logger.warning(f"[Portfolio] Could not restore positions: {e}")


# ── Module-level singleton ────────────────────────────────────────
portfolio_tracker = PortfolioTracker()
