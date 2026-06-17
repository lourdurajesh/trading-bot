"""
cost_model.py
─────────────
Realistic, configurable transaction-cost model for Indian F&O (B2.2).

Replaces the old flat ₹40 round-trip assumption. Computes brokerage + STT/CTT +
exchange + SEBI + stamp + GST per round trip, with rates loaded from
config/cost_rates.json (editable; no hard-coded numbers).

Used to report **net-of-cost** P&L so the ₹1,000/day expectancy math reflects
what actually lands in the account.

Usage:
    from analysis.cost_model import round_trip_cost
    fees = round_trip_cost("NSE_OPT", num_orders=2, buy_value=4912, sell_value=7368)
    # single long option: buy 1 order + sell 1 order; values are premium × qty per side
"""
import json
import logging
import os

logger = logging.getLogger(__name__)

_RATES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "config", "cost_rates.json"
)


def _load_rates() -> dict:
    try:
        with open(_RATES_PATH, "r") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"[CostModel] could not load {_RATES_PATH}: {e} — using zero costs")
        return {}


_RATES = _load_rates()


def round_trip_cost(
    segment: str,
    num_orders: int,
    buy_value: float,
    sell_value: float,
) -> float:
    """
    Estimate total round-trip transaction cost (INR).

      segment    : key in cost_rates.json — 'NSE_OPT' | 'MCX_OPT' | 'NSE_EQ_INTRADAY'
      num_orders : total order count across the round trip
                   (single option = 2; a 2-leg spread = 4)
      buy_value  : total premium PAID across all buy orders (premium × qty), entry + exit
      sell_value : total premium RECEIVED across all sell orders (premium × qty), entry + exit

    STT/CTT applies to the sell side; stamp duty to the buy side; exchange + SEBI to
    total turnover; GST to (brokerage + exchange + SEBI). Returns 0.0 if rates missing.
    """
    r = _RATES.get(segment)
    if not r:
        return 0.0
    buy_value  = max(0.0, float(buy_value))
    sell_value = max(0.0, float(sell_value))
    turnover   = buy_value + sell_value

    brokerage = r.get("brokerage_per_order", 0.0) * max(0, int(num_orders))
    txn       = r.get("exchange_txn_pct", 0.0) * turnover
    sebi      = r.get("sebi_pct", 0.0) * turnover
    # STT (equity/index options) or CTT (commodity options) — sell side only.
    stt_ctt   = (r.get("stt_pct_sell", r.get("ctt_pct_sell", 0.0))) * sell_value
    stamp     = r.get("stamp_pct_buy", 0.0) * buy_value
    gst       = r.get("gst_pct", 0.0) * (brokerage + txn + sebi)

    return round(brokerage + txn + sebi + stt_ctt + stamp + gst, 2)


def single_option_cost(segment: str, entry_premium: float, exit_premium: float, qty: int) -> float:
    """Convenience for a single long option: buy at entry, sell at exit (2 orders)."""
    return round_trip_cost(
        segment, num_orders=2,
        buy_value=entry_premium * qty,
        sell_value=exit_premium * qty,
    )


def debit_spread_cost(
    segment: str,
    entry_long_prem: float, entry_short_prem: float,
    exit_long_prem: float,  exit_short_prem: float,
    qty: int,
) -> float:
    """
    Debit spread round trip (4 orders): buy long+ sell short at entry,
    sell long + buy short at exit.
      buy side  = entry long premium + exit short premium
      sell side = entry short premium + exit long premium
    """
    buy_value  = (entry_long_prem + exit_short_prem) * qty
    sell_value = (entry_short_prem + exit_long_prem) * qty
    return round_trip_cost(segment, num_orders=4, buy_value=buy_value, sell_value=sell_value)
