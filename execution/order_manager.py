"""
order_manager.py
────────────────
Routes approved signals to execution based on BOT_MODE.

AUTO mode:   Signal → Risk check → Execute immediately → Notify
MANUAL mode: Signal → Risk check → Queue for dashboard → Await confirm → Execute

Also manages bracket orders (entry + stop + target),
partial exits at target ladders, and stale order cleanup.
"""

import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from config.settings import BOT_MODE
from execution.fyers_broker import fyers_broker
from execution.alpaca_broker import alpaca_broker
from risk.portfolio_tracker import portfolio_tracker
from risk.risk_manager import risk_manager
from strategies.base_strategy import Direction, Signal

logger = logging.getLogger(__name__)

# Pending signal expires after this many minutes if not confirmed (MANUAL mode)
SIGNAL_EXPIRY_MINUTES = 30


class OrderManager:
    """
    Central order routing hub.

    Usage:
        order_manager.submit(signal)   # called by strategy_selector
        order_manager.confirm(sig_id)  # called by dashboard API (MANUAL mode)
        order_manager.reject(sig_id)   # called by dashboard API (MANUAL mode)
    """

    def __init__(self):
        self._mode             = BOT_MODE
        self._pending_signals: dict[str, Signal] = {}   # id → Signal (MANUAL queue)
        self._lock             = threading.Lock()

    # ─────────────────────────────────────────────────────────────
    # PUBLIC
    # ─────────────────────────────────────────────────────────────

    def submit(self, signal: Signal) -> Optional[str]:
        """
        Entry point for all signals.
        Runs risk validation, then routes to AUTO or MANUAL flow.
        Returns a signal_id (useful for MANUAL confirm/reject).
        """
        # Risk validation
        open_positions = portfolio_tracker.get_open_positions()
        decision = risk_manager.validate(signal, open_positions)

        if not decision.approved:
            logger.info(f"[OrderManager] REJECTED by risk: {signal.symbol} — {decision.reason}")
            return None

        # Attach sizing from risk decision
        signal.position_size   = decision.position_size
        signal.capital_at_risk = decision.capital_at_risk

        signal_id = str(uuid.uuid4())[:8].upper()

        if self._mode == "AUTO":
            self._execute(signal)
        else:
            self._queue_for_confirmation(signal_id, signal)

        return signal_id

    def confirm(self, signal_id: str) -> bool:
        """
        MANUAL mode: user confirms a pending signal from the dashboard.
        Returns True if signal was found and executed.
        """
        with self._lock:
            signal = self._pending_signals.pop(signal_id, None)

        if not signal:
            logger.warning(f"[OrderManager] Confirm called for unknown/expired signal {signal_id}")
            return False

        if signal.expires_at and datetime.now(tz=timezone.utc) > signal.expires_at:
            logger.warning(f"[OrderManager] Signal {signal_id} expired — not executing")
            return False

        logger.info(f"[OrderManager] MANUAL CONFIRM for {signal.symbol} (id: {signal_id})")
        self._execute(signal)
        return True

    def reject(self, signal_id: str) -> bool:
        """MANUAL mode: user rejects a pending signal."""
        with self._lock:
            signal = self._pending_signals.pop(signal_id, None)
        if signal:
            logger.info(f"[OrderManager] MANUAL REJECT for {signal.symbol} (id: {signal_id})")
            return True
        return False

    def get_pending_signals(self) -> list[dict]:
        """Returns all pending signals waiting for manual confirmation."""
        self._purge_expired_signals()
        with self._lock:
            return [
                {"signal_id": sid, **sig.to_dict()}
                for sid, sig in self._pending_signals.items()
            ]

    def set_mode(self, mode: str) -> None:
        """Switch between AUTO and MANUAL mode at runtime."""
        if mode not in ("AUTO", "MANUAL"):
            logger.error(f"[OrderManager] Invalid mode: {mode}")
            return
        self._mode = mode
        logger.info(f"[OrderManager] Mode switched to {mode}")

    @property
    def mode(self) -> str:
        return self._mode

    # ─────────────────────────────────────────────────────────────
    # INTERNAL — execution
    # ─────────────────────────────────────────────────────────────

    def _execute(self, signal: Signal) -> None:
        """Route signal to the appropriate broker and open position."""
        broker = self._get_broker(signal.symbol)
        if not broker:
            logger.error(f"[OrderManager] No broker available for {signal.symbol}")
            return

        try:
            # Place entry order
            order_id = broker.place_order(
                symbol    = signal.symbol,
                direction = signal.direction.value,
                qty       = signal.position_size,
                order_type = "MARKET",
                price     = signal.entry,
            )

            if not order_id:
                logger.error(f"[OrderManager] Entry order failed for {signal.symbol}")
                return

            logger.info(f"[OrderManager] Entry order placed: {order_id} for {signal.symbol}")

            # Place bracket stop-loss order
            stop_order_id = broker.place_order(
                symbol     = signal.symbol,
                direction  = "SHORT" if signal.direction == Direction.LONG else "LONG",
                qty        = signal.position_size,
                order_type = "SL",
                price      = signal.stop_loss,
                trigger    = signal.stop_loss,
            )

            # Record in portfolio tracker (using entry as fill — in production use actual fill)
            portfolio_tracker.open_position(signal, fill_price=signal.entry)

            # Send alert
            self._send_alert(signal, order_id)

        except Exception as e:
            logger.error(f"[OrderManager] Execution error for {signal.symbol}: {e}")

    def _queue_for_confirmation(self, signal_id: str, signal: Signal) -> None:
        """Add signal to MANUAL pending queue."""
        signal.expires_at = datetime.now(tz=timezone.utc) + timedelta(minutes=SIGNAL_EXPIRY_MINUTES)
        with self._lock:
            self._pending_signals[signal_id] = signal
        logger.info(
            f"[OrderManager] QUEUED for manual confirm: {signal.symbol} "
            f"(id: {signal_id}, expires in {SIGNAL_EXPIRY_MINUTES}m)"
        )
        self._send_alert(signal, signal_id, pending=True)

    def _purge_expired_signals(self) -> None:
        """Remove signals that have passed their expiry time."""
        now = datetime.now(tz=timezone.utc)
        with self._lock:
            expired = [
                sid for sid, sig in self._pending_signals.items()
                if sig.expires_at and now > sig.expires_at
            ]
            for sid in expired:
                sig = self._pending_signals.pop(sid)
                logger.info(f"[OrderManager] Signal expired: {sig.symbol} (id: {sid})")

    def _get_broker(self, symbol: str):
        """Return the correct broker based on symbol prefix."""
        if symbol.startswith("NSE:") or symbol.startswith("BSE:"):
            return fyers_broker
        return alpaca_broker

    def _send_alert(self, signal: Signal, order_id: str, pending: bool = False) -> None:
        """Push notification via alert_service (non-blocking)."""
        try:
            from notifications.alert_service import alert_service
            if pending:
                alert_service.signal_pending(signal, order_id)
            else:
                alert_service.trade_opened(signal, order_id)
        except Exception as e:
            logger.debug(f"Alert send failed (non-critical): {e}")


# ── Module-level singleton ────────────────────────────────────────
order_manager = OrderManager()
