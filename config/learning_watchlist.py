"""
learning_watchlist.py
─────────────────────
Diverse symbol universe for simple learning paper trades.

Covers NSE equities and indices so the equity strategies get exposed to
different volatility profiles, correlation structures, and sector dynamics.

MCX commodities are NOT included here — they trade exclusively as options
via commodity_options_learning.py (the dedicated MCX tab), never as direct
futures through the generic equity strategies.
"""

# ── NSE Large-cap equities ────────────────────────────────────────
# Diversified: banking, IT, pharma, energy, consumer, metals, telecom
LEARNING_NSE_EQUITIES = [
    # Banking (large-cap)
    "NSE:HDFCBANK-EQ",
    "NSE:ICICIBANK-EQ",
    "NSE:SBIN-EQ",
    "NSE:AXISBANK-EQ",
    "NSE:KOTAKBANK-EQ",
    # IT (large-cap)
    "NSE:TCS-EQ",
    "NSE:INFY-EQ",
    # Consumer
    "NSE:HINDUNILVR-EQ",
    "NSE:NESTLEIND-EQ",
    "NSE:TITAN-EQ",
    # Energy & Industrial
    "NSE:RELIANCE-EQ",
    "NSE:NTPC-EQ",
    "NSE:ADANIPORTS-EQ",
    # Auto
    "NSE:MARUTI-EQ",
    "NSE:TMCV-EQ",
    "NSE:TMPV-EQ",
    # Pharma
    "NSE:SUNPHARMA-EQ",
    "NSE:DRREDDY-EQ",
    # Metals
    "NSE:TATASTEEL-EQ",
    # Cement
    "NSE:ULTRACEMCO-EQ",
    # Finance (large-cap)
    "NSE:BAJFINANCE-EQ",
    "NSE:BAJAJFINSV-EQ",
    # Telecom
    "NSE:BHARTIARTL-EQ",
    # ── Mid-caps ─────────────────────────────────────────────────
    "NSE:PERSISTENT-EQ",
    "NSE:COFORGE-EQ",
    "NSE:LTTS-EQ",
    "NSE:MPHASIS-EQ",
    "NSE:DIVISLAB-EQ",
    "NSE:POLICYBZR-EQ",
    "NSE:ABCAPITAL-EQ",
    "NSE:MUTHOOTFIN-EQ",
    "NSE:AUROPHARMA-EQ",
    "NSE:ALKEM-EQ",
    "NSE:HAVELLS-EQ",
    "NSE:DMART-EQ",
    "NSE:IDFCFIRSTB-EQ",
    "NSE:FEDERALBNK-EQ",
    "NSE:ETERNAL-EQ",
    "NSE:NAUKRI-EQ",
]

# ── NSE Indices ───────────────────────────────────────────────────
LEARNING_NSE_INDICES = [
    "NSE:NIFTY50-INDEX",
    "NSE:NIFTYBANK-INDEX",
    "NSE:FINNIFTY-INDEX",
]
