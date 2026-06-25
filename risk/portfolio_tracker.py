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
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

from config.settings import DB_PATH, TOTAL_CAPITAL
from data.data_store import store
from execution import fees as txn_fees  # single source for transaction costs


def _fee_segment(signal_type: str) -> str:
    return "OPTIONS" if signal_type == "OPTIONS" else "EQUITY"
from strategies.base_strategy import Direction, Signal

logger = logging.getLogger(__name__)


@dataclass
class Position:
    """Represents an open or closed trade position."""
    id:                  str             # unique trade id
    symbol:              str
    strategy:            str
    direction:           str             # LONG / SHORT
    signal_type:         str             # EQUITY / OPTIONS
    entry_price:         float
    stop_loss:           float
    target_1:            float
    target_2:            float
    position_size:       int
    capital_at_risk:     float
    entry_time:          datetime
    hold_type:           str     = "intraday"   # "intraday" | "swing"
    exit_price:          float   = 0.0
    exit_time:           Optional[datetime] = None
    realised_pnl:        float   = 0.0
    status:              str     = "OPEN"   # OPEN | PENDING_CLOSE | CLOSED | STOPPED | CANCELLED
    exit_reason:         str     = ""
    options_meta:        dict    = field(default_factory=dict)
    # Immutable at entry — used for state reconstruction after restart (Bug 6)
    original_stop_loss:  float   = 0.0
    # Active broker SL order ID — needed to cancel before placing updated SL (Bug 2)
    sl_order_id:         str     = ""
    # Set to True the moment the T1 partial exit fires — survives restart (T1 edge case fix)
    t1_hit:              bool    = False
    # Symbol whose LTP drives stop/target checks (equity: same as symbol; options: NFO contract)
    monitor_symbol:      str     = ""


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

    def open_position(self, signal: Signal, fill_price: float,
                      paper: bool = False) -> Position:
        """Record a newly filled trade entry."""
        self._trade_counter += 1
        trade_id = f"T{datetime.now(tz=IST).strftime('%Y%m%d%H%M%S')}-{self._trade_counter:04d}"

        # Capture the underlying spot at entry for OPTIONS so the structural exit can gate on
        # underlying profit (the option's symbol IS the underlying index; entry_price is the
        # premium). Single source: PositionManager reads options_meta["entry_spot"].
        opts_meta = dict(signal.options_meta or {})
        if signal.signal_type.value == "OPTIONS" and "entry_spot" not in opts_meta:
            _spot = store.get_ltp(signal.symbol)
            if _spot and _spot > 0:
                opts_meta["entry_spot"] = float(_spot)

        position = Position(
            id                 = trade_id,
            symbol             = signal.symbol,
            strategy           = signal.strategy,
            direction          = signal.direction.value,
            signal_type        = signal.signal_type.value,
            entry_price        = fill_price,
            stop_loss          = signal.stop_loss,
            target_1           = signal.target_1,
            target_2           = signal.target_2,
            position_size      = signal.position_size,
            capital_at_risk    = signal.capital_at_risk,
            entry_time         = datetime.now(tz=IST),
            hold_type          = getattr(signal, "hold_type", "intraday"),
            options_meta       = opts_meta,
            original_stop_loss = signal.stop_loss,   # frozen at entry — never updated
            monitor_symbol     = getattr(signal, "monitor_symbol", "") or signal.symbol,
        )

        self._open_positions[signal.symbol] = position
        self._save_position(position)

        logger.info(
            f"[Portfolio] OPENED {position.direction} {position.symbol} | "
            f"Fill: {fill_price:.2f} | Size: {position.position_size} | "
            f"SL: {position.stop_loss:.2f} | T1: {position.target_1:.2f} | "
            f"ID: {trade_id}"
        )

        try:
            from audit_log import audit_log
            audit_log.position_opened(
                symbol      = signal.symbol,
                direction   = signal.direction.value,
                qty         = signal.position_size,
                fill_price  = fill_price,
                strategy    = signal.strategy,
                paper       = paper,
            )
        except Exception:
            pass

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
        position.exit_time   = datetime.now(tz=IST)
        position.exit_reason = reason
        position.status      = "CLOSED"

        # Calculate P&L net of round-trip transaction cost (single source)
        if position.direction == "LONG":
            gross = (fill_price - position.entry_price) * position.position_size
        else:
            gross = (position.entry_price - fill_price) * position.position_size
        fee = txn_fees.round_trip(_fee_segment(position.signal_type),
                                  position.entry_price, fill_price, position.position_size)
        position.realised_pnl = round(gross - fee, 2)

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

        try:
            from audit_log import audit_log
            audit_log.position_closed(
                symbol     = position.symbol,
                exit_price = fill_price,
                pnl        = position.realised_pnl,
                reason     = reason,
            )
        except Exception:
            pass

        return position

    # ─────────────────────────────────────────────────────────────
    # PUBLIC — queries
    # ─────────────────────────────────────────────────────────────

    def get_open_positions(self) -> list[dict]:
        """Returns all open positions with live unrealised P&L."""
        result = []
        for symbol, pos in self._open_positions.items():
            options_meta = pos.options_meta or {}
            nfo_sym = options_meta.get("nfo_symbol") if pos.signal_type == "OPTIONS" else None
            # Display price: live LTP, else last candle close (survives market close / restart)
            ltp = store.get_last_price(nfo_sym or symbol) or pos.entry_price
            if pos.direction == "LONG":
                gross = (ltp - pos.entry_price) * pos.position_size
            else:
                gross = (pos.entry_price - ltp) * pos.position_size
            # Entry-leg cost from the single source — same model as paper & learning
            fee = txn_fees.open_leg(_fee_segment(pos.signal_type), pos.entry_price, pos.position_size)
            unrealised = gross - fee

            result.append({
                "id":              pos.id,
                "symbol":          pos.symbol,
                "strategy":        pos.strategy,
                "direction":       pos.direction,
                "signal_type":     pos.signal_type,
                "hold_type":       pos.hold_type,
                "entry_price":     pos.entry_price,
                "ltp":             ltp,
                "stop_loss":       pos.stop_loss,
                "target_1":        pos.target_1,
                "target_2":        pos.target_2,
                "position_size":   pos.position_size,
                "capital_at_risk": pos.capital_at_risk,
                "unrealised_pnl":  round(unrealised, 2),
                "entry_time":      pos.entry_time.isoformat(),
                "options_meta":    options_meta,
                "monitor_symbol":  pos.monitor_symbol or pos.symbol,
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

    def force_close(self, symbol: str, reason: str = "MANUAL_CLOSE") -> bool:
        """
        Manually remove a stale open position.
        Use when the broker already closed the position but the bot missed the fill
        (e.g., crash during exit, manual broker close, option expiry).
        Returns True if a position was found and closed.
        """
        pos = self._open_positions.pop(symbol, None)
        if pos:
            nfo = (pos.options_meta or {}).get("nfo_symbol")
            ltp = store.get_ltp(nfo or symbol)
            exit_price = ltp or pos.entry_price
            pos.status      = "CLOSED"
            pos.exit_time   = datetime.now(tz=IST)
            pos.exit_reason = reason
            pos.exit_price  = exit_price
            if pos.direction == "LONG":
                pos.realised_pnl = (exit_price - pos.entry_price) * pos.position_size
            else:
                pos.realised_pnl = (pos.entry_price - exit_price) * pos.position_size
            self._closed_trades.append(pos)
            self._update_position_db(pos)
            logger.warning(f"[Portfolio] FORCE CLOSED {symbol} reason={reason} est_pnl={pos.realised_pnl:+.0f}")
            return True

        # Position not in memory — update DB directly (restored-but-not-in-memory edge case)
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.execute(
                "UPDATE trades SET status='CLOSED', exit_reason=?, exit_time=? "
                "WHERE symbol=? AND status='OPEN'",
                (reason, datetime.now(tz=IST).isoformat(), symbol),
            )
            if cur.rowcount > 0:
                logger.warning(f"[Portfolio] FORCE CLOSED from DB only: {symbol}")
                return True
        return False

    def has_open_position(self, symbol: str) -> bool:
        return symbol in self._open_positions

    def get_position(self, symbol: str) -> Optional[Position]:
        return self._open_positions.get(symbol)

    def update_stop_loss(self, symbol: str, new_sl: float) -> None:
        """Persist an updated stop loss (trailing or breakeven) into the Position and DB."""
        pos = self._open_positions.get(symbol)
        if pos:
            pos.stop_loss = round(new_sl, 2)
            self._update_position_db(pos)

    def update_position_size(self, symbol: str, new_size: int) -> None:
        """Persist reduced position size after a partial exit (Bug 5)."""
        pos = self._open_positions.get(symbol)
        if pos:
            pos.position_size = new_size
            self._update_position_db(pos)

    def update_sl_order_id(self, symbol: str, order_id: str) -> None:
        """Record the current active SL broker order ID (Bug 2)."""
        pos = self._open_positions.get(symbol)
        if pos:
            pos.sl_order_id = order_id
            self._update_position_db(pos)

    def mark_t1_hit(self, symbol: str) -> None:
        """Persist T1-hit flag so reconstruction after restart knows partial exit already fired."""
        pos = self._open_positions.get(symbol)
        if pos:
            pos.t1_hit = True
            self._update_position_db(pos)

    def set_pending_close(self, symbol: str) -> None:
        """
        Mark position as PENDING_CLOSE before placing the exit order (Bug 1).
        If the bot crashes after the broker order is placed but before close_position()
        writes CLOSED, startup reconciliation detects PENDING_CLOSE and resolves it.
        """
        pos = self._open_positions.get(symbol)
        if pos:
            pos.status = "PENDING_CLOSE"
            self._update_position_db(pos)

    # ─────────────────────────────────────────────────────────────
    # INTERNAL — SQLite persistence
    # ─────────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        # Trades live in the unified ledger (execution/ledger.py, segment "live"), surfaced via
        # the `trades` compatibility VIEW so every reader (dashboard, analysis, scripts) is
        # unchanged. ledger.init() creates the ledger table + view, and leaves a not-yet-migrated
        # `trades` TABLE untouched until scripts/migrate_unified_ledger.py runs (deploy step).
        # Writes go through ledger.record() (see _save_position). param_changes stays a plain table.
        from execution import ledger
        ledger.init()
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS param_changes (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts        TEXT NOT NULL,
                    strategy  TEXT NOT NULL,
                    param     TEXT NOT NULL,
                    old_value TEXT,
                    new_value TEXT,
                    reason    TEXT DEFAULT 'manual'
                )
            """)
        logger.info(f"[Portfolio] Database initialised at {DB_PATH}")

    def _save_position(self, pos: Position) -> None:
        """Upsert the trade into the unified ledger (segment "live"). The `trades` compat VIEW
        projects the payload back to the original columns, so all readers are unchanged."""
        import json
        from execution import ledger
        ledger.record("live", {
            "id":                 pos.id,
            "symbol":             pos.symbol,
            "strategy":           pos.strategy,
            "direction":          pos.direction,
            "signal_type":        pos.signal_type,
            "entry_price":        pos.entry_price,
            "exit_price":         pos.exit_price,
            "stop_loss":          pos.stop_loss,
            "target_1":           pos.target_1,
            "position_size":      pos.position_size,
            "capital_at_risk":    pos.capital_at_risk,
            "realised_pnl":       pos.realised_pnl,
            "status":             pos.status,
            "exit_reason":        pos.exit_reason,
            "entry_time":         pos.entry_time.isoformat() if pos.entry_time else None,
            "exit_time":          pos.exit_time.isoformat()  if pos.exit_time  else None,
            "target_2":           pos.target_2,
            "hold_type":          pos.hold_type,
            "original_stop_loss": pos.original_stop_loss,
            "sl_order_id":        pos.sl_order_id or "",
            "options_meta":       json.dumps(pos.options_meta) if pos.options_meta else "{}",
            "t1_hit":             1 if pos.t1_hit else 0,
            "monitor_symbol":     pos.monitor_symbol or "",
        })

    def _update_position_db(self, pos: Position) -> None:
        self._save_position(pos)

    def _load_open_positions(self) -> None:
        """Reload OPEN and PENDING_CLOSE positions from DB on restart (Bug 1, 6, 18)."""
        import json
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM trades WHERE status IN ('OPEN', 'PENDING_CLOSE')"
                ).fetchall()

            for row in rows:
                try:
                    options_meta = json.loads(row["options_meta"] or "{}") if "options_meta" in row.keys() else {}
                except Exception:
                    options_meta = {}

                pos = Position(
                    id                 = row["id"],
                    symbol             = row["symbol"],
                    strategy           = row["strategy"],
                    direction          = row["direction"],
                    signal_type        = row["signal_type"] if "signal_type" in row.keys() else "EQUITY",
                    entry_price        = row["entry_price"],
                    stop_loss          = row["stop_loss"],
                    target_1           = row["target_1"],
                    target_2           = float(row["target_2"]) if row["target_2"] else 0.0,
                    position_size      = row["position_size"],
                    capital_at_risk    = row["capital_at_risk"],
                    entry_time         = datetime.fromisoformat(row["entry_time"]),
                    hold_type          = row["hold_type"] if "hold_type" in row.keys() and row["hold_type"] else "intraday",
                    status             = row["status"],
                    options_meta       = options_meta,
                    original_stop_loss = float(row["original_stop_loss"]) if "original_stop_loss" in row.keys() and row["original_stop_loss"] else row["stop_loss"],
                    sl_order_id        = row["sl_order_id"] if "sl_order_id" in row.keys() and row["sl_order_id"] else "",
                    t1_hit             = bool(row["t1_hit"]) if "t1_hit" in row.keys() and row["t1_hit"] else False,
                    monitor_symbol     = row["monitor_symbol"] if "monitor_symbol" in row.keys() and row["monitor_symbol"] else row["symbol"],
                )
                self._open_positions[pos.symbol] = pos

                if pos.status == "PENDING_CLOSE":
                    logger.warning(
                        f"[Portfolio] PENDING_CLOSE detected for {pos.symbol} — "
                        f"crash occurred during exit. Reconcile with broker."
                    )
                else:
                    logger.info(f"[Portfolio] Restored open position: {pos.symbol}")

        except Exception as e:
            logger.warning(f"[Portfolio] Could not restore positions: {e}")

        # Load today's closed trades for accurate win-rate / P&L stats (Bug 18)
        try:
            today_str = datetime.now(tz=IST).strftime("%Y-%m-%d")
            with sqlite3.connect(DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                closed_rows = conn.execute(
                    "SELECT * FROM trades WHERE status = 'CLOSED' AND entry_time >= ?",
                    (today_str,),
                ).fetchall()
            for row in closed_rows:
                try:
                    options_meta = json.loads(row["options_meta"] or "{}") if "options_meta" in row.keys() else {}
                except Exception:
                    options_meta = {}
                pos = Position(
                    id              = row["id"],
                    symbol          = row["symbol"],
                    strategy        = row["strategy"],
                    direction       = row["direction"],
                    signal_type     = row["signal_type"] if "signal_type" in row.keys() else "EQUITY",
                    entry_price     = row["entry_price"],
                    stop_loss       = row["stop_loss"],
                    target_1        = row["target_1"],
                    target_2        = float(row["target_2"]) if row["target_2"] else 0.0,
                    position_size   = row["position_size"],
                    capital_at_risk = row["capital_at_risk"],
                    entry_time      = datetime.fromisoformat(row["entry_time"]),
                    hold_type       = row["hold_type"] if "hold_type" in row.keys() and row["hold_type"] else "intraday",
                    exit_price      = float(row["exit_price"]) if row["exit_price"] else 0.0,
                    exit_time       = datetime.fromisoformat(row["exit_time"]) if row["exit_time"] else None,
                    realised_pnl    = float(row["realised_pnl"]) if row["realised_pnl"] else 0.0,
                    status          = "CLOSED",
                    exit_reason     = row["exit_reason"] or "",
                    options_meta    = options_meta,
                )
                self._closed_trades.append(pos)
            if closed_rows:
                logger.info(f"[Portfolio] Restored {len(closed_rows)} today's closed trades")
        except Exception as e:
            logger.warning(f"[Portfolio] Could not restore closed trades: {e}")


# ── Module-level singleton ────────────────────────────────────────
portfolio_tracker = PortfolioTracker()
