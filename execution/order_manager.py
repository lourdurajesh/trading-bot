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

from config.settings import BOT_MODE, TOTAL_CAPITAL
import os
PAPER_TRADING = os.getenv("PAPER_TRADING", "false").lower() == "true"
from risk.portfolio_tracker import portfolio_tracker
from risk.risk_manager import risk_manager
from strategies.base_strategy import Direction, Signal

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
        """
        try:
            broker = self._get_broker(signal.symbol)
            funds  = broker.get_funds()
            if not funds:
                logger.warning("[OrderManager] Could not fetch funds — proceeding anyway")
                return True

            # Available cash / margin
            available = float(
                funds.get("availableBalance", 0)
                or funds.get("cash", 0)
                or funds.get("equity", 0)
                or TOTAL_CAPITAL
            )
            required = signal.entry * size * 0.25   # 25% margin estimate for equities

            if available < required:
                logger.warning(
                    f"[OrderManager] INSUFFICIENT MARGIN for {signal.symbol}: "
                    f"available ₹{available:,.0f} < required ₹{required:,.0f}"
                )
                # Try reducing size to fit available margin
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
        """
        # Paper trading mode — simulate execution
        if PAPER_TRADING:
            from paper_trading import paper_trading_engine
            order_id = paper_trading_engine.place_order(signal)
            if order_id:
                portfolio_tracker.open_position(signal, fill_price=signal.entry)
                logger.info(f"[OrderManager] [PAPER] Trade recorded: {signal.symbol}")
            return

        broker = self._get_broker(signal.symbol)

        # ── Step 1: Place entry order ─────────────────────────────
        logger.info(
            f"[OrderManager] EXECUTING {signal.direction.value} {signal.symbol} "
            f"× {signal.position_size} @ {signal.entry:.2f}"
        )

        entry_order_id = broker.place_order(
            symbol     = signal.symbol,
            direction  = signal.direction.value,
            qty        = signal.position_size,
            order_type = "MARKET",
            price      = signal.entry,
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
            self._send_alert(signal, "FILL_FAILED", pending=False)
            return

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

        # ── Step 5: Send success alert ────────────────────────────
        self._send_alert(signal, sl_order_id, pending=False)

    def _confirm_fill(
        self, broker, order_id: str, signal: Signal
    ) -> tuple[Optional[float], int]:
        """
        Poll broker until order fills or times out.
        Returns (fill_price, fill_qty) or (None, 0) if failed.
        """
        deadline = time.time() + ORDER_POLL_MAX_WAIT

        while time.time() < deadline:
            try:
                orders = broker.get_orders()
                for order in orders:
                    oid = order.get("id") or order.get("orderId") or order.get("order_id", "")
                    if str(oid) != str(order_id):
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
                        fill_qty = int(
                            order.get("tradedQty") or
                            order.get("filledQty") or
                            order.get("filled_qty") or
                            signal.position_size
                        )
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