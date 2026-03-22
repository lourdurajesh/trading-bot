"""
fyers_broker.py
───────────────
Thin wrapper around the Fyers REST API for order execution.
Maps the bot's internal order format to Fyers API schema.

Supports: NSE equities + NFO options (future: BSE)
"""

import logging
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)


class FyersBroker:
    """
    Wraps Fyers REST API for order placement, modification and cancellation.

    All methods return order_id string on success, None on failure.
    """

    def __init__(self):
        self._client = None
        self._initialised = False

    def initialise(self) -> bool:
        """Connect to Fyers REST API. Call once on bot startup."""
        if not settings.FYERS_ACCESS_TOKEN:
            logger.warning("[FyersBroker] No access token — running in simulation mode.")
            return False
        try:
            from fyers_apiv3 import fyersModel
            self._client = fyersModel.FyersModel(
                client_id = settings.FYERS_APP_ID,
                token     = settings.FYERS_ACCESS_TOKEN,
                log_path  = "logs/",
                is_async  = False,
            )
            self._initialised = True
            logger.info("[FyersBroker] Initialised successfully.")
            return True
        except Exception as e:
            logger.error(f"[FyersBroker] Init failed: {e}")
            return False

    # ─────────────────────────────────────────────────────────────
    # ORDER OPERATIONS
    # ─────────────────────────────────────────────────────────────

    def place_order(
        self,
        symbol:     str,
        direction:  str,          # "LONG" or "SHORT"
        qty:        int,
        order_type: str = "MARKET",  # "MARKET" | "LIMIT" | "SL" | "SL-M"
        price:      float = 0,
        trigger:    float = 0,
        product:    str = "INTRADAY",  # "INTRADAY" | "CNC" (delivery)
    ) -> Optional[str]:
        """Place an order. Returns order_id or None."""

        if not self._initialised:
            logger.warning(f"[FyersBroker] SIMULATION place_order: {direction} {qty} {symbol} @ {price}")
            return f"SIM-{symbol[:6]}-{qty}"

        side = 1 if direction == "LONG" else -1

        order_data = {
            "symbol":       symbol,
            "qty":          qty,
            "type":         self._map_order_type(order_type),
            "side":         side,
            "productType":  product,
            "limitPrice":   price if order_type == "LIMIT" else 0,
            "stopPrice":    trigger if order_type in ("SL", "SL-M") else 0,
            "validity":     "DAY",
            "disclosedQty": 0,
            "offlineOrder": False,
        }

        try:
            response = self._client.place_order(data=order_data)
            if response.get("s") == "ok":
                order_id = response.get("id", "")
                logger.info(f"[FyersBroker] Order placed: {order_id} — {direction} {qty} {symbol}")
                return order_id
            else:
                logger.error(f"[FyersBroker] Order failed: {response.get('message')}")
                return None
        except Exception as e:
            logger.error(f"[FyersBroker] place_order exception: {e}")
            return None

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a pending order. Returns True on success."""
        if not self._initialised:
            logger.warning(f"[FyersBroker] SIMULATION cancel_order: {order_id}")
            return True
        try:
            response = self._client.cancel_order(data={"id": order_id})
            return response.get("s") == "ok"
        except Exception as e:
            logger.error(f"[FyersBroker] cancel_order exception: {e}")
            return False

    def modify_order(self, order_id: str, price: float = 0, qty: int = 0) -> bool:
        """Modify a pending order price or quantity."""
        if not self._initialised:
            return True
        try:
            data = {"id": order_id}
            if price: data["limitPrice"] = price
            if qty:   data["qty"] = qty
            response = self._client.modify_order(data=data)
            return response.get("s") == "ok"
        except Exception as e:
            logger.error(f"[FyersBroker] modify_order exception: {e}")
            return False

    def get_positions(self) -> list[dict]:
        """Fetch all open positions from Fyers."""
        if not self._initialised:
            return []
        try:
            response = self._client.positions()
            if response.get("s") == "ok":
                return response.get("netPositions", [])
            return []
        except Exception as e:
            logger.error(f"[FyersBroker] get_positions exception: {e}")
            return []

    def get_orders(self) -> list[dict]:
        """Fetch today's orders."""
        if not self._initialised:
            return []
        try:
            response = self._client.orderbook()
            if response.get("s") == "ok":
                return response.get("orderBook", [])
            return []
        except Exception as e:
            logger.error(f"[FyersBroker] get_orders exception: {e}")
            return []

    def get_funds(self) -> dict:
        """Get available margin/funds."""
        if not self._initialised:
            return {}
        try:
            response = self._client.funds()
            if response.get("s") == "ok":
                return response.get("fund_limit", [{}])[0]
            return {}
        except Exception as e:
            logger.error(f"[FyersBroker] get_funds exception: {e}")
            return {}

    # ─────────────────────────────────────────────────────────────
    # HELPERS
    # ─────────────────────────────────────────────────────────────

    @staticmethod
    def _map_order_type(order_type: str) -> int:
        """Map internal order type string to Fyers numeric type."""
        return {"LIMIT": 1, "MARKET": 2, "SL": 3, "SL-M": 4}.get(order_type, 2)


# ── Module-level singleton ────────────────────────────────────────
fyers_broker = FyersBroker()
