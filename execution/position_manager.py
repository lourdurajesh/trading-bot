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
from datetime import date, datetime, time, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from config.settings import OPTIONS_DTE_FORCE_EXIT
from data.data_store import store
from risk.portfolio_tracker import portfolio_tracker, Position
from notifications.alert_service import alert_service

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# Exit rules configuration
EOD_EXIT_TIME      = time(15, 15)    # 3:15 PM IST — close intraday positions
MAX_HOLDING_DAYS   = 20              # force exit after this many calendar days
BREAKEVEN_TRIGGER  = 1.0             # move SL to BE after 1R profit
TRAIL_TRIGGER      = 1.5             # start trailing after 1.5R profit
PARTIAL_EXIT_PCT   = 0.5             # exit 50% at T1

# Options-specific exit thresholds
OPTIONS_DEBIT_STOP_PCT   = 0.50     # exit debit spread when premium drops to 50% of entry
OPTIONS_CREDIT_STOP_MULT = 2.0      # exit short strangle when value rises to 2× original credit


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

        now_ist = datetime.now(tz=IST)   # always IST regardless of server timezone

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
        symbol      = pos_dict.get("symbol", "")
        direction   = pos_dict.get("direction", "LONG")
        entry       = float(pos_dict.get("entry_price", 0))
        stop        = float(pos_dict.get("stop_loss", 0))
        target_1    = float(pos_dict.get("target_1", 0))
        target_2    = float(pos_dict.get("target_2", 0))
        entry_time  = pos_dict.get("entry_time", "")
        signal_type = pos_dict.get("signal_type", "EQUITY")
        options_meta = pos_dict.get("options_meta") or {}

        # ── OPTIONS positions — separate exit management ──────────
        if signal_type == "OPTIONS":
            self._check_options_position(
                symbol, direction, entry, stop, target_1,
                0, options_meta, now,
            )
            return

        ltp = store.get_ltp(symbol)
        if not ltp or ltp <= 0:
            return

        # Always use live position size from tracker (not stale pos_dict snapshot)
        pos = portfolio_tracker.get_position(symbol)
        if not pos or pos.position_size <= 0:
            return
        remaining_size = pos.position_size

        with self._lock:
            already_partial = symbol in self._partial_exited
            already_be      = symbol in self._breakeven_applied

        # Use trailing stop if set, else original stop
        effective_stop = self._trailing_stops.get(symbol, stop)

        # ── 1. EOD forced exit (3:15 PM IST) ─────────────────────
        if now.time() >= EOD_EXIT_TIME:
            logger.info(f"[PositionManager] EOD exit: {symbol} × {remaining_size}")
            self._exit_position(symbol, remaining_size, "EOD_FORCED", ltp)
            return

        # ── 2. Max holding period ─────────────────────────────────
        if entry_time:
            try:
                entry_dt  = datetime.fromisoformat(entry_time.replace("Z", "+00:00"))
                days_held = (datetime.now(tz=IST) - entry_dt).days
                if days_held >= MAX_HOLDING_DAYS:
                    logger.info(f"[PositionManager] Max hold {days_held}d: {symbol}")
                    self._exit_position(symbol, remaining_size, "MAX_HOLD", ltp)
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
                self._exit_position(symbol, remaining_size, "STOP", ltp)
                return

            # Target 2 hit — exit remaining position (only after T1 partial exit)
            if target_2 > 0 and ltp >= target_2 and already_partial:
                logger.info(f"[PositionManager] T2 HIT {symbol}: "
                            f"ltp={ltp:.2f} >= t2={target_2:.2f} — "
                            f"exiting remaining {remaining_size} shares")
                self._exit_position(symbol, remaining_size, "TARGET2", ltp)
                return

            # Target 1 hit — partial exit + move SL to breakeven
            if target_1 > 0 and ltp >= target_1 and not already_partial:
                partial_size = max(1, int(remaining_size * PARTIAL_EXIT_PCT))
                logger.info(f"[PositionManager] T1 HIT {symbol}: "
                            f"ltp={ltp:.2f} >= t1={target_1:.2f} — "
                            f"exiting {partial_size} shares")
                self._partial_exit(symbol, partial_size, "TARGET1", ltp)
                with self._lock:
                    self._partial_exited.add(symbol)
                if not already_be:
                    self._move_stop_to_breakeven(symbol, entry)
                    with self._lock:
                        self._breakeven_applied.add(symbol)
                return

            # Trailing stop — after 1.5R profit
            profit_r = (ltp - entry) / risk
            if profit_r >= TRAIL_TRIGGER:
                self._update_trailing_stop(symbol, ltp, direction, risk)

            # Breakeven move — after 1R profit (if T1 not yet hit)
            elif profit_r >= BREAKEVEN_TRIGGER and not already_be:
                self._move_stop_to_breakeven(symbol, entry)
                with self._lock:
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
                self._exit_position(symbol, remaining_size, "STOP", ltp)
                return

            # Target 2 hit — exit remaining (only after T1 partial exit)
            if target_2 > 0 and ltp <= target_2 and already_partial:
                logger.info(f"[PositionManager] T2 HIT SHORT {symbol}: "
                            f"ltp={ltp:.2f} <= t2={target_2:.2f} — "
                            f"exiting remaining {remaining_size} shares")
                self._exit_position(symbol, remaining_size, "TARGET2", ltp)
                return

            # Target 1 hit — partial exit + move SL to breakeven
            if target_1 > 0 and ltp <= target_1 and not already_partial:
                partial_size = max(1, int(remaining_size * PARTIAL_EXIT_PCT))
                logger.info(f"[PositionManager] T1 HIT SHORT {symbol}")
                self._partial_exit(symbol, partial_size, "TARGET1", ltp)
                with self._lock:
                    self._partial_exited.add(symbol)
                if not already_be:
                    self._move_stop_to_breakeven(symbol, entry)
                    with self._lock:
                        self._breakeven_applied.add(symbol)
                return

            # Trailing stop for short
            profit_r = (entry - ltp) / risk
            if profit_r >= TRAIL_TRIGGER:
                self._update_trailing_stop(symbol, ltp, direction, risk)

    # ─────────────────────────────────────────────────────────────
    # OPTIONS EXIT MANAGEMENT
    # ─────────────────────────────────────────────────────────────

    def _check_options_position(
        self,
        symbol: str,
        direction: str,
        entry: float,
        stop: float,
        target_1: float,
        size: int,
        options_meta: dict,
        now: datetime,
    ) -> None:
        """
        Options-specific position monitoring.

        Key differences from equity:
        - Monitor OPTION premium LTP (via NFO symbol), not underlying price
        - No trailing stop (theta decay changes the math)
        - DTE-based forced exit (expiry risk)
        - Debit spread: stop = 50% of entry premium (option value halved)
        - Short strangle: stop = 2× original credit received (value doubles)
        - EOD exit applies (don't hold overnight unless explicitly swing)
        """
        strategy = options_meta.get("strategy", "")

        # ── 1. DTE-based forced exit ──────────────────────────────
        expiry_str = options_meta.get("expiry")
        if not expiry_str:
            # Try to derive from NFO symbol embedded in options_meta
            nfo = options_meta.get("nfo_symbol") or options_meta.get("nfo_call")
            if nfo:
                try:
                    from risk.options_risk import options_risk_gate
                    expiry_str = options_risk_gate._parse_expiry_from_symbol(nfo)
                except Exception:
                    pass

        if expiry_str:
            try:
                expiry_dt = date.fromisoformat(expiry_str)
                dte = (expiry_dt - datetime.now(tz=IST).date()).days
                if dte <= OPTIONS_DTE_FORCE_EXIT:
                    logger.warning(
                        f"[PositionManager] OPTIONS DTE EXIT {symbol}: "
                        f"{dte} days to expiry — closing to avoid expiry risk"
                    )
                    self._exit_options_position(symbol, size, "DTE_FORCED", options_meta)
                    return
            except Exception:
                pass

        # ── 2. Get current option premium LTP ─────────────────────
        # For debit spreads, monitor the long leg NFO symbol
        # For short strangles, monitor both legs
        option_ltp = self._get_option_ltp(options_meta, strategy)

        if option_ltp is None:
            # Cannot get option price — use underlying as fallback
            ltp = store.get_ltp(symbol)
            if ltp:
                option_ltp = ltp
            else:
                return

        # ── 3. EOD forced exit (3:15 PM) ─────────────────────────
        if now.time() >= EOD_EXIT_TIME:
            logger.info(f"[PositionManager] EOD OPTIONS exit: {symbol}")
            self._exit_options_position(symbol, size, "EOD_FORCED", options_meta)
            return

        # ── 4. Debit spread exit rules ────────────────────────────
        if strategy == "debit_spread":
            # Entry = debit paid (premium). Stop = 50% of premium (option halved in value).
            stop_level = entry * OPTIONS_DEBIT_STOP_PCT
            if option_ltp <= stop_level:
                logger.warning(
                    f"[PositionManager] OPTIONS STOP {symbol}: "
                    f"premium {option_ltp:.2f} <= stop {stop_level:.2f} "
                    f"(50% of entry {entry:.2f})"
                )
                self._exit_options_position(symbol, size, "STOP_50PCT_PREMIUM", options_meta)
                return

            # Profit target — option premium reached target_1
            if option_ltp >= target_1 and symbol not in self._partial_exited:
                logger.info(
                    f"[PositionManager] OPTIONS TARGET {symbol}: "
                    f"premium {option_ltp:.2f} >= target {target_1:.2f}"
                )
                self._exit_options_position(symbol, size, "TARGET1", options_meta)
                self._partial_exited.add(symbol)
                return

        # ── 5. Short strangle exit rules ─────────────────────────
        elif strategy == "short_strangle":
            # Entry = credit received. Stop = 2× credit (position value doubled)
            # For short strangle, current_value = sum of current call + put premiums
            stop_level = entry * OPTIONS_CREDIT_STOP_MULT
            if option_ltp >= stop_level:
                logger.warning(
                    f"[PositionManager] SHORT STRANGLE STOP {symbol}: "
                    f"current value {option_ltp:.2f} >= stop {stop_level:.2f} "
                    f"(2× credit {entry:.2f})"
                )
                self._exit_options_position(symbol, size, "STOP_2X_CREDIT", options_meta)
                return

            # Profit target — value decayed to 50% of original credit (50% profit)
            profit_target = entry * 0.50
            if option_ltp <= profit_target and symbol not in self._partial_exited:
                logger.info(
                    f"[PositionManager] STRANGLE TARGET {symbol}: "
                    f"value {option_ltp:.2f} <= target {profit_target:.2f} "
                    f"(50% profit on credit)"
                )
                self._exit_options_position(symbol, size, "TARGET_50PCT_CREDIT", options_meta)
                self._partial_exited.add(symbol)
                return

    def _get_option_ltp(self, options_meta: dict, strategy: str) -> Optional[float]:
        """
        Fetch current option premium from data store.
        For debit spreads: long leg LTP.
        For short strangles: sum of call + put LTP (total position value).
        """
        try:
            if strategy == "debit_spread":
                nfo = options_meta.get("nfo_symbol")
                if nfo:
                    ltp = store.get_ltp(nfo)
                    return float(ltp) if ltp and ltp > 0 else None

            elif strategy == "short_strangle":
                call_sym = options_meta.get("nfo_call")
                put_sym  = options_meta.get("nfo_put")
                call_ltp = store.get_ltp(call_sym) if call_sym else None
                put_ltp  = store.get_ltp(put_sym)  if put_sym  else None
                if call_ltp and put_ltp:
                    return float(call_ltp) + float(put_ltp)
        except Exception:
            pass
        return None

    def _exit_options_position(
        self, symbol: str, size: int, reason: str, options_meta: dict
    ) -> None:
        """
        Exit an options position.
        Closes all legs (call + put for strangles, single leg for debit spread).
        Routes to paper engine in paper mode, else Fyers NFO.
        """
        import os
        PAPER_TRADING = os.getenv("PAPER_TRADING", "false").lower() == "true"

        pos = portfolio_tracker.get_position(symbol)
        if not pos:
            logger.warning(f"[PositionManager] Options exit: no position found for {symbol}")
            return

        logger.info(
            f"[PositionManager] OPTIONS EXIT {symbol} × {size} — {reason}"
        )

        if PAPER_TRADING:
            from paper_trading import paper_trading_engine
            paper_trading_engine.close_order(
                symbol    = symbol,
                qty       = size,
                direction = pos.direction,
                reason    = reason,
            )
            order_id = "PAPER-OPT-EXIT"
        else:
            order_id = self._place_options_exit_orders(pos, size, options_meta)

        if order_id:
            closed = portfolio_tracker.close_position(symbol, pos.entry_price, reason)
            if closed:
                # Notify options risk gate of the P&L
                try:
                    from risk.options_risk import options_risk_gate
                    from config.settings import TOTAL_CAPITAL
                    options_risk_gate.update_daily_pnl(closed.realised_pnl, TOTAL_CAPITAL)
                except Exception:
                    pass
                alert_service.trade_closed(symbol, closed.realised_pnl, reason)
                self._apply_exit_cooldown(symbol, reason)
            with self._lock:
                self._breakeven_applied.discard(symbol)
                self._partial_exited.discard(symbol)
                self._trailing_stops.pop(symbol, None)
        else:
            logger.error(
                f"[PositionManager] OPTIONS EXIT ORDER FAILED for {symbol} — "
                f"MANUAL INTERVENTION REQUIRED"
            )
            alert_service.info(
                f"🚨 OPTIONS EXIT FAILED: {symbol}\n"
                f"Reason: {reason}\nMANUAL EXIT REQUIRED"
            )

    def _place_options_exit_orders(self, pos, size: int, options_meta: dict) -> Optional[str]:
        """Place live Fyers NFO exit orders for options positions."""
        try:
            from execution.fyers_broker import fyers_broker
            strategy = options_meta.get("strategy", "")

            if strategy == "short_strangle":
                # Buy back both legs to close
                call_sym = options_meta.get("nfo_call")
                put_sym  = options_meta.get("nfo_put")
                lot_size = int(options_meta.get("lot_size", 1))
                ids = []
                if call_sym:
                    ids.append(fyers_broker.place_order(
                        symbol=call_sym, direction="LONG",
                        qty=lot_size, order_type="MARKET",
                    ))
                if put_sym:
                    ids.append(fyers_broker.place_order(
                        symbol=put_sym, direction="LONG",
                        qty=lot_size, order_type="MARKET",
                    ))
                return ids[0] if ids else None

            else:
                # Debit spread — sell the long leg
                nfo = options_meta.get("nfo_symbol")
                if nfo:
                    return fyers_broker.place_order(
                        symbol=nfo, direction="SHORT",
                        qty=size, order_type="MARKET",
                    )
        except Exception as e:
            logger.error(f"[PositionManager] Options exit order error: {e}")
        return None

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
                self._apply_exit_cooldown(symbol, reason)

            # Clean up tracking sets (locked)
            with self._lock:
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

    def _apply_exit_cooldown(self, symbol: str, reason: str) -> None:
        """
        Apply a re-entry cooldown after any position close.
        Persisted to DB so bot restarts don't lose the cooldown.

        Loss/forced exits  → full SYMBOL_COOLDOWN_MINUTES (default 60 min)
        Target/win exits   → 30 min (prevent immediate same-day re-entry)
        """
        from config.settings import SYMBOL_COOLDOWN_MINUTES
        win_reasons  = {"TARGET1", "TARGET2", "TARGET1_PARTIAL", "TARGET_50PCT_CREDIT"}
        loss_reasons = {"STOP", "EOD_FORCED", "MAX_HOLD", "DTE_FORCED",
                        "STOP_50PCT_PREMIUM", "STOP_2X_CREDIT", "SL_PLACEMENT_FAILED"}
        if reason in win_reasons:
            minutes = 30
        elif reason in loss_reasons:
            minutes = SYMBOL_COOLDOWN_MINUTES
        else:
            minutes = 30   # unknown reason — short cooldown as safety net
        try:
            from strategies.strategy_selector import strategy_selector
            strategy_selector.apply_cooldown(symbol, minutes=minutes)
        except Exception as e:
            logger.warning(f"[PositionManager] Could not apply cooldown for {symbol}: {e}")

    def reset_symbol(self, symbol: str) -> None:
        """Clean up tracking state for a symbol after full exit."""
        with self._lock:
            self._breakeven_applied.discard(symbol)
            self._partial_exited.discard(symbol)
            self._trailing_stops.pop(symbol, None)


# ── Module-level singleton ────────────────────────────────────────
position_manager = PositionManager()
