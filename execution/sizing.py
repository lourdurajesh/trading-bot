"""
sizing.py
─────────
SINGLE SOURCE for the position-SIZE decision — how many lots (options/futures) or
shares (equity) to trade given a per-unit cost, a capital base, and risk/exposure
budgets. One place so a change to "how big do we trade" lands once and applies to
every engine (NSE options, MCX, institutional, equity) instead of being copied per
engine — the same shape as execution/exit_rules.py and execution/order_router.py.

Owns only the size-to-fit ARITHMETIC (the DECISION). Each engine keeps its ADAPTER:
which capital base it sizes against (TOTAL_CAPITAL vs free funds), which config %s it
uses, where premium / lot_size come from, and what it does with 0 lots. Those differ
by asset class, not by rule, so they stay as PARAMETERS (risk_budget, cap_budget,
max_units) at the call site — never forked here.

Before this module the floor-division `int(budget / cost_per_lot)` was re-implemented
in four places (NSE options risk gate ×2, MCX, institutional momentum); a fix to the
sizing rule had to be made four times or the engines silently diverged. Now there is
one place.

Consumers (U4):
  • risk/options_risk._calculate_lots               — NSE index options       ✅
  • risk/options_risk._calculate_institutional_lots — institutional conviction ✅
  • commodity_options._compute_lots                 — MCX options              ✅
  • risk/risk_manager._calculate_size  (equity)     — NSE equity / futures     ✅
  • strategies/institutional_momentum               — requested-lot pre-sizing ✅
Debt: learning_engine (NSE) and us_reversal are fixed 1 lot / 1 contract today — they
route through here the moment they size for real (mirrors the U3/U5 remaining debt).
"""
from typing import Optional


def units_in_budget(budget: float, cost_per_unit: float) -> int:
    """Floor count of whole units (lots/contracts) whose total cost fits in `budget`.

    The atomic size primitive every sizing rule is built from. Returns 0 for a
    non-positive cost (caller rejects). Never rounds up — a partial lot is no lot."""
    if cost_per_unit is None or cost_per_unit <= 0:
        return 0
    return int(budget / cost_per_unit)


def lots_to_fit(cost_per_unit: float, risk_budget: float, cap_budget: float,
                max_units: int) -> int:
    """Size-to-fit a lot-based (options / futures) position — the B2.1 rule.

    Take the most lots whose total debit (cost_per_unit × lots) fits inside BOTH the
    per-trade risk budget AND the exposure-cap budget, hard-capped at `max_units` lots.

      cost_per_unit : premium × lot_size — ₹/$ to buy ONE lot.
      risk_budget   : ₹ max acceptable loss on this trade (for a debit, premium IS the
                      max loss, so risk_budget and cap_budget may coincide).
      cap_budget    : ₹ max capital this trade may deploy (exposure cap).
      max_units     : hard lot cap (operator ceiling, independent of capital).

    Returns 0 when even one lot won't fit — the caller REJECTS the trade. We never
    force a minimum of 1: forcing 1 lot when the budget says 0 can deploy many times
    the intended capital (the bug guarded against in options_risk).
    """
    if cost_per_unit is None or cost_per_unit <= 0:
        return 0
    by_risk = units_in_budget(risk_budget, cost_per_unit)
    by_cap  = units_in_budget(cap_budget,  cost_per_unit)
    return max(0, min(by_risk, by_cap, int(max_units)))


def lots_capped(requested: int, cost_per_unit: float, cap_budget: float,
                *, floor: int = 1) -> int:
    """Cap a strategy-REQUESTED lot count by an exposure budget, with a minimum floor.

    Used by conviction sizing (institutional momentum), where the strategy has already
    chosen how many lots it WANTS to deploy and we only clamp it to the hard capital
    cap — unlike `lots_to_fit`, an approved conviction trade keeps at least `floor`
    lots. Returns 0 only when the per-unit cost is non-positive (nothing to size).
    """
    if cost_per_unit is None or cost_per_unit <= 0:
        return 0
    by_cap = units_in_budget(cap_budget, cost_per_unit)
    return max(floor, min(int(requested), by_cap))


def shares_to_fit(entry: float, stop: float, risk_budget: float,
                  *, min_risk_frac: float = 0.001) -> tuple[int, float, str]:
    """Size-to-fit a share-based (equity / futures) position by fixed-fractional risk.

    shares = risk_budget / risk_per_share, where risk_per_share = |entry − stop|.

    Rejects (0 shares) when the stop is closer than `min_risk_frac` of entry: a stop
    that tight means the stop is practically at entry — a strategy error, not a signal —
    and would otherwise size to an enormous share count (a ₹0.01 stop on a ₹100 stock
    = 750k shares on ₹500k capital).

    Returns (shares, capital_at_risk, reason). reason is "" on success, else a
    human-readable rejection string the caller can log.
    """
    risk_per_share = abs(entry - stop)
    min_risk = entry * min_risk_frac
    if risk_per_share < min_risk:
        return 0, 0.0, (f"stop loss too tight (risk ₹{risk_per_share:.4f} "
                        f"< min ₹{min_risk:.4f})")
    shares = int(risk_budget / risk_per_share)
    return shares, round(shares * risk_per_share, 2), ""


__all__ = ["units_in_budget", "lots_to_fit", "lots_capped", "shares_to_fit"]
