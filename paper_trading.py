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

Capital tracking:
    Starts with PAPER_STARTING_CAPITAL (₹5,00,000).
    Each paper trade deducts capital on entry and credits it back on exit.
    When balance drops below MIN_PAPER_BALANCE, no new paper trades are opened
    (learning trades continue independently — they don't use this wallet).
"""

import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

from config.settings import DB_PATH, RISK_PER_TRADE_PCT, TOTAL_CAPITAL
from data.data_store import store
from strategies.base_strategy import Direction, Signal, SignalType

logger = logging.getLogger(__name__)

PAPER_TRADING = os.getenv("PAPER_TRADING", "false").lower() == "true"

PAPER_STARTING_CAPITAL = 500_000.0   # ₹5L paper wallet
MIN_PAPER_BALANCE      = 25_000.0    # stop new paper trades below this floor
MAX_LEARNING_LOTS      = int(os.getenv("MAX_LEARNING_LOTS", "10"))  # cap lots per learning option mirror

# Realistic simulation parameters
SLIPPAGE_PCT  = 0.05    # 0.05% slippage on fills
# Transaction costs come from the SINGLE source: execution.fees (facade over
# analysis/cost_model.py + config/cost_rates.json). No inline fee math here —
# so paper, learning and live can never diverge on costs.
from execution import fees


def _fee_segment(instrument_type: str) -> str:
    return "OPTIONS" if instrument_type == "nse_options" else "EQUITY"


class PaperTradingEngine:
    """
    Simulates order execution for paper trading.

    When PAPER_TRADING=true:
    - place_order() → simulates fill at current LTP + slippage
    - Deducts capital from a ₹5L paper wallet on entry
    - Credits capital + P&L back on exit
    - When wallet < MIN_PAPER_BALANCE, refuses new trades (learning trades unaffected)
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
        # Always initialise DB — learning mirrors need the tables even when
        # full paper trading mode is off (PAPER_TRADING=false).
        self._init_db()
        if self._active:
            logger.info("[PaperTrading] PAPER TRADING MODE ACTIVE — no real orders will be placed")
        logger.info(f"[PaperTrading] Paper wallet balance: ₹{self.get_balance():,.0f}")

    def is_active(self) -> bool:
        return self._active

    # ─────────────────────────────────────────────────────────────
    # WALLET — capital tracking
    # ─────────────────────────────────────────────────────────────

    def get_balance(self) -> float:
        """Current paper wallet balance."""
        try:
            with sqlite3.connect(DB_PATH) as conn:
                row = conn.execute(
                    "SELECT balance FROM paper_wallet WHERE id=1"
                ).fetchone()
                return float(row[0]) if row else PAPER_STARTING_CAPITAL
        except Exception:
            return PAPER_STARTING_CAPITAL

    def can_trade(self, required_capital: float) -> bool:
        """True when wallet has enough for the trade and is above the safety floor."""
        balance = self.get_balance()
        return balance >= required_capital and balance >= MIN_PAPER_BALANCE

    def is_capital_exhausted(self) -> bool:
        """True when wallet is at or below the safety floor."""
        return self.get_balance() < MIN_PAPER_BALANCE

    def _deduct(self, amount: float) -> None:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "UPDATE paper_wallet SET balance = balance - ?, updated_at = ? WHERE id = 1",
                (amount, datetime.now(tz=IST).isoformat()),
            )

    def _credit(self, amount: float) -> None:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "UPDATE paper_wallet SET balance = balance + ?, updated_at = ? WHERE id = 1",
                (amount, datetime.now(tz=IST).isoformat()),
            )

    def _calc_capital(self, signal: Signal, ltp: float) -> float:
        """
        Capital required for this trade (deducted from wallet on entry).
        Options  — full premium upfront: entry × position_size (units = lots × lot_size)
        Equity   — 25% intraday margin: fill_price × size × 0.25
        """
        if signal.signal_type == SignalType.OPTIONS:
            return signal.entry * signal.position_size
        return ltp * signal.position_size * 0.25

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
        Returns a fake order ID, or None if capital is exhausted.
        """
        ltp = store.get_ltp(signal.symbol)
        if not ltp:
            ltp = signal.entry   # fallback to signal entry

        capital_deployed = self._calc_capital(signal, ltp)

        if not self.can_trade(capital_deployed):
            logger.warning(
                f"[PaperTrading] SKIP {signal.symbol}: paper capital exhausted "
                f"(balance ₹{self.get_balance():,.0f}, required ₹{capital_deployed:,.0f})"
            )
            return None

        # Apply slippage
        if signal.direction == Direction.LONG:
            fill_price = round(ltp * (1 + SLIPPAGE_PCT / 100), 2)
        else:
            fill_price = round(ltp * (1 - SLIPPAGE_PCT / 100), 2)

        order_id = f"PAPER-{uuid.uuid4().hex[:8].upper()}"

        # Deduct capital from wallet
        self._deduct(capital_deployed)

        logger.info(
            f"[PaperTrading] FILL {signal.direction.value} "
            f"{signal.symbol} × {signal.position_size} "
            f"@ ₹{fill_price:.2f} (LTP: ₹{ltp:.2f}) | "
            f"Capital: ₹{capital_deployed:,.0f} | "
            f"Wallet: ₹{self.get_balance():,.0f} | {order_id}"
        )

        # Save to paper trades DB
        self._record_entry(signal, fill_price, order_id, capital_deployed)

        # Send paper trade alert
        self._send_alert(
            f"📝 *[PAPER] TRADE OPENED*\n"
            f"Symbol:   `{signal.symbol}`\n"
            f"Direction:`{signal.direction.value}`\n"
            f"Fill:     `₹{fill_price:.2f}`\n"
            f"Size:     `{signal.position_size} units`\n"
            f"Stop:     `₹{signal.stop_loss:.2f}`\n"
            f"Target:   `₹{signal.target_1:.2f}`\n"
            f"Strategy: `{signal.strategy}`\n"
            f"R:R:      `{signal.risk_reward:.1f}`\n"
            f"Capital:  `₹{capital_deployed:,.0f}`\n"
            f"Wallet:   `₹{self.get_balance():,.0f}`\n"
            f"Order ID: `{order_id}`"
        )

        return order_id

    def close_order(
        self,
        symbol:     str,
        qty:        int,
        direction:  str,
        reason:     str = "manual",
        exit_price: Optional[float] = None,
    ) -> Optional[str]:
        """Simulate closing a paper position.

        exit_price: pass the instrument's OWN traded price (e.g. the option premium
        via its NFO symbol) when `symbol` is not the tradable (index options store the
        index as `symbol`). When omitted, falls back to store.get_ltp(symbol)."""
        ltp = exit_price if (exit_price and exit_price > 0) else store.get_ltp(symbol)
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
            f"P&L: ₹{pnl:+,.0f} | {reason} | {order_id} | "
            f"Wallet: ₹{self.get_balance():,.0f}"
        )

        self._send_alert(
            f"📝 *[PAPER] TRADE CLOSED*\n"
            f"Symbol: `{symbol}`\n"
            f"Exit:   `₹{fill_price:.2f}`\n"
            f"P&L:    `₹{pnl:+,.0f}`\n"
            f"Wallet: `₹{self.get_balance():,.0f}`\n"
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
                symbol       = row["symbol"]
                nfo_sym      = row["nfo_symbol"] if row["nfo_symbol"] else None
                instr_type   = row["instrument_type"] if row["instrument_type"] else ""
                entry        = row["entry_price"]
                size         = row["position_size"]
                direction    = row["direction"]

                # Infer options trade from strategy name for old rows lacking instrument_type
                if not instr_type and any(s in (row["strategy"] or "") for s in ["Options", "Institutional"]):
                    instr_type = "nse_options"

                # Options: use nfo_symbol for LTP. If none stored (old stuck trade), show zero P&L
                if instr_type == "nse_options":
                    # Display price: live LTP, else last candle close (survives close/restart)
                    ltp = store.get_last_price(nfo_sym) if nfo_sym else None
                    if ltp is None:
                        # Stuck trade — no contract symbol, cannot get real LTP
                        unrealised = 0.0
                        ltp        = entry
                    else:
                        unrealised = (ltp - entry) * size
                else:
                    raw_ltp = store.get_last_price(symbol) or entry
                    # Safety: if LTP is 20× the entry, it's an index being used instead of
                    # an option premium (old stuck mirror trade) — show zero P&L
                    ltp        = raw_ltp if raw_ltp <= entry * 20 else entry
                    unrealised = (ltp - entry) * size if direction == "LONG" else (entry - ltp) * size

                # Deduct entry-leg cost only (exit not yet incurred on an open position)
                brokerage  = fees.open_leg(_fee_segment(instr_type), entry, size)
                net_pnl    = unrealised - brokerage

                positions.append({
                    "id":               row["id"],
                    "symbol":           symbol,
                    "nfo_symbol":       nfo_sym,
                    "instrument_type":  instr_type,
                    "strategy":         row["strategy"],
                    "direction":        direction,
                    "entry_price":      entry,
                    "ltp":              ltp,
                    "stop_loss":        row["stop_loss"],
                    "target_1":         row["target_1"],
                    "position_size":    size,
                    "capital_deployed": float(row["capital_deployed"] or 0),
                    "unrealised_pnl":   round(net_pnl, 2),
                    "entry_time":       row["entry_time"],
                    "paper":            True,
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

            balance     = self.get_balance()
            deployed    = sum(float(r["capital_deployed"] or 0) for r in open_pos)
            total_gain  = total_pnl + unrealised

            return {
                "mode":               "PAPER",
                "total_trades":       total_trades,
                "open_positions":     len(open_pos),
                "win_rate":           round(win_rate, 1),
                "profit_factor":      pf,
                "total_realised":     round(total_pnl, 2),
                "total_unrealised":   round(unrealised, 2),
                "total_pnl":          round(total_gain, 2),
                "total_pnl_pct":      round(total_gain / PAPER_STARTING_CAPITAL * 100, 2),
                "avg_winner":         round(sum(r["realised_pnl"] for r in winners) / len(winners), 2) if winners else 0,
                "avg_loser":          round(abs(sum(r["realised_pnl"] for r in losers) / len(losers)), 2) if losers else 0,
                # Wallet
                "starting_capital":   PAPER_STARTING_CAPITAL,
                "available_balance":  round(balance, 2),
                "capital_deployed":   round(deployed, 2),
                "balance_pct":        round(balance / PAPER_STARTING_CAPITAL * 100, 1),
                "is_exhausted":       self.is_capital_exhausted(),
            }

        except Exception as e:
            logger.error(f"[PaperTrading] get_stats failed: {e}")
            return {"mode": "PAPER", "error": str(e)}

    # ─────────────────────────────────────────────────────────────
    # DATABASE
    # ─────────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        """Create paper_trades and paper_wallet tables if not exist."""
        import os
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS paper_trades (
                    id               TEXT PRIMARY KEY,
                    symbol           TEXT,
                    nfo_symbol       TEXT DEFAULT '',
                    instrument_type  TEXT DEFAULT '',
                    strategy         TEXT,
                    direction        TEXT,
                    entry_price      REAL,
                    exit_price       REAL DEFAULT 0,
                    stop_loss        REAL,
                    target_1         REAL,
                    position_size    INTEGER,
                    capital_at_risk  REAL,
                    capital_deployed REAL DEFAULT 0,
                    realised_pnl     REAL DEFAULT 0,
                    status           TEXT DEFAULT 'OPEN',
                    exit_reason      TEXT DEFAULT '',
                    entry_time       TEXT,
                    exit_time        TEXT
                )
            """)
            # Safe migration for pre-existing tables
            for col, defval in [
                ("capital_deployed", "REAL DEFAULT 0"),
                ("nfo_symbol",       "TEXT DEFAULT ''"),
                ("instrument_type",  "TEXT DEFAULT ''"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE paper_trades ADD COLUMN {col} {defval}")
                except Exception:
                    pass

            conn.execute("""
                CREATE TABLE IF NOT EXISTS paper_wallet (
                    id         INTEGER PRIMARY KEY,
                    balance    REAL,
                    updated_at TEXT
                )
            """)
            # Seed wallet with starting capital if not yet initialised
            existing = conn.execute(
                "SELECT id FROM paper_wallet WHERE id = 1"
            ).fetchone()
            if not existing:
                conn.execute(
                    "INSERT INTO paper_wallet (id, balance, updated_at) VALUES (1, ?, ?)",
                    (PAPER_STARTING_CAPITAL, datetime.now(tz=IST).isoformat()),
                )
                logger.info(
                    f"[PaperTrading] Wallet initialised: ₹{PAPER_STARTING_CAPITAL:,.0f}"
                )

        logger.info("[PaperTrading] DB tables ready")

    def _record_entry(
        self,
        signal:           Signal,
        fill_price:       float,
        order_id:         str,
        capital_deployed: float,
    ) -> None:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                INSERT INTO paper_trades
                (id, symbol, strategy, direction, entry_price, stop_loss,
                 target_1, position_size, capital_at_risk, capital_deployed,
                 status, entry_time)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
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
                capital_deployed,
                "OPEN",
                datetime.now(tz=IST).isoformat(),
            ))

    def _record_exit(self, symbol: str, exit_price: float, reason: str) -> float:
        """Close paper position, calculate P&L, return capital to wallet. Returns realised P&L.

        Only closes LIVE paper trades — never learning mirrors (PAPER-LRN-*), which are
        closed exclusively by mirror_learning_close() with the option's own premium.
        For index options the position symbol is the INDEX (e.g. NSE:NIFTY50-INDEX), so
        without this exclusion a live index-options exit (priced at the index spot) would
        match the learning mirror by symbol and book a catastrophic phantom P&L.
        """
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM paper_trades WHERE symbol=? AND status='OPEN' "
                "AND id NOT LIKE 'PAPER-LRN-%' ORDER BY entry_time LIMIT 1",
                (symbol,)
            ).fetchone()

            if not row:
                return 0.0

            entry            = row["entry_price"]
            size             = row["position_size"]
            capital_deployed = float(row["capital_deployed"] or 0)
            instr_type       = row["instrument_type"] if row["instrument_type"] else ""

            if row["direction"] == "LONG":
                gross_pnl = (exit_price - entry) * size
            else:
                gross_pnl = (entry - exit_price) * size

            # Round-trip transaction cost from the single source (entry + exit)
            brokerage = fees.round_trip(_fee_segment(instr_type), entry, exit_price, size)
            pnl       = round(gross_pnl - brokerage, 2)

            conn.execute("""
                UPDATE paper_trades
                SET status='CLOSED', exit_price=?, realised_pnl=?,
                    exit_reason=?, exit_time=?
                WHERE id=?
            """, (
                exit_price, pnl, reason,
                datetime.now(tz=IST).isoformat(),
                row["id"],
            ))

        # Return deployed capital + net P&L back to wallet
        # e.g. deployed ₹10k, pnl +₹2k → credit ₹12k back
        #      deployed ₹10k, pnl -₹3k → credit ₹7k back (capital partially lost)
        self._credit(max(0.0, capital_deployed + pnl))
        return pnl

    # ─────────────────────────────────────────────────────────────
    # LEARNING MIRROR — every learning trade also becomes a paper trade
    # ─────────────────────────────────────────────────────────────

    def mirror_learning_open(self, trade: dict) -> Optional[int]:
        """
        Called by LearningEngine when it opens a trade.
        Mirrors it to paper_trades using the SHARED sizer (execution/sizing.py) with
        the LEARNING run-context's risk budget — the SAME rule live uses
        (TOTAL_CAPITAL × RISK_PER_TRADE_PCT), so a symbol sizes identically across
        live / paper / learning. No more 1%-of-wallet divergence; the ₹5L wallet is
        for P&L reporting only, never sizing.
        Returns the executed quantity (shares for equity, lots×lot_size for options)
        so the caller stores the real order size and derives P&L as
        (LTP − entry) × qty − fees. None if the trade can't be sized.
        """

        symbol          = trade.get("symbol", "")
        direction       = trade.get("direction", "LONG")
        entry           = float(trade.get("entry_price", 0))
        stop            = float(trade.get("stop_loss", 0))
        metadata        = trade.get("metadata") or {}
        instrument_type = metadata.get("instrument_type", "")
        nfo_symbol      = metadata.get("nfo_symbol") or ""

        if entry <= 0 or stop <= 0:
            return None

        # Single sizer, single risk budget — identical to the live pipeline.
        from execution.run_context import learning_context
        from execution.sizing import shares_to_fit, lots_to_fit
        risk_budget = learning_context().risk_budget   # TOTAL_CAPITAL × RISK_PER_TRADE_PCT/100

        if instrument_type == "nse_options":
            # Options: full premium upfront (no intraday margin on option buying).
            lot_size    = int(metadata.get("lot_size", 1)) or 1
            cost_per_lot = entry * lot_size
            # Long premium: debit IS the max loss, so risk budget == capital budget.
            lots        = lots_to_fit(cost_per_lot, risk_budget, risk_budget,
                                      MAX_LEARNING_LOTS)
            lots        = max(1, lots)   # learning records every signal (ignores fund cap)
            qty         = lots * lot_size
            capital_req = qty * entry
            cap_at_risk = capital_req            # long premium: debit IS the max loss
        else:
            qty, cap_at_risk, reason = shares_to_fit(entry, stop, risk_budget)
            if qty <= 0:
                logger.info(f"[PaperTrading] Mirror SKIP {symbol}: {reason or 'size 0'}")
                return None
            capital_req = qty * entry * 0.25   # 25% intraday margin for equity/futures

        # LEARNING ignores fund availability — every signal is recorded (no can_trade
        # gate). The ₹5L wallet is for P&L reporting only, not a trading constraint.
        order_id  = f"PAPER-LRN-{trade.get('id', 'UNKNOWN')[-8:]}"
        now_str   = datetime.now(tz=IST).isoformat()

        self._deduct(capital_req)

        logger.info(
            f"[PaperTrading] MIRROR {direction} {symbol} × {qty} "
            f"@ ₹{entry:.2f} | Cap ₹{capital_req:,.0f} | Wallet ₹{self.get_balance():,.0f} "
            f"| {order_id}"
        )

        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute("""
                    INSERT OR IGNORE INTO paper_trades
                    (id, symbol, nfo_symbol, instrument_type, strategy, direction,
                     entry_price, stop_loss, target_1, position_size,
                     capital_at_risk, capital_deployed, status, entry_time)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    order_id,
                    symbol,
                    nfo_symbol,
                    instrument_type,
                    trade.get("strategy", "Learning"),
                    direction,
                    entry,
                    stop,
                    float(trade.get("target", entry)),
                    qty,
                    round(cap_at_risk, 2),
                    round(capital_req, 2),
                    "OPEN",
                    now_str,
                ))
        except Exception as e:
            logger.warning(f"[PaperTrading] mirror_learning_open DB error: {e}")
            self._credit(capital_req)   # undo deduction on failure
            return None

        return qty

    def mirror_learning_close(
        self,
        learning_id:  str,
        exit_price:   float,
        reason:       str,
    ) -> float:
        """
        Called by LearningEngine when it closes a trade.
        Looks up the mirrored paper trade by learning_id suffix and closes it.
        Always active regardless of PAPER_TRADING env var.
        Returns realised P&L (0 if no mirrored trade found).
        """

        paper_id = f"PAPER-LRN-{learning_id[-8:]}"

        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT * FROM paper_trades WHERE id=? AND status='OPEN'",
                    (paper_id,)
                ).fetchone()

                if not row:
                    return 0.0

                entry            = float(row["entry_price"])
                size             = int(row["position_size"])
                capital_deployed = float(row["capital_deployed"] or 0)
                direction        = row["direction"]
                instr_type       = row["instrument_type"] if row["instrument_type"] else ""

                # Options debit spreads: always long the premium regardless of market direction
                if instr_type == "nse_options" or direction == "LONG":
                    gross_pnl = (exit_price - entry) * size
                else:
                    gross_pnl = (entry - exit_price) * size

                brokerage = fees.round_trip(_fee_segment(instr_type), entry, exit_price, size)
                pnl       = round(gross_pnl - brokerage, 2)

                conn.execute("""
                    UPDATE paper_trades
                    SET status='CLOSED', exit_price=?, realised_pnl=?,
                        exit_reason=?, exit_time=?
                    WHERE id=?
                """, (
                    exit_price, pnl, reason,
                    datetime.now(tz=IST).isoformat(),
                    paper_id,
                ))

            self._credit(max(0.0, capital_deployed + pnl))
            logger.info(
                f"[PaperTrading] MIRROR CLOSE {paper_id} @ ₹{exit_price:.2f} "
                f"| P&L ₹{pnl:+,.0f} | {reason} | Wallet ₹{self.get_balance():,.0f}"
            )
            return pnl

        except Exception as e:
            logger.warning(f"[PaperTrading] mirror_learning_close error: {e}")
            return 0.0

    def _send_alert(self, message: str) -> None:
        try:
            from notifications.alert_service import alert_service
            alert_service._send(message)
        except Exception:
            pass


# ── Module-level singleton ────────────────────────────────────────
paper_trading_engine = PaperTradingEngine()
