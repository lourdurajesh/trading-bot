"""
test_sizing.py
──────────────
U4 regression guard: the shared execution/sizing.py primitives must be BYTE-IDENTICAL
to the four inline sizing implementations they replaced (NSE options risk gate ×2,
MCX _compute_lots, equity _calculate_size, institutional target_lots).

Each test re-implements the OLD arithmetic inline (the pre-U4 code, copied verbatim
from git) and asserts the new shared function returns the same result across a grid of
realistic premiums / capital / lot sizes / caps. If these pass, routing the engines
through the shared sizer changed no behaviour.

Run:  ./venv/bin/python -m pytest tests/test_sizing.py -q     (or: python tests/test_sizing.py)
"""
from execution.sizing import units_in_budget, lots_to_fit, lots_capped, shares_to_fit

# Grids covering the realistic + edge ranges.
PREMIUMS    = [0.0, 0.05, 2.5, 40.0, 120.0, 350.0, 1500.0, 9000.0]
LOT_SIZES   = [1, 15, 25, 50, 75, 100]
CAPITALS    = [100_000, 250_000, 500_000, 2_000_000]
RISK_PCTS   = [1.0, 1.5, 3.0]
CAP_PCTS    = [5.0, 10.0, 25.0, 50.0]
MAX_LOTS    = [1, 5, 10, 50]


def test_lots_to_fit_matches_old_options_risk():
    """Old risk/options_risk._calculate_lots inline arithmetic vs lots_to_fit."""
    for premium in PREMIUMS:
        for lot_size in LOT_SIZES:
            for capital in CAPITALS:
                for risk_pct in RISK_PCTS:
                    for cap_pct in CAP_PCTS:
                        for max_lot in MAX_LOTS:
                            cost_per_lot = premium * lot_size
                            # ---- OLD code (verbatim) ----
                            if cost_per_lot <= 0:
                                old = 0
                            else:
                                risk_budget = capital * (risk_pct / 100)
                                cap_budget  = capital * (cap_pct / 100)
                                max_by_risk = int(risk_budget / cost_per_lot)
                                max_by_cap  = int(cap_budget  / cost_per_lot)
                                lots = min(max_by_risk, max_by_cap, max_lot)
                                old = lots if lots > 0 else 0
                            # ---- NEW ----
                            new = lots_to_fit(
                                cost_per_lot,
                                capital * (risk_pct / 100),
                                capital * (cap_pct / 100),
                                max_lot,
                            )
                            new = new if new > 0 else 0
                            assert old == new, (premium, lot_size, capital, risk_pct, cap_pct, max_lot, old, new)


def test_lots_to_fit_matches_old_mcx():
    """Old commodity_options._compute_lots (real branch) inline vs lots_to_fit."""
    for premium in PREMIUMS:           # net_debit per unit
        for lot_size in LOT_SIZES:
            for available in CAPITALS:
                for max_cap_frac in (0.05, 0.10, 0.25):   # engine_settings.max_capital_pct() -> fraction
                    for lot_cap in MAX_LOTS:
                        for risk_inr in (5_000, 7_500, 15_000):
                            cost_per_lot = premium * lot_size
                            if cost_per_lot <= 0:
                                continue   # MCX returns 1 earlier on cost<=0; not this path
                            # ---- OLD code (verbatim) ----
                            max_lots    = int((available * max_cap_frac) / cost_per_lot)
                            max_by_risk = int(risk_inr / cost_per_lot)
                            old = min(max_lots, lot_cap, max_by_risk)
                            old = old if old >= 1 else 0
                            # ---- NEW ----
                            new = lots_to_fit(cost_per_lot, risk_inr, available * max_cap_frac, lot_cap)
                            new = new if new >= 1 else 0
                            assert old == new, (premium, lot_size, available, max_cap_frac, lot_cap, risk_inr, old, new)


def test_lots_capped_matches_old_institutional():
    """Old _calculate_institutional_lots inline vs lots_capped(floor=1)."""
    for premium in PREMIUMS:
        for lot_size in LOT_SIZES:
            for capital in CAPITALS:
                for fo_pct in (35.0, 50.0):
                    for requested in (1, 5, 33, 100):
                        cost_per_lot = premium * lot_size
                        if cost_per_lot <= 0:
                            old = 0
                        else:
                            max_cap         = capital * fo_pct / 100
                            max_lots_by_cap = int(max_cap / cost_per_lot)
                            lots = min(requested, max_lots_by_cap)
                            old  = max(1, lots)
                        new = lots_capped(requested, cost_per_lot, capital * fo_pct / 100, floor=1)
                        assert old == new, (premium, lot_size, capital, fo_pct, requested, old, new)


def test_units_in_budget_matches_old_institutional_target():
    """Old institutional_momentum target_lots inline vs units_in_budget."""
    for premium in PREMIUMS:
        for lot_size in LOT_SIZES:
            for capital in CAPITALS:
                for cap_pct in (35.0, 50.0):
                    cost_per_lot   = premium * lot_size
                    capital_budget = capital * cap_pct / 100
                    old = int(capital_budget / cost_per_lot) if cost_per_lot > 0 else 0
                    new = units_in_budget(capital_budget, cost_per_lot)
                    assert old == new, (premium, lot_size, capital, cap_pct, old, new)


def test_shares_to_fit_matches_old_equity():
    """Old risk_manager._calculate_size (equity) inline vs shares_to_fit."""
    entries = [10.0, 100.0, 750.0, 2500.0]
    for entry in entries:
        # stops from very tight (rejected) to wide
        for stop in [entry, entry * 0.9995, entry * 0.999, entry * 0.99, entry * 0.95, entry * 1.05]:
            for capital in CAPITALS:
                for risk_pct in RISK_PCTS:
                    risk_amount    = capital * (risk_pct / 100)
                    # ---- OLD code (verbatim) ----
                    risk_per_share = abs(entry - stop)
                    min_risk = entry * 0.001
                    if risk_per_share < min_risk:
                        old = (0, 0.0)
                    else:
                        shares = int(risk_amount / risk_per_share)
                        old = (shares, round(shares * risk_per_share, 2))
                    # ---- NEW ----
                    shares, risk, reason = shares_to_fit(entry, stop, risk_amount)
                    new = (0, 0.0) if reason else (shares, risk)
                    assert old == new, (entry, stop, capital, risk_pct, old, new)


if __name__ == "__main__":
    test_lots_to_fit_matches_old_options_risk()
    test_lots_to_fit_matches_old_mcx()
    test_lots_capped_matches_old_institutional()
    test_units_in_budget_matches_old_institutional_target()
    test_shares_to_fit_matches_old_equity()
    print("OK — all U4 sizing regression checks passed (new == old across the grid)")
