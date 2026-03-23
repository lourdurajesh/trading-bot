"""
paper_trading.py
────────────────
Paper trading engine — fully simulates order execution.
Real signals, real intelligence, real risk management.
Fake order fills saved to DB — no real broker API calls.

Enables full AUTO mode testing without risking real money.
Results tell you exactly if the bot is profitable before going live.

Enable by setting in .env:
    BOT_MODE=AUTO
    PAPER_TRADING=true

All paper trades are tagged with [PAPER] in alerts and dashboard.
P&L is tracked separately from live trading.
"""

import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Optional

from config.settings import DB_PATH, RISK_PER_TRADE_PCT, TOTAL_CAPITAL
from data.data_store import store
from strategies.base_strategy import Direction, Signal

logger = logging.getLogger(__name__)

PAPER_TRADING = os.getenv("PAPER_TRADING", "false").lower() == "true"

# Realistic simulation parameters
SLIPPAGE_PCT  = 0.05    # 0.05% slippage on fills
BROKERAGE_PCT = 0.03    # 0.03% per leg brokerage


class PaperTradingEngine:
    """
    Simulates order execution for paper trading.

    When PAPER_TRADING=true:
    - place_order() → simulates fill at current LTP + slippage
    - Records to paper_trades table in SQLite
    - Tracks paper P&L separately from live
    - Sends [PAPER] tagged alerts

    Usage:
        if paper_trading_engine.is_active():
            order_id = paper_trading_engine.place_order(signal)
        else:
            order_id = fyers_broker.place_order(...)
    """

    def __init__(self):
        self._active = PAPER_TRADING
        if self._active:
            logger.info("[PaperTrading] PAPER TRADING MODE ACTIVE — no real orders will be placed")
            self._init_db()

    def is_active(self) -> bool:
        return self._active

    # ─────────────────────────────────────────────────────────────
    # PAPER ORDER EXECUTION
    # ─────────────────────────────────────────────────────────────

    def place_order(
        self,
        signal:     Signal,
        order_type: str = "MARKET",
    ) -> Optional[str]:
        """
        Simulate order placement.
        Fill price = current LTP ± slippage.
        Returns a fake order ID.
        """
        ltp = store.get_ltp(signal.symbol)
        if not ltp:
            ltp = signal.entry   # fallback to signal entry

        # Apply slippage
        if signal.direction == Direction.LONG:
            fill_price = round(ltp * (1 + SLIPPAGE_PCT / 100), 2)
        else:
            fill_price = round(ltp * (1 - SLIPPAGE_PCT / 100), 2)

        order_id = f"PAPER-{uuid.uuid4().hex[:8].upper()}"

        logger.info(
            f"[PaperTrading] FILL {signal.direction.value} "
            f"{signal.symbol} × {signal.position_size} "
            f"@ ₹{fill_price:.2f} (LTP: ₹{ltp:.2f}) | {order_id}"
        )

        # Save to paper trades DB
        self._record_entry(signal, fill_price, order_id)

        # Send paper trade alert
        self._send_alert(
            f"📝 *[PAPER] TRADE OPENED*\n"
            f"Symbol:   `{signal.symbol}`\n"
            f"Direction:`{signal.direction.value}`\n"
            f"Fill:     `₹{fill_price:.2f}`\n"
            f"Size:     `{signal.position_size} shares`\n"
            f"Stop:     `₹{signal.stop_loss:.2f}`\n"
            f"Target:   `₹{signal.target_1:.2f}`\n"
            f"Strategy: `{signal.strategy}`\n"
            f"R:R:      `{signal.risk_reward:.1f}`\n"
            f"Order ID: `{order_id}`"
        )

        return order_id

    def close_order(
        self,
        symbol:    str,
        qty:       int,
        direction: str,
        reason:    str = "manual",
    ) -> Optional[str]:
        """Simulate closing a paper position."""
        ltp = store.get_ltp(symbol)
        if not ltp:
            return None

        # Apply slippage on exit
        if direction == "LONG":
            fill_price = round(ltp * (1 - SLIPPAGE_PCT / 100), 2)
        else:
            fill_price = round(ltp * (1 + SLIPPAGE_PCT / 100), 2)

        order_id = f"PAPER-{uuid.uuid4().hex[:8].upper()}"
        pnl      = self._record_exit(symbol, fill_price, reason)

        logger.info(
            f"[PaperTrading] EXIT {symbol} @ ₹{fill_price:.2f} | "
            f"P&L: ₹{pnl:+,.0f} | {reason} | {order_id}"
        )

        self._send_alert(
            f"📝 *[PAPER] TRADE CLOSED*\n"
            f"Symbol: `{symbol}`\n"
            f"Exit:   `₹{fill_price:.2f}`\n"
            f"P&L:    `₹{pnl:+,.0f}`\n"
            f"Reason: `{reason}`"
        )

        return order_id

    # ─────────────────────────────────────────────────────────────
    # PAPER P&L TRACKING
    # ─────────────────────────────────────────────────────────────

    def get_paper_positions(self) -> list[dict]:
        """Get all open paper positions with live P&L."""
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM paper_trades WHERE status = 'OPEN'"
                ).fetchall()

            positions = []
            for row in rows:
                symbol    = row["symbol"]
                ltp       = store.get_ltp(symbol) or row["entry_price"]
                entry     = row["entry_price"]
                size      = row["position_size"]
                direction = row["direction"]

                if direction == "LONG":
                    unrealised = (ltp - entry) * size
                else:
                    unrealised = (entry - ltp) * size

                # Deduct brokerage
                brokerage  = entry * size * BROKERAGE_PCT / 100
                net_pnl    = unrealised - brokerage

                positions.append({
                    "id":             row["id"],
                    "symbol":         symbol,
                    "strategy":       row["strategy"],
                    "direction":      direction,
                    "entry_price":    entry,
                    "ltp":            ltp,
                    "stop_loss":      row["stop_loss"],
                    "target_1":       row["target_1"],
                    "position_size":  size,
                    "unrealised_pnl": round(net_pnl, 2),
                    "entry_time":     row["entry_time"],
                    "paper":          True,
                })
            return positions

        except Exception as e:
            logger.error(f"[PaperTrading] get_positions failed: {e}")
            return []

    def get_paper_stats(self) -> dict:
        """Get paper trading performance statistics."""
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                closed = conn.execute(
                    "SELECT * FROM paper_trades WHERE status = 'CLOSED'"
                ).fetchall()
                open_pos = conn.execute(
                    "SELECT * FROM paper_trades WHERE status = 'OPEN'"
                ).fetchall()

            total_trades = len(closed)
            winners      = [r for r in closed if r["realised_pnl"] > 0]
            losers       = [r for r in closed if r["realised_pnl"] <= 0]
            total_pnl    = sum(r["realised_pnl"] for r in closed)
            win_rate     = len(winners) / total_trades * 100 if total_trades else 0

            gross_profit = sum(r["realised_pnl"] for r in winners)
            gross_loss   = abs(sum(r["realised_pnl"] for r in losers))
            pf           = round(gross_profit / gross_loss, 2) if gross_loss > 0 else 0

            # Unrealised P&L on open positions
            unrealised = sum(
                p["unrealised_pnl"]
                for p in self.get_paper_positions()
            )

            return {
                "mode":            "PAPER",
                "total_trades":    total_trades,
                "open_positions":  len(open_pos),
                "win_rate":        round(win_rate, 1),
                "profit_factor":   pf,
                "total_realised":  round(total_pnl, 2),
                "total_unrealised": round(unrealised, 2),
                "total_pnl":       round(total_pnl + unrealised, 2),
                "total_pnl_pct":   round((total_pnl + unrealised) / TOTAL_CAPITAL * 100, 2),
                "avg_winner":      round(sum(r["realised_pnl"] for r in winners) / len(winners), 2) if winners else 0,
                "avg_loser":       round(abs(sum(r["realised_pnl"] for r in losers) / len(losers)), 2) if losers else 0,
            }

        except Exception as e:
            logger.error(f"[PaperTrading] get_stats failed: {e}")
            return {"mode": "PAPER", "error": str(e)}

    # ─────────────────────────────────────────────────────────────
    # DATABASE
    # ─────────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        """Create paper_trades table if not exists."""
        import os
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS paper_trades (
                    id              TEXT PRIMARY KEY,
                    symbol          TEXT,
                    strategy        TEXT,
                    direction       TEXT,
                    entry_price     REAL,
                    exit_price      REAL DEFAULT 0,
                    stop_loss       REAL,
                    target_1        REAL,
                    position_size   INTEGER,
                    capital_at_risk REAL,
                    realised_pnl    REAL DEFAULT 0,
                    status          TEXT DEFAULT 'OPEN',
                    exit_reason     TEXT DEFAULT '',
                    entry_time      TEXT,
                    exit_time       TEXT
                )
            """)
        logger.info("[PaperTrading] DB table ready")

    def _record_entry(self, signal: Signal, fill_price: float, order_id: str) -> None:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                INSERT INTO paper_trades
                (id, symbol, strategy, direction, entry_price, stop_loss,
                 target_1, position_size, capital_at_risk, status, entry_time)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (
                order_id,
                signal.symbol,
                signal.strategy,
                signal.direction.value,
                fill_price,
                signal.stop_loss,
                signal.target_1,
                signal.position_size,
                signal.capital_at_risk,
                "OPEN",
                datetime.now(tz=timezone.utc).isoformat(),
            ))

    def _record_exit(self, symbol: str, exit_price: float, reason: str) -> float:
        """Close paper position and calculate P&L. Returns realised P&L."""
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM paper_trades WHERE symbol=? AND status='OPEN'",
                (symbol,)
            ).fetchone()

            if not row:
                return 0.0

            entry = row["entry_price"]
            size  = row["position_size"]

            if row["direction"] == "LONG":
                gross_pnl = (exit_price - entry) * size
            else:
                gross_pnl = (entry - exit_price) * size

            # Deduct brokerage both legs
            brokerage = (entry + exit_price) * size * BROKERAGE_PCT / 100
            pnl       = round(gross_pnl - brokerage, 2)

            conn.execute("""
                UPDATE paper_trades
                SET status='CLOSED', exit_price=?, realised_pnl=?,
                    exit_reason=?, exit_time=?
                WHERE symbol=? AND status='OPEN'
            """, (
                exit_price, pnl, reason,
                datetime.now(tz=timezone.utc).isoformat(),
                symbol,
            ))
            return pnl

    def _send_alert(self, message: str) -> None:
        try:
            from notifications.alert_service import alert_service
            alert_service._send(message)
        except Exception:
            pass


# ── Module-level singleton ────────────────────────────────────────
paper_trading_engine = PaperTradingEngine()