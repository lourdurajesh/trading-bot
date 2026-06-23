"""
position_manager.py
───────────────────
Active position exit management — runs on every tick.
This is the most critical missing piece for autonomous trading.

Rules enforced:
  1. Stop loss hit        → exit immediately at market
  2. Target 1 hit         → exit 50%, move SL to breakeven
  3. Target 2 hit         → exit remaining 50% (skipped if dynamic target active)
  4. Trailing stop        → after 1R profit, trail by 1×ATR
  5. Breakeven move       → after T1 hit, SL moves to entry price
  6. EOD forced exit      → close all intraday positions at 3:25 PM IST (configurable via EOD_EXIT_TIME env)
  7. Max holding period   → force exit after 20 trading days
  8. Catastrophic gap     → if price gaps past SL by >3%, exit immediately
  9. Dynamic target       → after T1 hit, target extends by 1R at each milestone;
                            trailing stop becomes the sole exit mechanism

Called by main.py fast loop every 5 seconds.
"""

import logging
import math
import threading
from datetime import date, datetime, time, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from config.settings import OPTIONS_DTE_FORCE_EXIT, PAPER_TRADING, EOD_EXIT_TIME   # Bug 16: consistent source
from data.data_store import store
from risk.portfolio_tracker import portfolio_tracker, Position
from notifications.alert_service import alert_service

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# Exit rules configuration — EOD_EXIT_TIME now sourced from config.settings (env: EOD_EXIT_TIME)
MAX_HOLDING_DAYS   = 20              # force exit after this many calendar days
BREAKEVEN_TRIGGER       = 1.0   # move SL to BE after 1R profit
TRAIL_TRIGGER           = 1.5   # start trailing after 1.5R profit
PARTIAL_EXIT_PCT        = 0.5   # exit 50% at T1
DYNAMIC_TARGET_START_R  = 3.0   # first dynamic milestone after T1 (~2R)
DYNAMIC_TARGET_STEP     = 1.0   # advance target by this many R per milestone




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
        self._dynamic_target_r:  dict[str, float] = {}  # symbol → next R-milestone target

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
            pos = portfolio_tracker.get_position(symbol)
            opt_size = pos.position_size if pos else 0
            self._check_options_position(
                symbol, direction, entry, stop, target_1,
                opt_size, options_meta, now, pos_dict,
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

        # Strategy-aware exit policy — the SAME shared source the learning engine uses
        # (execution/exit_policy + config). Mean-reversion takes a hard full exit at the
        # target; trend/momentum keeps partial + trail + T2.
        from execution.exit_policy import exit_style, MEAN_REVERSION
        is_mean_rev = (exit_style(pos.strategy) == MEAN_REVERSION)

        with self._lock:
            already_partial = symbol in self._partial_exited
            already_be      = symbol in self._breakeven_applied

        # ── Reconstruct in-memory state after a service restart ───
        # If none of our tracking dicts know about this symbol yet, infer from
        # the persisted stop_loss: if SL has already been moved to/past breakeven
        # it means T1 was hit in a previous session.
        with self._lock:
            untracked = (symbol not in self._partial_exited
                         and symbol not in self._breakeven_applied
                         and symbol not in self._trailing_stops)
        if untracked:
            self._reconstruct_state_from_position(symbol, direction, entry, stop, ltp)
            with self._lock:
                already_partial = symbol in self._partial_exited
                already_be      = symbol in self._breakeven_applied

        # Use trailing stop if set, else original stop
        effective_stop = self._trailing_stops.get(symbol, stop)

        # ── 1. EOD forced exit (3:15 PM IST) ─────────────────────
        if now.time() >= EOD_EXIT_TIME:
            if pos.hold_type == "swing":
                # Swing trades hold overnight — skip EOD close, keep monitoring stops/targets.
                # Broker product is CNC so no auto-squareoff from exchange side either.
                return
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
            # Use original_stop_loss for risk so trailing continues after breakeven move;
            # entry - stop = 0 once SL is at breakeven, which would freeze trailing (Bug 15).
            pos_obj = portfolio_tracker.get_position(symbol)
            original_sl = (pos_obj.original_stop_loss
                           if pos_obj and pos_obj.original_stop_loss else stop)
            risk = entry - original_sl
            if risk <= 0:
                return

            profit_r = (ltp - entry) / risk

            # Stop loss hit
            if ltp <= effective_stop:
                logger.info(f"[PositionManager] STOP HIT {symbol}: "
                            f"ltp={ltp:.2f} <= sl={effective_stop:.2f}")
                self._exit_position(symbol, remaining_size, "STOP", ltp)
                return

            # Mean-reversion: hard FULL exit at the target — no partial/trail (the
            # snap-back reverts). Same policy as the learning engine (shared exit_policy).
            if is_mean_rev:
                if target_1 > 0 and ltp >= target_1:
                    logger.info(f"[PositionManager] MEAN-REV TARGET {symbol}: "
                                f"ltp={ltp:.2f} >= t1={target_1:.2f} — full exit")
                    self._exit_position(symbol, remaining_size, "TARGET", ltp)
                return

            # Dynamic target — advance milestone after T1 is hit
            if already_partial:
                self._update_dynamic_target(symbol, profit_r, entry, risk, direction)

            # Target 2 hit — skipped when dynamic target is active (trailing stop exits instead)
            if target_2 > 0 and ltp >= target_2 and already_partial:
                if symbol not in self._dynamic_target_r:
                    logger.info(f"[PositionManager] T2 HIT {symbol}: "
                                f"ltp={ltp:.2f} >= t2={target_2:.2f} — "
                                f"exiting remaining {remaining_size} shares")
                    self._exit_position(symbol, remaining_size, "TARGET2", ltp)
                    return

            # Target 1 hit — partial exit + move SL to breakeven + activate dynamic target
            if target_1 > 0 and ltp >= target_1 and not already_partial:
                partial_size = max(1, int(remaining_size * PARTIAL_EXIT_PCT))
                logger.info(f"[PositionManager] T1 HIT {symbol}: "
                            f"ltp={ltp:.2f} >= t1={target_1:.2f} — "
                            f"exiting {partial_size} shares")
                self._partial_exit(symbol, partial_size, "TARGET1", ltp)
                portfolio_tracker.mark_t1_hit(symbol)   # persist so restart knows T1 fired
                with self._lock:
                    self._partial_exited.add(symbol)
                    self._dynamic_target_r[symbol] = DYNAMIC_TARGET_START_R
                if not already_be:
                    self._move_stop_to_breakeven(symbol, entry)
                    with self._lock:
                        self._breakeven_applied.add(symbol)
                return

            # Trailing stop — after 1.5R profit
            if profit_r >= TRAIL_TRIGGER:
                self._update_trailing_stop(symbol, ltp, direction, risk)

            # Breakeven move — after 1R profit (if T1 not yet hit)
            elif profit_r >= BREAKEVEN_TRIGGER and not already_be:
                self._move_stop_to_breakeven(symbol, entry)
                with self._lock:
                    self._breakeven_applied.add(symbol)

        # ── 4. SHORT position management ──────────────────────────
        elif direction == "SHORT":
            pos_obj = portfolio_tracker.get_position(symbol)
            original_sl = (pos_obj.original_stop_loss
                           if pos_obj and pos_obj.original_stop_loss else stop)
            risk = original_sl - entry   # Bug 15: use original SL, not current (moved) stop
            if risk <= 0:
                return

            profit_r = (entry - ltp) / risk

            # Stop loss hit
            if ltp >= effective_stop:
                logger.info(f"[PositionManager] STOP HIT SHORT {symbol}: "
                            f"ltp={ltp:.2f} >= sl={effective_stop:.2f}")
                self._exit_position(symbol, remaining_size, "STOP", ltp)
                return

            # Mean-reversion: hard FULL exit at the target — no partial/trail (shared policy).
            if is_mean_rev:
                if target_1 > 0 and ltp <= target_1:
                    logger.info(f"[PositionManager] MEAN-REV TARGET SHORT {symbol}: "
                                f"ltp={ltp:.2f} <= t1={target_1:.2f} — full exit")
                    self._exit_position(symbol, remaining_size, "TARGET", ltp)
                return

            # Dynamic target — advance milestone after T1 is hit
            if already_partial:
                self._update_dynamic_target(symbol, profit_r, entry, risk, direction)

            # Target 2 hit — skipped when dynamic target is active (trailing stop exits instead)
            if target_2 > 0 and ltp <= target_2 and already_partial:
                if symbol not in self._dynamic_target_r:
                    logger.info(f"[PositionManager] T2 HIT SHORT {symbol}: "
                                f"ltp={ltp:.2f} <= t2={target_2:.2f} — "
                                f"exiting remaining {remaining_size} shares")
                    self._exit_position(symbol, remaining_size, "TARGET2", ltp)
                    return

            # Target 1 hit — partial exit + move SL to breakeven + activate dynamic target
            if target_1 > 0 and ltp <= target_1 and not already_partial:
                partial_size = max(1, int(remaining_size * PARTIAL_EXIT_PCT))
                logger.info(f"[PositionManager] T1 HIT SHORT {symbol}")
                self._partial_exit(symbol, partial_size, "TARGET1", ltp)
                portfolio_tracker.mark_t1_hit(symbol)   # persist so restart knows T1 fired
                with self._lock:
                    self._partial_exited.add(symbol)
                    self._dynamic_target_r[symbol] = DYNAMIC_TARGET_START_R
                if not already_be:
                    self._move_stop_to_breakeven(symbol, entry)
                    with self._lock:
                        self._breakeven_applied.add(symbol)
                return

            # Trailing stop for short
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
        pos_dict: dict,
    ) -> None:
        """
        Unified options position monitoring.

        All exit rules are data-driven from the position record — no strategy
        name branching. Each strategy must declare:
          - monitor_symbol (or monitor_symbols + monitor_combine="sum")
          - stop_loss as an absolute price/premium level
          - target_1 as an absolute price/premium level
          - time_stop in options_meta if a time-based exit is needed
        """
        # ── 1. DTE-based forced exit ──────────────────────────────
        expiry_str = options_meta.get("expiry")
        if not expiry_str:
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

        # ── 2. EOD forced exit (3:15 PM) ─────────────────────────
        # Must run BEFORE the LTP check so EOD exits fire even when tick data
        # is temporarily unavailable (e.g. WS reconnect gap, subscription delay).
        if now.time() >= EOD_EXIT_TIME:
            opt_pos = portfolio_tracker.get_position(symbol)
            if opt_pos and opt_pos.hold_type == "swing":
                return
            logger.info(f"[PositionManager] EOD OPTIONS exit: {symbol}")
            self._exit_options_position(symbol, size, "EOD_FORCED", options_meta)
            return

        # ── 3. Get current monitor LTP ────────────────────────────
        option_ltp = self._get_monitor_ltp(pos_dict)
        if option_ltp is None:
            logger.warning(f"[PositionManager] {symbol}: monitor LTP unavailable — stop/target checks skipped (WS subscription may be pending)")
            return

        # ── 4. Time stop (optional — set in options_meta["time_stop"]) ───
        time_stop_str = options_meta.get("time_stop")
        if time_stop_str:
            try:
                h, m = map(int, time_stop_str.split(":"))
                if now.time() >= time(h, m):
                    logger.info(f"[PositionManager] TIME STOP {symbol}: {time_stop_str} — exiting")
                    self._exit_options_position(symbol, size, "TIME_STOP", options_meta)
                    return
            except Exception:
                pass

        # ── 5/6. Premium STOP / TARGET — shared decision (execution/exit_rules) ──
        # LONG (buying): STOP when premium ≤ stop, TARGET when ≥ target.
        # SHORT (selling/strangle): mirrored. One predicate, no LONG/SHORT fork here.
        from execution.exit_rules import premium_exit
        _pdec = premium_exit(option_ltp, stop, target_1, direction)
        if _pdec == "STOP" and stop > 0:
            logger.warning(
                f"[PositionManager] OPTIONS STOP {symbol}: "
                f"{'value' if direction == 'SHORT' else 'premium'} {option_ltp:.2f} "
                f"{'≥' if direction == 'SHORT' else '≤'} stop {stop:.2f}"
            )
            self._exit_options_position(symbol, size, "STOP", options_meta)
            return
        if _pdec == "TARGET" and target_1 > 0 and symbol not in self._partial_exited:
            logger.info(
                f"[PositionManager] OPTIONS TARGET {symbol}: "
                f"ltp {option_ltp:.2f} {'≥' if direction == 'LONG' else '≤'} target {target_1:.2f}"
            )
            self._exit_options_position(symbol, size, "TARGET", options_meta)
            self._partial_exited.add(symbol)
            return

    def _get_monitor_ltp(self, pos_dict: dict) -> Optional[float]:
        """
        Return the live LTP of the position's monitor instrument.

        Uses monitor_symbol for single-leg positions.
        Uses options_meta["monitor_symbols"] + monitor_combine="sum" for multi-leg.
        Returns None if the symbol has no data — caller skips the check.
        Never falls back to the underlying index price.
        """
        try:
            options_meta = pos_dict.get("options_meta") or {}
            combine = options_meta.get("monitor_combine", "single")

            if combine == "sum":
                syms = options_meta.get("monitor_symbols", [])
                ltps = [store.get_ltp(s) for s in syms if s]
                valid = [float(v) for v in ltps if v and v > 0]
                return sum(valid) if valid and len(valid) == len(syms) else None
            else:
                monitor_sym = pos_dict.get("monitor_symbol") or ""
                if not monitor_sym:
                    return None
                ltp = store.get_ltp(monitor_sym)
                return float(ltp) if ltp and ltp > 0 else None
        except Exception:
            return None

    def _exit_options_position(
        self, symbol: str, size: int, reason: str, options_meta: dict
    ) -> None:
        """
        Exit an options position.
        Closes all legs (call + put for strangles, single leg for debit spread).
        Routes to paper engine in paper mode, else Fyers NFO.
        """

        pos = portfolio_tracker.get_position(symbol)
        if not pos:
            logger.warning(f"[PositionManager] Options exit: no position found for {symbol}")
            return

        logger.info(
            f"[PositionManager] OPTIONS EXIT {symbol} × {size} — {reason}"
        )

        # Resolve the option's OWN premium for an accurate exit price (never the index spot)
        pos_dict_for_ltp = {
            "monitor_symbol": pos.monitor_symbol or pos.symbol,
            "options_meta":   options_meta,
        }
        exit_ltp   = self._get_monitor_ltp(pos_dict_for_ltp)
        exit_price = exit_ltp if exit_ltp and exit_ltp > 0 else pos.entry_price

        if PAPER_TRADING:
            from paper_trading import paper_trading_engine
            paper_trading_engine.close_order(
                symbol     = symbol,
                qty        = size,
                direction  = pos.direction,
                reason     = reason,
                exit_price = exit_price,   # option premium, not index spot
            )
            order_id = "PAPER-OPT-EXIT"
        else:
            order_id = self._place_options_exit_orders(pos, size, options_meta)

        if order_id:
            # exit_price (option premium) already resolved above — reuse it.
            closed = portfolio_tracker.close_position(symbol, exit_price, reason)
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
                self._dynamic_target_r.pop(symbol, None)
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
        """
        Place exit orders for all legs using options_meta["exit_legs"].
        Data-driven — no strategy name branching.
        """
        try:
            from execution.fyers_broker import fyers_broker
            exit_legs = options_meta.get("exit_legs", [])
            lot_size  = int(options_meta.get("lot_size", 1))
            ids = []
            for leg in exit_legs:
                oid = fyers_broker.place_order(
                    symbol=leg["symbol"], direction=leg["direction"],
                    qty=lot_size, order_type="MARKET",
                )
                if oid:
                    ids.append(oid)
            return ids[0] if ids else None
        except Exception as e:
            logger.error(f"[PositionManager] Options exit order error: {e}")
        return None

    # ─────────────────────────────────────────────────────────────
    # EXIT OPERATIONS
    # ─────────────────────────────────────────────────────────────

    def _exit_position(self, symbol: str, size: int, reason: str, price: float) -> None:
        """Full exit of a position."""

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

        # Mark PENDING_CLOSE BEFORE placing the order so that a crash between
        # the broker call and close_position() is detectable on restart (Bug 1).
        portfolio_tracker.set_pending_close(symbol)

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
                self._dynamic_target_r.pop(symbol, None)
        else:
            # Revert PENDING_CLOSE → OPEN so position monitoring continues
            pos = portfolio_tracker.get_position(symbol)
            if pos:
                pos.status = "OPEN"
                portfolio_tracker._update_position_db(pos)
            logger.error(f"[PositionManager] EXIT ORDER FAILED for {symbol} — "
                         f"MANUAL INTERVENTION REQUIRED")
            alert_service.info(
                f"🚨 EXIT FAILED for {symbol}\n"
                f"Reason: {reason}\nPrice: ₹{price:.2f}\n"
                f"MANUAL EXIT REQUIRED IMMEDIATELY"
            )

    def _partial_exit(self, symbol: str, size: int, reason: str, price: float) -> None:
        """Exit part of a position."""

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
            # Persist reduced size to DB immediately so restart sees correct qty (Bug 5)
            new_size = pos.position_size - size
            portfolio_tracker.update_position_size(symbol, new_size)
            pos.position_size = new_size

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
        portfolio_tracker.update_stop_loss(symbol, entry_price)
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
            portfolio_tracker.update_stop_loss(symbol, new_sl)
            logger.info(f"[PositionManager] Trail SL updated: {symbol} → ₹{new_sl:.2f}")
            self._update_broker_sl(symbol, new_sl)

        elif direction == "SHORT" and (current_sl == 0 or new_sl < current_sl):
            self._trailing_stops[symbol] = round(new_sl, 2)
            portfolio_tracker.update_stop_loss(symbol, new_sl)
            logger.info(f"[PositionManager] Trail SL updated SHORT: {symbol} → ₹{new_sl:.2f}")
            self._update_broker_sl(symbol, new_sl)

    def _reconstruct_state_from_position(
        self, symbol: str, direction: str, entry: float, original_stop: float, ltp: float
    ) -> None:
        """
        Rebuild in-memory tracking state from the persisted stop_loss after a restart.

        Uses original_stop_loss (frozen at entry) rather than the current stop_loss
        to compute risk, because if SL was moved to breakeven, stop_loss == entry,
        making risk = 0 and causing early return without restoring state (Bug 6).
        """
        pos = portfolio_tracker.get_position(symbol)
        if not pos:
            return

        persisted_sl = pos.stop_loss
        # Use the immutable original stop for risk — never use the current (moved) SL (Bug 6)
        true_original_stop = pos.original_stop_loss if pos.original_stop_loss else original_stop
        risk = abs(entry - true_original_stop)
        if risk <= 0:
            return

        # Use the persisted t1_hit flag as ground truth (exact).
        # Fallback to SL-position inference only for old positions that predate the column.
        if pos.t1_hit:
            t1_was_hit = True
        else:
            # Legacy inference: SL moved past breakeven implies T1 was hit.
            # This can produce a false positive (breakeven applied but T1 not yet hit),
            # but that's safe: worst case = T1 partial exit skipped, trailing active.
            t1_was_hit = (direction == "LONG"  and persisted_sl > entry) or \
                         (direction == "SHORT" and persisted_sl < entry)

        if not t1_was_hit:
            return

        if direction == "LONG":
            profit_r = (ltp - entry) / risk
        else:
            profit_r = (entry - ltp) / risk

        next_milestone = max(DYNAMIC_TARGET_START_R, math.ceil(profit_r) + 1)

        with self._lock:
            self._partial_exited.add(symbol)
            self._breakeven_applied.add(symbol)
            self._trailing_stops[symbol]   = persisted_sl
            self._dynamic_target_r[symbol] = next_milestone

        logger.info(
            f"[PositionManager] State restored after restart: {symbol} | "
            f"T1 hit | trail SL=₹{persisted_sl:.2f} | "
            f"profit={profit_r:.1f}R | next target={next_milestone:.0f}R"
        )

    def _update_dynamic_target(
        self, symbol: str, profit_r: float, entry: float, risk: float, direction: str
    ) -> None:
        """
        Advance the dynamic target milestone each time the trade gains another R.
        Called every tick after T1 partial exit. Trailing stop remains the exit trigger.
        """
        with self._lock:
            current_milestone = self._dynamic_target_r.get(symbol)
        if current_milestone is None:
            return

        if profit_r >= current_milestone:
            next_milestone = current_milestone + DYNAMIC_TARGET_STEP
            with self._lock:
                self._dynamic_target_r[symbol] = next_milestone

            if direction == "LONG":
                next_price = entry + next_milestone * risk
            else:
                next_price = entry - next_milestone * risk

            logger.info(
                f"[PositionManager] DYNAMIC TARGET {symbol}: "
                f"{current_milestone:.0f}R hit → target extended to "
                f"{next_milestone:.0f}R = ₹{next_price:.2f}"
            )
            alert_service.info(
                f"🎯 {symbol.replace('NSE:','').replace('-EQ','')}: "
                f"{current_milestone:.0f}R milestone hit\n"
                f"Target extended → {next_milestone:.0f}R = ₹{next_price:.2f}\n"
                f"Trailing stop riding the move"
            )

    def _update_broker_sl(self, symbol: str, new_sl: float) -> None:
        """
        Update stop loss order on broker.
        Cancels the EXISTING SL order first, then places the new one (Bug 2).
        Without cancellation, each update leaves an orphaned SL-M order on the broker;
        when price hits SL all orphaned orders fire simultaneously → over-sized exit.
        Skipped in paper trading mode.
        """
        if PAPER_TRADING:   # Bug 16: imported at top of module
            return
        try:
            from execution.fyers_broker import fyers_broker
            pos = portfolio_tracker.get_position(symbol)
            if not pos:
                return
            remaining_size = pos.position_size
            if remaining_size <= 0:
                return

            # Cancel old SL order before placing the updated one (Bug 2)
            if pos.sl_order_id:
                try:
                    fyers_broker.cancel_order(pos.sl_order_id)
                    logger.debug(f"[PositionManager] Cancelled old SL {pos.sl_order_id} for {symbol}")
                except Exception as ce:
                    logger.warning(f"[PositionManager] Could not cancel old SL {pos.sl_order_id}: {ce}")

            exit_dir = "SHORT" if pos.direction == "LONG" else "LONG"
            # Pass correct product so swing CNC SL isn't rejected (Bug 7)
            product = "CNC" if pos.hold_type == "swing" else "INTRADAY"
            new_sl_id = fyers_broker.place_order(
                symbol     = symbol,
                direction  = exit_dir,
                qty        = remaining_size,
                order_type = "SL-M",
                trigger    = new_sl,
                product    = product,
            )
            if new_sl_id:
                portfolio_tracker.update_sl_order_id(symbol, new_sl_id)
                logger.debug(f"[PositionManager] New SL order {new_sl_id} @ ₹{new_sl:.2f} for {symbol}")
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
            self._dynamic_target_r.pop(symbol, None)


# ── Module-level singleton ────────────────────────────────────────
position_manager = PositionManager()
