"""
alpaca_broker.py
────────────────
Thin wrapper around the Alpaca Trade API for US equity execution.
Supports paper trading mode (sandbox) by default.
"""

import logging
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)


class AlpacaBroker:
    """
    Wraps Alpaca REST API for order placement, modification and cancellation.
    Defaults to paper trading until ALPACA_PAPER=false in .env.
    """

    def __init__(self):
        self._client = None
        self._initialised = False

    def initialise(self) -> bool:
        """Connect to Alpaca REST API. Call once on bot startup."""
        if not settings.ALPACA_API_KEY:
            logger.warning("[AlpacaBroker] No API key — running in simulation mode.")
            return False
        try:
            import alpaca_trade_api as alpaca
            self._client = alpaca.REST(
                key_id     = settings.ALPACA_API_KEY,
                secret_key = settings.ALPACA_SECRET_KEY,
                base_url   = settings.ALPACA_BASE_URL,
            )
            account = self._client.get_account()
            mode = "PAPER" if settings.ALPACA_PAPER else "LIVE"
            logger.info(
                f"[AlpacaBroker] Initialised ({mode}). "
                f"Equity: ${float(account.equity):,.2f}"
            )
            self._initialised = True
            return True
        except Exception as e:
            logger.error(f"[AlpacaBroker] Init failed: {e}")
            return False

    # ─────────────────────────────────────────────────────────────
    # ORDER OPERATIONS
    # ─────────────────────────────────────────────────────────────

    def place_order(
        self,
        symbol:     str,
        direction:  str,           # "LONG" or "SHORT"
        qty:        int,
        order_type: str = "MARKET",
        price:      float = 0,
        trigger:    float = 0,
        product:    str = "day",   # "day" | "gtc" | "ioc"
    ) -> Optional[str]:
        """Place an order. Returns order_id or None."""

        if not self._initialised:
            logger.warning(f"[AlpacaBroker] SIMULATION place_order: {direction} {qty} {symbol} @ {price}")
            return f"SIM-{symbol[:6]}-{qty}"

        side       = "buy" if direction == "LONG" else "sell"
        order_type_mapped = self._map_order_type(order_type)

        try:
            kwargs = {
                "symbol":        symbol,
                "qty":           qty,
                "side":          side,
                "type":          order_type_mapped,
                "time_in_force": product,
            }
            if order_type == "LIMIT":
                kwargs["limit_price"] = str(price)
            if order_type in ("SL", "stop"):
                kwargs["stop_price"] = str(trigger or price)

            order = self._client.submit_order(**kwargs)
            logger.info(f"[AlpacaBroker] Order placed: {order.id} — {side} {qty} {symbol}")
            return order.id
        except Exception as e:
            logger.error(f"[AlpacaBroker] place_order exception: {e}")
            return None

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a pending order."""
        if not self._initialised:
            return True
        try:
            self._client.cancel_order(order_id)
            return True
        except Exception as e:
            logger.error(f"[AlpacaBroker] cancel_order exception: {e}")
            return False

    def get_positions(self) -> list[dict]:
        """Fetch all open positions from Alpaca."""
        if not self._initialised:
            return []
        try:
            positions = self._client.list_positions()
            return [
                {
                    "symbol":    p.symbol,
                    "qty":       int(p.qty),
                    "side":      p.side,
                    "avg_entry": float(p.avg_entry_price),
                    "market_val": float(p.market_value),
                    "unrealised_pnl": float(p.unrealized_pl),
                }
                for p in positions
            ]
        except Exception as e:
            logger.error(f"[AlpacaBroker] get_positions exception: {e}")
            return []

    def get_account(self) -> dict:
        """Get account details including buying power."""
        if not self._initialised:
            return {}
        try:
            acc = self._client.get_account()
            return {
                "equity":       float(acc.equity),
                "cash":         float(acc.cash),
                "buying_power": float(acc.buying_power),
                "status":       acc.status,
            }
        except Exception as e:
            logger.error(f"[AlpacaBroker] get_account exception: {e}")
            return {}

    @staticmethod
    def _map_order_type(order_type: str) -> str:
        return {
            "MARKET": "market",
            "LIMIT":  "limit",
            "SL":     "stop",
            "SL-M":   "stop",
        }.get(order_type, "market")


# ── Module-level singleton ────────────────────────────────────────
alpaca_broker = AlpacaBroker()
