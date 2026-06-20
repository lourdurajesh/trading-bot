"""
order_router.py
───────────────
SINGLE entry point for placing/cancelling broker orders.

Picks the BrokerAdapter by segment — Fyers for NSE / BSE / MCX, Alpaca for US — so the
broker-selection rule lives in ONE place and every engine (production order_manager,
MCX commodity_options, future US) places orders through the same call. fyers_broker
and alpaca_broker already implement the same place_order / cancel_order interface, so
they ARE the adapters; this just routes to them.

Real-money safety unchanged: each adapter still honours its own initialised / paper
state; this module only chooses the adapter and forwards the call.
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def get_broker(symbol: str):
    """Return the BrokerAdapter for a symbol's segment (the ONE selection rule)."""
    s = (symbol or "").upper()
    if s.startswith(("NSE:", "BSE:", "MCX:")):
        from execution.fyers_broker import fyers_broker
        return fyers_broker
    from execution.alpaca_broker import alpaca_broker
    return alpaca_broker


class OrderRouter:
    """One place to place and cancel orders. Mirrors the adapter place_order signature."""

    def place(self, symbol: str, direction: str, qty: int, order_type: str = "MARKET",
              price: float = 0, trigger: float = 0, product: Optional[str] = None) -> Optional[str]:
        broker = get_broker(symbol)
        if product is None:   # let the adapter apply its own default (Fyers INTRADAY / Alpaca day)
            return broker.place_order(symbol, direction, qty, order_type, price, trigger)
        return broker.place_order(symbol, direction, qty, order_type, price, trigger, product)

    def cancel(self, symbol: str, order_id: str):
        return get_broker(symbol).cancel_order(order_id)


order_router = OrderRouter()
