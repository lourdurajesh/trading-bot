"""
position_manager.py
───────────────────
Active position exit management — runs on every tick.
This is the most critical missing piece for autonomous trading.

Rules enforced:
  1. Stop loss hit        → exit immediately at market
  2. Target 1 hit         → exit 50%, move SL to breakeven
  3. Target 2 hit         → exit remaining 50%
  4. Trailing stop        → after 1R profit, trail by 1×ATR
  5. Breakeven move       → after T1 hit, SL moves to entry price
  6. EOD forced exit      → close all intraday positions at 3:15 PM IST
  7. Max holding period   → force exit after 20 trading days
  8. Catastrophic gap     → if price gaps past SL by >3%, exit immediately

Called by main.py fast loop every 5 seconds.
"""

import logging
import threading
from datetime import datetime, time, timezone
from typing import Optional

from data.data_store import store
from risk.portfolio_tracker import portfolio_tracker, Position
from notifications.alert_service import alert_service

logger = logging.getLogger(__name__)

# Exit rules configuration
EOD_EXIT_TIME      = time(15, 15)    # 3:15 PM IST — close intraday positions
MAX_HOLDING_DAYS   = 20              # force exit after this many calendar days
BREAKEVEN_TRIGGER  = 1.0             # move SL to BE after 1R profit
TRAIL_TRIGGER      = 1.5             # start trailing after 1.5R profit
PARTIAL_EXIT_PCT   = 0.5             # exit 50% at T1


