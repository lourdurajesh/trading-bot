"""
learning_watchlist.py
─────────────────────
Diverse symbol universe for simple learning paper trades.

Covers multiple asset classes and market segments so the strategies
get exposed to different volatility profiles, correlation structures,
and sector dynamics. Good for building intuition.

MCX commodity futures use the nearest active contract — these roll
monthly so they may need updating. Verified format: MCX:GOLD25JUNFUT
(month code = first 3 letters of month + 2-digit year + FUT).
"""

# ── NSE Large-cap equities ────────────────────────────────────────
# Diversified: banking, IT, pharma, energy, consumer, metals, telecom
LEARNING_NSE_EQUITIES = [
    # Banking
    "NSE:HDFCBANK-EQ",
    "NSE:ICICIBANK-EQ",
    "NSE:SBIN-EQ",
    "NSE:AXISBANK-EQ",
    # IT
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
    # Pharma
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
]

# ── NSE Indices ───────────────────────────────────────────────────
LEARNING_NSE_INDICES = [
    "NSE:NIFTY50-INDEX",
    "NSE:NIFTYBANK-INDEX",
    "NSE:FINNIFTY-INDEX",
]

# ── MCX Commodities (nearest active futures) ─────────────────────
# Update contract codes monthly (JUN → JUL etc.)
# Format: MCX:SYMBOL + DDMMMYY + FUT  (e.g. MCX:CRUDEOIL25JUNFUT)
LEARNING_MCX_COMMODITIES = [
    "MCX:CRUDEOIL25JUNFUT",   # crude oil — high volatility
    "MCX:GOLD25JUNFUT",       # gold — safe haven proxy
    "MCX:SILVER25JUNFUT",     # silver — more volatile than gold
    "MCX:COPPER25JUNFUT",     # copper — global growth indicator
    "MCX:NATURALGAS25JUNFUT", # natural gas — seasonal patterns
]

# ── Combined ─────────────────────────────────────────────────────
ALL_LEARNING_SYMBOLS = (
    LEARNING_NSE_EQUITIES
    + LEARNING_NSE_INDICES
    + LEARNING_MCX_COMMODITIES
)

# Symbols that work with commodity data — skip options strategies
COMMODITY_SYMBOLS = set(LEARNING_MCX_COMMODITIES)
