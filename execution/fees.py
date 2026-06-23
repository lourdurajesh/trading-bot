"""
fees.py
───────
SINGLE source for transaction costs across EVERY engine (live, paper, learning,
MCX) and the dashboard P&L. A thin facade over analysis/cost_model.py — the
realistic, config-driven model (brokerage + STT/CTT + exchange + SEBI + stamp +
GST, rates in config/cost_rates.json).

Before this, fees were computed in several places with different models (flat ₹40
in learning equity, 0.03%/flat in paper_trading, cost_model in learning options).
Now every consumer calls these two functions, so a cost change lands in
config/cost_rates.json ONLY and Live/Paper/Learning can never diverge on fees.

Segment mapping (our internal labels → cost_rates.json keys):
    OPTIONS / nse_options          → NSE_OPT
    MCX / commodity                → MCX_OPT
    everything else (equity/fut)   → NSE_EQ_INTRADAY
"""
from analysis.cost_model import round_trip_cost


def _seg(segment: str) -> str:
    s = (segment or "").upper()
    if s in ("OPTIONS", "NSE_OPTIONS", "NSE_OPT", "OPTION"):
        return "NSE_OPT"
    if s in ("MCX", "MCX_OPT", "COMMODITY"):
        return "MCX_OPT"
    return "NSE_EQ_INTRADAY"


def round_trip(segment: str, entry: float, exit_price: float, qty: int,
               num_orders: int = 2) -> float:
    """Full entry+exit cost for `qty` units (shares, or lots×lot_size).

    num_orders = 2 for a single instrument round trip, 4 for a two-leg spread."""
    if not qty or qty <= 0:
        return 0.0
    return round_trip_cost(_seg(segment), num_orders,
                           buy_value=float(entry) * qty,
                           sell_value=float(exit_price) * qty)


def open_leg(segment: str, entry: float, qty: int, num_orders: int = 1) -> float:
    """Entry-side-only cost for an OPEN position — brokerage + stamp (buy) +
    exchange/SEBI on the buy turnover + GST. No STT/CTT (that is a sell-side
    cost, not yet incurred). Used for mark-to-market of open positions so the
    displayed unrealised P&L reflects only fees actually paid so far."""
    if not qty or qty <= 0:
        return 0.0
    return round_trip_cost(_seg(segment), num_orders,
                           buy_value=float(entry) * qty,
                           sell_value=0.0)


__all__ = ["round_trip", "open_leg"]
