"""
order_manager.py
────────────────
Routes approved signals to execution.
Now includes:
  - Order fill confirmation loop (polls broker until filled)
  - Margin check before every order
  - Atomic entry + SL placement (exit if SL fails)
  - Minimum net profit threshold check
  - Proper handling of rejections and partial fills
"""

import logging
import time
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

from config.settings import BOT_MODE, PAPER_TRADING, TOTAL_CAPITAL   # Bug 16: use settings constant
from risk.portfolio_tracker import portfolio_tracker
from risk.risk_manager import risk_manager
from strategies.base_strategy import Direction, Signal, SignalType

logger = logging.getLogger(__name__)

SIGNAL_EXPIRY_MINUTES  = 30
ORDER_POLL_INTERVAL    = 2      # seconds between fill status checks
ORDER_POLL_MAX_WAIT    = 30     # max seconds to wait for fill
MIN_TRADE_PROFIT       = 500    # minimum expected net profit in INR
BROKERAGE_RATE         = 0.0008 # 0.08% total round-trip brokerage estimate


class OrderManager:

    def __init__(self):
        self._mode             = BOT_MODE
        self._pending_signals: dict[str, Signal] = {}
        self._lock             = threading.Lock()

    def submit(self, signal: Signal) -> Optional[str]:
        """
        Entry point for all signals.
        Runs risk + profit validation, then routes to AUTO or MANUAL.
        """
        # Risk validation
        open_positions = portfolio_tracker.get_open_positions()
        decision       = risk_manager.validate(signal, open_positions)

        if not decision.approved:
            logger.info(f"[OrderManager] REJECTED {signal.symbol}: {decision.reason}")
            try:
                from audit_log import audit_log
                audit_log.rejection(signal, reason=decision.reason, layer="risk")
            except Exception:
                pass
            return None

        # Minimum net profit check
        if not self._check_min_profit(signal, decision.position_size):
            return None

        # Margin check
        if not self._check_margin(signal, decision.position_size):
            return None

        signal.position_size   = decision.position_size
        signal.capital_at_risk = decision.capital_at_risk

        signal_id = str(uuid.uuid4())[:8].upper()

        if self._mode == "AUTO":
            self._execute(signal)
        else:
            self._queue_for_confirmation(signal_id, signal)

        return signal_id

    def confirm(self, signal_id: str) -> bool:
        with self._lock:
            signal = self._pending_signals.pop(signal_id, None)
        if not signal:
            return False
        if signal.expires_at and datetime.now(tz=IST) > signal.expires_at:
            logger.warning(f"[OrderManager] Signal {signal_id} expired")
            return False
        self._execute(signal)
        return True

    def reject(self, signal_id: str) -> bool:
        with self._lock:
            signal = self._pending_signals.pop(signal_id, None)
        if signal:
            logger.info(f"[OrderManager] REJECTED by user: {signal.symbol}")
            return True
        return False

    def get_pending_signals(self) -> list[dict]:
        self._purge_expired_signals()
        with self._lock:
            return [{"signal_id": sid, **sig.to_dict()}
                    for sid, sig in self._pending_signals.items()]

    def set_mode(self, mode: str) -> None:
        if mode.upper() in ("AUTO", "MANUAL"):
            old = self._mode
            self._mode = mode.upper()
            logger.info(f"[OrderManager] Mode → {self._mode}")
            try:
                from audit_log import audit_log
                audit_log.mode_change(old, self._mode)
            except Exception:
                pass

    @property
    def mode(self) -> str:
        return self._mode

    # ─────────────────────────────────────────────────────────────
    # PRE-EXECUTION CHECKS
    # ─────────────────────────────────────────────────────────────

    def _check_min_profit(self, signal: Signal, size: int) -> bool:
        """
        Verify expected net profit exceeds minimum threshold.
        Filters out trades where fees eat the profit.
        """
        risk          = abs(signal.entry - signal.stop_loss)
        gross_profit  = risk * signal.risk_reward * size
        fees          = signal.entry * size * BROKERAGE_RATE
        net_profit    = gross_profit - fees

        if net_profit < MIN_TRADE_PROFIT:
            logger.info(
                f"[OrderManager] SKIP {signal.symbol}: "
                f"net profit ₹{net_profit:.0f} < minimum ₹{MIN_TRADE_PROFIT} "
                f"(gross ₹{gross_profit:.0f} - fees ₹{fees:.0f})"
            )
            return False
        return True

    def _check_margin(self, signal: Signal, size: int) -> bool:
        """
        Verify sufficient margin before placing order.
        Uses broker's available funds.

        Options margin:
          Debit spread  — full premium upfront: entry × position_size
          Short strangle / Iron condor — SPAN margin ≈ 6% of notional per lot
        Equity margin — 25% of notional (intraday bracket).
        """
        try:
            broker = self._get_broker(signal.symbol)
            funds  = broker.get_funds()
            if not funds:
                logger.warning("[OrderManager] Could not fetch funds — proceeding anyway")
                return True

            available = float(
                funds.get("availableBalance", 0)
                or funds.get("cash", 0)
                or funds.get("equity", 0)
                or TOTAL_CAPITAL
            )

            if signal.signal_type == SignalType.OPTIONS:
                meta          = signal.options_meta or {}
                strategy_type = meta.get("strategy", "")
                lot_size      = int(meta.get("lot_size", 1)) or 1
                lots          = max(1, size // lot_size)

                if strategy_type == "debit_spread":
                    # Full premium paid upfront — entry × total units
                    required = signal.entry * size
                else:
                    # Credit strategies: SPAN margin ≈ 6% of underlying notional per lot
                    # NSE SPAN for index options is typically 5–8%
                    from data.data_store import store
                    spot     = store.get_ltp(signal.symbol) or 0
                    if spot <= 0:
                        return True   # can't compute — let it through
                    SPAN_PCT = 0.06
                    required = lots * lot_size * spot * SPAN_PCT
            elif getattr(signal, "hold_type", "intraday") == "swing" and signal.direction.value == "LONG":
                required = signal.entry * size   # CNC delivery: full notional required
            else:
                required = signal.entry * size * 0.25   # INTRADAY: 25% bracket margin

            if available < required:
                logger.warning(
                    f"[OrderManager] INSUFFICIENT MARGIN for {signal.symbol}: "
                    f"available ₹{available:,.0f} < required ₹{required:,.0f}"
                )
                if signal.signal_type == SignalType.OPTIONS:
                    # Options are lot-based — cannot reduce below 1 lot
                    return False
                # Equity: try reducing size to fit available margin
                reduced_size = int(available * 0.9 / (signal.entry * 0.25))
                if reduced_size >= 1:
                    logger.info(f"[OrderManager] Reducing size {size} → {reduced_size}")
                    signal.position_size   = reduced_size
                    signal.capital_at_risk = reduced_size * abs(signal.entry - signal.stop_loss)
                    return True
                return False

        except Exception as e:
            logger.debug(f"[OrderManager] Margin check error (non-fatal): {e}")

        return True

    # ─────────────────────────────────────────────────────────────
    # EXECUTION
    # ─────────────────────────────────────────────────────────────

    def _execute(self, signal: Signal) -> None:
        """
        Atomic execution — routes to paper trading or live broker.
        Options signals are routed to _execute_options() for multi-leg placement.
        """
        # Block new entries for NSE symbols outside trading hours (09:15–15:15 IST).
        # Prevents the open→EOD-forced-close→re-signal infinite loop.
        if signal.symbol.startswith("NSE:"):
            from datetime import time as dtime
            now_ist = datetime.now(tz=IST)
            nse_open   = dtime(9, 15)
            eod_cutoff = dtime(15, 15)   # match position_manager EOD_EXIT_TIME
            if not (nse_open <= now_ist.time() <= eod_cutoff):
                logger.warning(
                    f"[OrderManager] Blocked entry outside NSE hours: "
                    f"{signal.symbol} at {now_ist.strftime('%H:%M:%S')} IST"
                )
                # Apply a 60-min cooldown so the same symbol doesn't re-trigger
                # every cycle for the rest of the session.
                try:
                    from strategies.strategy_selector import strategy_selector
                    strategy_selector.apply_cooldown(signal.symbol, minutes=60)
                except Exception:
                    pass
                return

        # Index symbols (NIFTY/BANKNIFTY) can only be traded as options contracts,
        # never as direct equity orders. Block any non-options signal on an index
        # to prevent accidental broker calls on untradeable instruments.
        if "INDEX" in signal.symbol and signal.signal_type != SignalType.OPTIONS:
            logger.warning(
                f"[OrderManager] BLOCKED equity order on index {signal.symbol} "
                f"(strategy={signal.strategy}) — indices require options contracts"
            )
            return

        # Options: multi-leg execution via dedicated path
        if signal.signal_type == SignalType.OPTIONS:
            self._execute_options(signal)
            return

        # Paper trading mode — simulate execution
        # paper_trading_engine (₹5L wallet) is exclusive to learning mirrors; only
        # portfolio_tracker is written here so Trading tab and Paper P&L tab stay separate.
        if PAPER_TRADING:
            portfolio_tracker.open_position(signal, fill_price=signal.entry)
            logger.info(f"[OrderManager] [PAPER] Trade recorded: {signal.symbol}")
            return

        broker = self._get_broker(signal.symbol)

        # ── Step 1: Place entry order ─────────────────────────────
        logger.info(
            f"[OrderManager] EXECUTING {signal.direction.value} {signal.symbol} "
            f"× {signal.position_size} @ {signal.entry:.2f}"
        )

        # Swing trades use CNC (delivery) so broker doesn't auto-square at EOD.
        # Intraday shorts must use INTRADAY — NSE doesn't allow delivery short selling.
        eq_product = (
            "CNC"
            if getattr(signal, "hold_type", "intraday") == "swing"
            and signal.direction.value == "LONG"
            else "INTRADAY"
        )
        entry_order_id = broker.place_order(
            symbol     = signal.symbol,
            direction  = signal.direction.value,
            qty        = signal.position_size,
            order_type = "MARKET",
            price      = signal.entry,
            product    = eq_product,
        )

        if not entry_order_id:
            logger.error(f"[OrderManager] Entry order placement FAILED: {signal.symbol}")
            self._send_alert(signal, "FAILED", pending=False)
            return

        # ── Step 2: Confirm fill ──────────────────────────────────
        fill_price, fill_qty = self._confirm_fill(broker, entry_order_id, signal)

        if fill_price is None:
            logger.error(
                f"[OrderManager] Entry fill NOT CONFIRMED: {signal.symbol} "
                f"order {entry_order_id} — attempting cancel"
            )
            broker.cancel_order(entry_order_id)
            # Bug 4: do one final check after cancel — order may have already filled
            fill_price, fill_qty = self._confirm_fill(
                broker, entry_order_id, signal, max_wait=5
            )
            if fill_price is None:
                self._send_alert(signal, "FILL_FAILED", pending=False)
                return
            logger.warning(
                f"[OrderManager] Fill detected AFTER cancel attempt for {signal.symbol}: "
                f"qty={fill_qty} @ ₹{fill_price:.2f} — recording position and placing SL"
            )

        logger.info(
            f"[OrderManager] Fill confirmed: {signal.symbol} "
            f"× {fill_qty} @ ₹{fill_price:.2f}"
        )

        # Update entry price to actual fill
        signal.entry = fill_price

        # ── Step 3: Record position ───────────────────────────────
        portfolio_tracker.open_position(signal, fill_price=fill_price)

        # ── Step 4: Place SL order (critical — retry 3 times) ─────
        sl_placed = False
        for attempt in range(1, 4):
            sl_order_id = broker.place_order(
                symbol     = signal.symbol,
                direction  = "SHORT" if signal.direction == Direction.LONG else "LONG",
                qty        = fill_qty,
                order_type = "SL-M",
                trigger    = signal.stop_loss,
            )
            if sl_order_id:
                sl_placed = True
                logger.info(
                    f"[OrderManager] SL order placed: {sl_order_id} "
                    f"@ ₹{signal.stop_loss:.2f} (attempt {attempt})"
                )
                break
            logger.warning(f"[OrderManager] SL placement attempt {attempt} failed, retrying...")
            time.sleep(1)

        if not sl_placed:
            # CRITICAL: SL failed — must exit the position immediately
            logger.critical(
                f"[OrderManager] SL PLACEMENT FAILED after 3 attempts for {signal.symbol}. "
                f"EXITING POSITION to protect capital."
            )
            self._emergency_exit(broker, signal, fill_price, fill_qty)
            return

        # Bug 2 (part 2): persist the SL order ID so _update_broker_sl can cancel it later
        portfolio_tracker.update_sl_order_id(signal.symbol, sl_order_id)

        # ── Step 5: Send success alert ────────────────────────────
        self._send_alert(signal, sl_order_id, pending=False)

    def _execute_options(self, signal: Signal) -> None:
        """
        Place all legs for an options signal.

        Leg geometry is fully data-driven from options_meta["entry_legs"]:
            [{"symbol": "NSE:NIFTY..CE", "direction": "LONG"}, ...]

        No SL order is placed at the broker — position_manager monitors option
        premium LTP and issues a market-close order when stop/target is breached.
        """
        meta      = signal.options_meta or {}
        lot_size  = int(meta.get("lot_size", 1)) or 1
        lots      = max(1, signal.position_size // lot_size)
        qty       = lots * lot_size

        entry_legs = meta.get("entry_legs", [])
        if not entry_legs:
            logger.warning(
                f"[OrderManager] {signal.symbol}: options_meta has no entry_legs — "
                f"strategy must declare leg geometry. Signal dropped."
            )
            return

        # Paper trading — record position and subscribe monitor symbols; skip real orders.
        if PAPER_TRADING:
            portfolio_tracker.open_position(signal, fill_price=signal.entry)
            logger.info(
                f"[OrderManager] [PAPER/OPTIONS] {meta.get('strategy','')} "
                f"{signal.symbol} × {lots} lot(s)"
            )
            try:
                from data.fyers_stream import fyers_stream
                fyers_stream.subscribe_extra([leg["symbol"] for leg in entry_legs])
            except Exception:
                pass
            return

        broker      = self._get_broker(signal.symbol)
        placed_ids  = []
        opt_product = "CARRYFORWARD" if getattr(signal, "hold_type", "intraday") == "swing" else "INTRADAY"

        for leg in entry_legs:
            nfo_symbol = leg["symbol"]
            direction  = leg["direction"]
            oid = broker.place_order(
                symbol     = nfo_symbol,
                direction  = direction,
                qty        = qty,
                order_type = "MARKET",
                product    = opt_product,
            )
            if oid:
                placed_ids.append((nfo_symbol, direction, oid))
                logger.info(f"[OrderManager] OPTIONS leg placed: {direction} {qty} × {nfo_symbol} → {oid}")
            else:
                logger.error(f"[OrderManager] OPTIONS leg FAILED: {direction} {qty} × {nfo_symbol}")
                if placed_ids:
                    logger.critical(
                        f"[OrderManager] PARTIAL OPTIONS FILL on {signal.symbol} — "
                        f"rolling back {len(placed_ids)} placed leg(s) to prevent naked position."
                    )
                    for placed_sym, placed_dir, _ in placed_ids:
                        close_dir = "LONG" if placed_dir == "SHORT" else "SHORT"
                        close_id  = broker.place_order(
                            symbol=placed_sym, direction=close_dir,
                            qty=qty, order_type="MARKET",
                        )
                        if close_id:
                            logger.info(f"[OrderManager] Rolled back leg {placed_sym}: {close_id}")
                        else:
                            logger.critical(f"[OrderManager] ROLLBACK FAILED for {placed_sym} — MANUAL CLOSE REQUIRED")
                    self._send_alert(signal, "OPTIONS_PARTIAL_FILL", pending=False)
                return

        portfolio_tracker.open_position(signal, fill_price=signal.entry)
        try:
            from data.fyers_stream import fyers_stream
            fyers_stream.subscribe_extra([sym for sym, _, _ in placed_ids])
        except Exception:
            pass
        self._send_alert(signal, ",".join(oid for _, _, oid in placed_ids), pending=False)

    def _confirm_fill(
        self, broker, order_id: str, signal: Signal, max_wait: int = ORDER_POLL_MAX_WAIT
    ) -> tuple[Optional[float], int]:
        """
        Poll broker until order fills or times out.
        Returns (fill_price, fill_qty) or (None, 0) if failed.
        max_wait can be shortened for the post-cancel final check (Bug 4).
        """
        deadline = time.time() + max_wait

        while time.time() < deadline:
            try:
                orders = broker.get_orders()
                for order in orders:
                    oid = order.get("id") or order.get("orderId") or order.get("order_id", "")
                    # Bug 17: strip whitespace to avoid ID mismatch from broker formatting
                    if str(oid).strip() != str(order_id).strip():
                        continue

                    status = (
                        order.get("status") or
                        order.get("orderStatus") or
                        str(order.get("statuses", ""))
                    ).upper()

                    # Fyers status codes: 2=Filled, 5=Cancelled, 6=Rejected
                    if "FILL" in status or status == "2" or "TRADED" in status:
                        fill_price = float(
                            order.get("tradedPrice") or
                            order.get("avgFillPrice") or
                            order.get("filled_avg_price") or
                            signal.entry
                        )
                        # Bug 14: warn if fill_qty cannot be read from broker response
                        raw_qty = (
                            order.get("tradedQty") or
                            order.get("filledQty") or
                            order.get("filled_qty")
                        )
                        if raw_qty is None:
                            logger.warning(
                                f"[OrderManager] fill_qty missing in broker response for "
                                f"{order_id} — using signal size {signal.position_size}. "
                                f"Verify actual fill in Fyers app."
                            )
                        fill_qty = int(raw_qty) if raw_qty is not None else signal.position_size
                        return fill_price, fill_qty

                    if status in ("5", "6", "CANCELLED", "REJECTED", "EXPIRED"):
                        logger.error(
                            f"[OrderManager] Order {order_id} {status}: "
                            f"{order.get('message', '')}"
                        )
                        return None, 0

            except Exception as e:
                logger.debug(f"[OrderManager] Fill poll error: {e}")

            time.sleep(ORDER_POLL_INTERVAL)

        logger.warning(f"[OrderManager] Fill poll timed out for {order_id}")
        return None, 0

    def _emergency_exit(
        self, broker, signal: Signal, fill_price: float, qty: int
    ) -> None:
        """Emergency exit when SL placement fails."""
        exit_dir = "SHORT" if signal.direction == Direction.LONG else "LONG"
        exit_id  = broker.place_order(
            symbol     = signal.symbol,
            direction  = exit_dir,
            qty        = qty,
            order_type = "MARKET",
        )
        if exit_id:
            ltp = fill_price  # best estimate
            portfolio_tracker.close_position(signal.symbol, ltp, "SL_PLACEMENT_FAILED")
            logger.info(f"[OrderManager] Emergency exit placed: {exit_id}")
        else:
            logger.critical(
                f"[OrderManager] EMERGENCY EXIT ALSO FAILED for {signal.symbol}. "
                f"MANUAL INTERVENTION REQUIRED IMMEDIATELY."
            )

        try:
            from notifications.alert_service import alert_service
            alert_service.kill_switch(
                f"SL placement failed AND emergency exit attempted for {signal.symbol}. "
                f"Check Fyers app immediately."
            )
        except Exception:
            pass

    def _queue_for_confirmation(self, signal_id: str, signal: Signal) -> None:
        signal.expires_at = datetime.now(tz=IST) + timedelta(minutes=SIGNAL_EXPIRY_MINUTES)
        with self._lock:
            self._pending_signals[signal_id] = signal
        logger.info(f"[OrderManager] QUEUED: {signal.symbol} (id: {signal_id})")
        self._send_alert(signal, signal_id, pending=True)

    def _purge_expired_signals(self) -> None:
        now = datetime.now(tz=IST)
        with self._lock:
            expired = [
                sid for sid, sig in self._pending_signals.items()
                if sig.expires_at and now > sig.expires_at
            ]
            for sid in expired:
                sig = self._pending_signals.pop(sid)
                logger.info(f"[OrderManager] Signal expired: {sig.symbol}")

    def _get_broker(self, symbol: str):
        from execution.fyers_broker import fyers_broker
        from execution.alpaca_broker import alpaca_broker
        return fyers_broker if symbol.startswith("NSE:") or symbol.startswith("BSE:") \
               else alpaca_broker

    def _send_alert(self, signal: Signal, order_id: str, pending: bool = False) -> None:
        try:
            from notifications.alert_service import alert_service
            if pending:
                alert_service.signal_pending(signal, order_id)
            else:
                alert_service.trade_opened(signal, order_id)
        except Exception as e:
            logger.debug(f"Alert send failed: {e}")


# ── Module-level singleton ────────────────────────────────────────
order_manager = OrderManager()