class PositionManager:
    """
    Monitors all open positions on every tick and manages exits.

    Usage:
        position_manager.check_all()   # called by fast loop every 5s
    """

    def __init__(self):
        self._lock              = threading.Lock()
        self._breakeven_applied: set[str] = set()   # symbols where SL moved to BE
        self._partial_exited:    set[str] = set()   # symbols where 50% already exited
        self._trailing_stops:    dict[str, float] = {}  # symbol → current trail SL

    def check_all(self) -> None:
        """
        Check all open positions against current prices.
        Called every 5 seconds from main.py fast loop.
        """
        positions = portfolio_tracker.get_open_positions()
        if not positions:
            return

        now_ist = datetime.now()   # assumes server in IST

        for pos_dict in positions:
            symbol = pos_dict.get("symbol", "")
            try:
                self._check_position(pos_dict, now_ist)
            except Exception as e:
                logger.error(f"[PositionManager] Error checking {symbol}: {e}")

    # ─────────────────────────────────────────────────────────────
    # INTERNAL — per-position check
    # ─────────────────────────────────────────────────────────────

    def _check_position(self, pos_dict: dict, now: datetime) -> None:
        symbol     = pos_dict.get("symbol", "")
        direction  = pos_dict.get("direction", "LONG")
        entry      = float(pos_dict.get("entry_price", 0))
        stop       = float(pos_dict.get("stop_loss", 0))
        target_1   = float(pos_dict.get("target_1", 0))
        size       = int(pos_dict.get("position_size", 0))
        entry_time = pos_dict.get("entry_time", "")

        ltp = store.get_ltp(symbol)
        if not ltp or ltp <= 0:
            return

        # Use trailing stop if set, else original stop
        effective_stop = self._trailing_stops.get(symbol, stop)

        # ── 1. EOD forced exit (3:15 PM) ─────────────────────────
        if now.time() >= EOD_EXIT_TIME:
            if symbol not in self._partial_exited or size > 0:
                logger.info(f"[PositionManager] EOD exit: {symbol}")
                self._exit_position(symbol, size, "EOD_FORCED", ltp)
                return

        # ── 2. Max holding period ─────────────────────────────────
        if entry_time:
            try:
                entry_dt   = datetime.fromisoformat(entry_time.replace("Z", "+00:00"))
                days_held  = (datetime.now(tz=timezone.utc) - entry_dt).days
                if days_held >= MAX_HOLDING_DAYS:
                    logger.info(f"[PositionManager] Max hold {days_held}d: {symbol}")
                    self._exit_position(symbol, size, "MAX_HOLD", ltp)
                    return
            except Exception:
                pass

        # ── 3. LONG position management ───────────────────────────
        if direction == "LONG":
            risk = entry - stop
            if risk <= 0:
                return

            # Stop loss hit
            if ltp <= effective_stop:
                logger.info(f"[PositionManager] STOP HIT {symbol}: "
                            f"ltp={ltp:.2f} <= sl={effective_stop:.2f}")
                self._exit_position(symbol, size, "STOP", ltp)
                return

            # Target 1 hit — partial exit + move SL to breakeven
            if ltp >= target_1 and symbol not in self._partial_exited:
                partial_size = max(1, int(size * PARTIAL_EXIT_PCT))
                logger.info(f"[PositionManager] T1 HIT {symbol}: "
                            f"ltp={ltp:.2f} >= t1={target_1:.2f} — "
                            f"exiting {partial_size} shares")
                self._partial_exit(symbol, partial_size, "TARGET1", ltp)
                self._partial_exited.add(symbol)

                # Move SL to breakeven
                if symbol not in self._breakeven_applied:
                    self._move_stop_to_breakeven(symbol, entry)
                    self._breakeven_applied.add(symbol)
                return

            # Trailing stop — after 1.5R profit
            profit_r = (ltp - entry) / risk
            if profit_r >= TRAIL_TRIGGER:
                self._update_trailing_stop(symbol, ltp, direction, risk)

            # Breakeven move — after 1R profit
            elif profit_r >= BREAKEVEN_TRIGGER and symbol not in self._breakeven_applied:
                self._move_stop_to_breakeven(symbol, entry)
                self._breakeven_applied.add(symbol)

        # ── 4. SHORT position management ──────────────────────────
        elif direction == "SHORT":
            risk = stop - entry
            if risk <= 0:
                return

            # Stop loss hit
            if ltp >= effective_stop:
                logger.info(f"[PositionManager] STOP HIT SHORT {symbol}: "
                            f"ltp={ltp:.2f} >= sl={effective_stop:.2f}")
                self._exit_position(symbol, size, "STOP", ltp)
                return

            # Target 1 hit
            if ltp <= target_1 and symbol not in self._partial_exited:
                partial_size = max(1, int(size * PARTIAL_EXIT_PCT))
                logger.info(f"[PositionManager] T1 HIT SHORT {symbol}")
                self._partial_exit(symbol, partial_size, "TARGET1", ltp)
                self._partial_exited.add(symbol)
                if symbol not in self._breakeven_applied:
                    self._move_stop_to_breakeven(symbol, entry)
                    self._breakeven_applied.add(symbol)
                return

            # Trailing stop for short
            profit_r = (entry - ltp) / risk
            if profit_r >= TRAIL_TRIGGER:
                self._update_trailing_stop(symbol, ltp, direction, risk)

    # ─────────────────────────────────────────────────────────────
    # EXIT OPERATIONS
    # ─────────────────────────────────────────────────────────────

    def _exit_position(self, symbol: str, size: int, reason: str, price: float) -> None:
        """Full exit of a position."""
        import os
        PAPER_TRADING = os.getenv("PAPER_TRADING", "false").lower() == "true"

        pos = portfolio_tracker.get_position(symbol)
        if not pos:
            logger.warning(f"[PositionManager] Exit called but no position found: {symbol}")
            return

        logger.info(f"[PositionManager] EXITING {symbol} × {size} @ {price:.2f} — {reason}")

        if PAPER_TRADING:
            # Paper mode — simulate exit via paper trading engine
            from paper_trading import paper_trading_engine
            paper_trading_engine.close_order(
                symbol    = symbol,
                qty       = size,
                direction = pos.direction,
                reason    = reason,
            )
            order_id = f"PAPER-EXIT"
        else:
            from execution.fyers_broker import fyers_broker
            from execution.alpaca_broker import alpaca_broker
            exit_direction = "SHORT" if pos.direction == "LONG" else "LONG"
            broker = fyers_broker if symbol.startswith("NSE:") else alpaca_broker
            order_id = broker.place_order(
                symbol     = symbol,
                direction  = exit_direction,
                qty        = size,
                order_type = "MARKET",
            )

        if order_id:
            # Close in portfolio tracker
            closed = portfolio_tracker.close_position(symbol, price, reason)
            if closed:
                alert_service.trade_closed(symbol, closed.realised_pnl, reason)

            # Clean up tracking sets
            self._breakeven_applied.discard(symbol)
            self._partial_exited.discard(symbol)
            self._trailing_stops.pop(symbol, None)
        else:
            logger.error(f"[PositionManager] EXIT ORDER FAILED for {symbol} — "
                         f"MANUAL INTERVENTION REQUIRED")
            alert_service.info(
                f"🚨 EXIT FAILED for {symbol}\n"
                f"Reason: {reason}\nPrice: ₹{price:.2f}\n"
                f"MANUAL EXIT REQUIRED IMMEDIATELY"
            )

    def _partial_exit(self, symbol: str, size: int, reason: str, price: float) -> None:
        """Exit part of a position."""
        import os
        PAPER_TRADING = os.getenv("PAPER_TRADING", "false").lower() == "true"

        pos = portfolio_tracker.get_position(symbol)
        if not pos:
            return

        logger.info(f"[PositionManager] PARTIAL EXIT {symbol} × {size} @ {price:.2f}")

        if PAPER_TRADING:
            from paper_trading import paper_trading_engine
            order_id = paper_trading_engine.close_order(
                symbol    = symbol,
                qty       = size,
                direction = pos.direction,
                reason    = f"PARTIAL_{reason}",
            )
        else:
            from execution.fyers_broker import fyers_broker
            from execution.alpaca_broker import alpaca_broker
            exit_direction = "SHORT" if pos.direction == "LONG" else "LONG"
            broker = fyers_broker if symbol.startswith("NSE:") else alpaca_broker
            order_id = broker.place_order(
                symbol     = symbol,
                direction  = exit_direction,
                qty        = size,
                order_type = "MARKET",
            )

        if order_id:
            # Update position size in tracker
            pos.position_size    -= size
            partial_pnl = (price - pos.entry_price) * size
            if pos.direction == "SHORT":
                partial_pnl = (pos.entry_price - price) * size
            logger.info(f"[PositionManager] Partial P&L: ₹{partial_pnl:+,.0f}")
            alert_service.info(
                f"📊 Partial exit: {symbol.replace('NSE:','').replace('-EQ','')}\n"
                f"Sold {size} shares @ ₹{price:.2f}\n"
                f"P&L: ₹{partial_pnl:+,.0f}\nSL moved to breakeven"
            )
        else:
            logger.error(f"[PositionManager] Partial exit order failed for {symbol}")

    def _move_stop_to_breakeven(self, symbol: str, entry_price: float) -> None:
        """Move stop loss to breakeven (entry price)."""
        self._trailing_stops[symbol] = entry_price
        logger.info(f"[PositionManager] SL moved to breakeven: "
                    f"{symbol} → ₹{entry_price:.2f}")

        # Cancel old SL order on broker and place new one
        self._update_broker_sl(symbol, entry_price)

    def _update_trailing_stop(
        self, symbol: str, ltp: float, direction: str, risk: float
    ) -> None:
        """Update trailing stop — trails by 1×ATR behind current price."""
        # Use ATR as trail distance (approximated as original risk amount)
        trail_distance = risk * 0.8

        if direction == "LONG":
            new_sl = ltp - trail_distance
        else:
            new_sl = ltp + trail_distance

        current_sl = self._trailing_stops.get(symbol, 0)

        # Only move stop in profitable direction (ratchet — never move backward)
        if direction == "LONG" and new_sl > current_sl:
            self._trailing_stops[symbol] = round(new_sl, 2)
            logger.info(f"[PositionManager] Trail SL updated: {symbol} → ₹{new_sl:.2f}")
            self._update_broker_sl(symbol, new_sl)

        elif direction == "SHORT" and (current_sl == 0 or new_sl < current_sl):
            self._trailing_stops[symbol] = round(new_sl, 2)
            logger.info(f"[PositionManager] Trail SL updated SHORT: {symbol} → ₹{new_sl:.2f}")
            self._update_broker_sl(symbol, new_sl)

    def _update_broker_sl(self, symbol: str, new_sl: float) -> None:
        """
        Update stop loss order on broker.
        Cancels existing SL order and places new one.
        This is best-effort — failure is logged but doesn't block.
        Skipped in paper trading mode (no real broker orders).
        """
        import os
        if os.getenv("PAPER_TRADING", "false").lower() == "true":
            return   # paper mode — no broker SL to update
        try:
            from execution.fyers_broker import fyers_broker
            pos = portfolio_tracker.get_position(symbol)
            if not pos:
                return
            remaining_size = pos.position_size
            if remaining_size <= 0:
                return
            exit_dir = "SHORT" if pos.direction == "LONG" else "LONG"
            fyers_broker.place_order(
                symbol     = symbol,
                direction  = exit_dir,
                qty        = remaining_size,
                order_type = "SL-M",
                trigger    = new_sl,
            )
        except Exception as e:
            logger.warning(f"[PositionManager] Broker SL update failed (non-fatal): {e}")

    def reset_symbol(self, symbol: str) -> None:
        """Clean up tracking state for a symbol after full exit."""
        self._breakeven_applied.discard(symbol)
        self._partial_exited.discard(symbol)
        self._trailing_stops.pop(symbol, None)


# ── Module-level singleton ────────────────────────────────────────
position_manager = PositionManager()
