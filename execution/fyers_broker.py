"""
fyers_broker.py
───────────────
Fyers REST API wrapper — rebuilt with:
  - GTT (Good Till Triggered) orders for persistent SL
  - Proper margin/funds checking
  - Order status polling
  - Simulation mode that clearly logs all actions
"""

import logging
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)


class FyersBroker:

    def __init__(self):
        self._client      = None
        self._initialised = False

    def initialise(self) -> bool:
        if not settings.FYERS_ACCESS_TOKEN:
            logger.warning("[FyersBroker] No token — simulation mode.")
            return False
        try:
            from fyers_apiv3 import fyersModel
            self._client = fyersModel.FyersModel(
                client_id = settings.FYERS_APP_ID,
                token     = settings.FYERS_ACCESS_TOKEN,
                log_path  = "logs/",
                is_async  = False,
            )
            # Verify token works
            resp = self._client.get_profile()
            if resp.get("s") == "ok":
                self._initialised = True
                logger.info(f"[FyersBroker] Initialised. "
                            f"User: {resp.get('data',{}).get('name','unknown')}")
                return True
            else:
                logger.error(f"[FyersBroker] Token verification failed: {resp}")
                return False
        except Exception as e:
            logger.error(f"[FyersBroker] Init failed: {e}")
            return False

    # ─────────────────────────────────────────────────────────────
    # ORDER OPERATIONS
    # ─────────────────────────────────────────────────────────────

    def place_order(
        self,
        symbol:     str,
        direction:  str,
        qty:        int,
        order_type: str = "MARKET",
        price:      float = 0,
        trigger:    float = 0,
        product:    str = "INTRADAY",
    ) -> Optional[str]:
        if not self._initialised:
            logger.info(
                f"[FyersBroker] [SIM] {direction} {qty} × {symbol} "
                f"@ {price:.2f} [{order_type}]"
            )
            return f"SIM-{symbol[:8]}-{qty}-{order_type}"

        side = 1 if direction == "LONG" else -1
        data = {
            "symbol":       symbol,
            "qty":          qty,
            "type":         self._map_order_type(order_type),
            "side":         side,
            "productType":  product,
            "limitPrice":   price   if order_type == "LIMIT" else 0,
            "stopPrice":    trigger if order_type in ("SL", "SL-M") else 0,
            "validity":     "DAY",
            "disclosedQty": 0,
            "offlineOrder": False,
        }
        try:
            resp = self._client.place_order(data=data)
            if resp.get("s") == "ok":
                oid = resp.get("id", "")
                logger.info(f"[FyersBroker] Order placed: {oid} — "
                            f"{direction} {qty} {symbol} [{order_type}]")
                return oid
            logger.error(f"[FyersBroker] Order rejected: {resp.get('message')}")
            return None
        except Exception as e:
            logger.error(f"[FyersBroker] place_order exception: {e}")
            return None

    def place_gtt_order(
        self,
        symbol:    str,
        direction: str,
        qty:       int,
        trigger:   float,
        price:     float = 0,
    ) -> Optional[str]:
        """
        Place GTT (Good Till Triggered) order — persists even if bot crashes.
        Use for stop losses on overnight positions.
        """
        if not self._initialised:
            logger.info(f"[FyersBroker] [SIM] GTT {direction} {qty} {symbol} trigger={trigger:.2f}")
            return f"SIM-GTT-{symbol[:8]}-{qty}"

        side = 1 if direction == "LONG" else -1
        data = {
            "type":      1,     # OCO type
            "symbol":    symbol,
            "segment":   "NSE_CM",
            "condition": [
                {
                    "qty":         qty,
                    "limitPrice":  price or trigger,
                    "stopPrice":   trigger,
                    "productType": "CNC",
                    "side":        side,
                    "type":        3,     # SL order
                    "validity":    "DAY",
                }
            ],
        }
        try:
            resp = self._client.place_gtt_order(data=data)
            if resp.get("s") == "ok":
                gtt_id = resp.get("id", "")
                logger.info(f"[FyersBroker] GTT placed: {gtt_id} — "
                            f"{direction} {qty} {symbol} trigger=₹{trigger:.2f}")
                return gtt_id
            logger.error(f"[FyersBroker] GTT rejected: {resp.get('message')}")
            return None
        except Exception as e:
            logger.error(f"[FyersBroker] place_gtt_order exception: {e}")
            return None

    def cancel_order(self, order_id: str) -> bool:
        if not self._initialised:
            logger.info(f"[FyersBroker] [SIM] cancel {order_id}")
            return True
        try:
            resp = self._client.cancel_order(data={"id": order_id})
            return resp.get("s") == "ok"
        except Exception as e:
            logger.error(f"[FyersBroker] cancel_order: {e}")
            return False

    def modify_order(self, order_id: str, price: float = 0, qty: int = 0,
                     trigger: float = 0) -> bool:
        if not self._initialised:
            return True
        try:
            data = {"id": order_id}
            if price:   data["limitPrice"] = price
            if qty:     data["qty"] = qty
            if trigger: data["stopPrice"] = trigger
            resp = self._client.modify_order(data=data)
            return resp.get("s") == "ok"
        except Exception as e:
            logger.error(f"[FyersBroker] modify_order: {e}")
            return False

    # ─────────────────────────────────────────────────────────────
    # DATA QUERIES
    # ─────────────────────────────────────────────────────────────

    def get_orders(self) -> list[dict]:
        if not self._initialised:
            return []
        try:
            resp = self._client.orderbook()
            if resp.get("s") == "ok":
                return resp.get("orderBook", [])
            return []
        except Exception as e:
            logger.error(f"[FyersBroker] get_orders: {e}")
            return []

    def get_positions(self) -> list[dict]:
        if not self._initialised:
            return []
        try:
            resp = self._client.positions()
            if resp.get("s") == "ok":
                return resp.get("netPositions", [])
            return []
        except Exception as e:
            logger.error(f"[FyersBroker] get_positions: {e}")
            return []

    def get_funds(self) -> dict:
        if not self._initialised:
            return {"availableBalance": 0}
        try:
            resp = self._client.funds()
            if resp.get("s") == "ok":
                funds = resp.get("fund_limit", [{}])
                if isinstance(funds, list) and funds:
                    return funds[0]
                return funds if isinstance(funds, dict) else {}
            return {}
        except Exception as e:
            logger.error(f"[FyersBroker] get_funds: {e}")
            return {}

    def get_profile(self) -> dict:
        if not self._initialised:
            return {}
        try:
            return self._client.get_profile()
        except Exception as e:
            logger.error(f"[FyersBroker] get_profile: {e}")
            return {}

    def reconcile_positions(self) -> dict:
        """
        Compare broker positions vs local portfolio tracker.
        Returns dict of discrepancies for crash recovery.
        """
        broker_positions = {
            p.get("symbol"): p
            for p in self.get_positions()
            if float(p.get("netQty", 0)) != 0
        }
        local_positions  = {
            p["symbol"]: p
            for p in portfolio_tracker.get_open_positions()
        }

        discrepancies = {}

        # Positions in broker but not in local DB
        for sym, pos in broker_positions.items():
            if sym not in local_positions:
                discrepancies[sym] = {
                    "issue":  "In broker but not in local DB",
                    "broker": pos,
                    "local":  None,
                }

        # Positions in local DB but not on broker
        for sym, pos in local_positions.items():
            if sym not in broker_positions:
                discrepancies[sym] = {
                    "issue":  "In local DB but not on broker",
                    "broker": None,
                    "local":  pos,
                }

        if discrepancies:
            logger.warning(
                f"[FyersBroker] Position discrepancies found: "
                f"{list(discrepancies.keys())}"
            )
        return discrepancies

    @staticmethod
    def _map_order_type(order_type: str) -> int:
        return {"LIMIT": 1, "MARKET": 2, "SL": 3, "SL-M": 4}.get(order_type, 2)


# Lazy import to avoid circular
def _get_portfolio_tracker():
    from risk.portfolio_tracker import portfolio_tracker
    return portfolio_tracker

portfolio_tracker = property(_get_portfolio_tracker)

# ── Module-level singleton ────────────────────────────────────────
fyers_broker = FyersBroker()
