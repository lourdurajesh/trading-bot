"""
learning_watchlist.py
─────────────────────
Diverse symbol universe for simple learning paper trades.

Covers multiple asset classes and market segments so the strategies
get exposed to different volatility profiles, correlation structures,
and sector dynamics. Good for building intuition.

MCX commodity futures use the nearest active contract — computed at call
time via get_mcx_learning_symbols() using DB-backed rollover configuration
from commodity_options_learning.  No manual monthly updates required;
contracts roll automatically when the expiry date approaches.
"""

# ── NSE Large-cap equities ────────────────────────────────────────
# Diversified: banking, IT, pharma, energy, consumer, metals, telecom
LEARNING_NSE_EQUITIES = [
    # Banking (large-cap) — vetoed during Q4 results week; mid-caps below fill the gap
    "NSE:HDFCBANK-EQ",
    "NSE:ICICIBANK-EQ",
    "NSE:SBIN-EQ",
    "NSE:AXISBANK-EQ",
    # IT (large-cap)
    "NSE:TCS-EQ",
    "NSE:INFY-EQ",
    "NSE:WIPRO-EQ",
    "NSE:HCLTECH-EQ",
    # Consumer
    "NSE:HINDUNILVR-EQ",
    "NSE:NESTLEIND-EQ",
    "NSE:TITAN-EQ",
    # Energy
    "NSE:RELIANCE-EQ",
    "NSE:ONGC-EQ",
    "NSE:NTPC-EQ",
    # Pharma (large-cap)
    "NSE:SUNPHARMA-EQ",
    "NSE:DRREDDY-EQ",
    # Metals & Industrial
    "NSE:TATASTEEL-EQ",
    "NSE:HINDALCO-EQ",
    # Auto
    "NSE:MARUTI-EQ",
    "NSE:BAJAJ-AUTO-EQ",
    # Cement
    "NSE:ULTRACEMCO-EQ",
    # ── Mid-caps (F&O eligible) — staggered results, active when large-caps vetoed ──
    # IT mid-cap (results 2-3 weeks after TCS/Infy cluster)
    "NSE:PERSISTENT-EQ",
    "NSE:COFORGE-EQ",
    "NSE:LTTS-EQ",
    # NBFC / Finance (independent results calendar)
    "NSE:ABCAPITAL-EQ",
    "NSE:MUTHOOTFIN-EQ",
    # Pharma mid-cap
    "NSE:AUROPHARMA-EQ",
    "NSE:ALKEM-EQ",
    # Consumer & Retail
    "NSE:HAVELLS-EQ",
    "NSE:DMART-EQ",
    # Banking (small/mid — independent results window)
    "NSE:IDFCFIRSTB-EQ",
    "NSE:FEDERALBNK-EQ",
    # Digital / diversified
    "NSE:ZOMATO-EQ",
    "NSE:NAUKRI-EQ",
]

# ── NSE Indices ───────────────────────────────────────────────────
LEARNING_NSE_INDICES = [
    "NSE:NIFTY50-INDEX",
    "NSE:NIFTYBANK-INDEX",
    "NSE:FINNIFTY-INDEX",
]

# ── MCX commodity instrument names (logical, not contract codes) ──
# These are the short instrument names used as keys in the commodity_instruments
# DB table (MCX_CONTRACTS).  get_mcx_learning_symbols() converts each name to
# the current front-month Fyers symbol automatically — no manual updates needed.
#
# To add/remove instruments, edit the commodity_instruments table via the
# dashboard (/commodity/instruments API) instead of changing this list.
# This list is only used as a fallback when the DB module is unavailable.
_MCX_LEARNING_INSTRUMENTS = [
    # Precious metals
    "GOLD", "GOLDM", "GOLDGUINEA", "GOLDPETAL",
    "SILVER", "SILVERM", "SILVERMIC",
    # Energy
    "CRUDEOIL", "CRUDEOILM", "NATURALGAS", "NATGASMINI",
    # Base metals
    "COPPER", "COPPERM", "ZINC", "ZINCMINI",
    "ALUMINIUM", "ALUMINI", "LEAD", "LEADMINI",
    "NICKEL", "NICKELM",
    # Agricultural
    "COTTON", "MENTHAOIL",
]

_MCX_MONTHS = [
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
    "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
]


def get_mcx_learning_symbols() -> list[str]:
    """
    Return current front-month Fyers symbols for all MCX learning instruments.

    Delegates to commodity_options_learning._fyers_sym() which reads per-instrument
    rollover buffers and valid expiry months from the DB — always correct for today,
    rolls to the next contract automatically as the expiry date approaches.

    The instrument set is taken from MCX_CONTRACTS (DB-backed, manageable via the
    /commodity/instruments dashboard API) so adding a new instrument there
    automatically includes it in the learning universe without code changes.

    Falls back to _MCX_LEARNING_INSTRUMENTS with a simple 5-day buffer if the
    commodity module is not yet initialised (e.g. unit tests, early boot).
    """
    try:
        from commodity_options_learning import _fyers_sym, MCX_CONTRACTS
        return [_fyers_sym(name) for name in sorted(MCX_CONTRACTS)]
    except Exception:
        pass
    # Fallback: basic date math, no per-instrument rollover config from DB
    return _mcx_symbols_builtin(_MCX_LEARNING_INSTRUMENTS)


def _mcx_symbols_builtin(shorts: list[str]) -> list[str]:
    """
    Compute MCX Fyers symbols from date arithmetic alone.
    Used only when commodity_options_learning is not yet available.
    Applies a conservative 5-day rollover buffer with no per-instrument overrides.
    """
    import calendar as _cal
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime.now(tz=ZoneInfo("Asia/Kolkata"))
    year, month = now.year, now.month
    days_in_month = _cal.monthrange(year, month)[1]
    if now.day >= days_in_month - 5:   # conservative 5-day buffer
        month += 1
        if month > 12:
            month, year = 1, year + 1
    suffix = f"{str(year)[-2:]}{_MCX_MONTHS[month - 1]}FUT"
    return [f"MCX:{s}{suffix}" for s in shorts]
