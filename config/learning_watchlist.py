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

# ── MCX Commodities (nearest active futures) ─────────────────────
# Update contract codes monthly (JUN → JUL etc.)
# Format: MCX:SYMBOL + YY + MON + FUT  (e.g. MCX:CRUDEOIL26JUNFUT)
# commodity_options_learning._fyers_sym() auto-computes these at runtime;
# this list is the manual fallback used by the learning strategy engine.
LEARNING_MCX_COMMODITIES = [
    # Precious metals
    "MCX:GOLD26JUNFUT",        # gold — safe haven, INR/10gm
    "MCX:GOLDM26JUNFUT",       # gold mini (100g lot)
    "MCX:GOLDGUINEA26JUNFUT",  # gold guinea (8g lot)
    "MCX:GOLDPETAL26JUNFUT",   # gold petal (1g lot)
    "MCX:SILVER26JULFUT",      # silver — more volatile than gold, INR/kg
    "MCX:SILVERM26JULFUT",     # silver mini (5 kg lot)
    "MCX:SILVERMIC26JULFUT",   # silver micro (1 kg lot)
    # Energy
    "MCX:CRUDEOIL26JUNFUT",    # crude oil — high volatility, INR/bbl
    "MCX:CRUDEOILM26JUNFUT",   # crude oil mini (10 bbl lot)
    "MCX:NATURALGAS26JUNFUT",  # natural gas — seasonal patterns, INR/mmBtu
    "MCX:NATGASMINI26JUNFUT",  # natural gas mini (250 mmBtu lot)
    # Base metals
    "MCX:COPPER26JUNFUT",      # copper — global growth indicator, INR/kg
    "MCX:COPPERM26JUNFUT",     # copper mini
    "MCX:ZINC26JUNFUT",        # zinc, INR/kg
    "MCX:ZINCMINI26JUNFUT",    # zinc mini
    "MCX:ALUMINIUM26JUNFUT",   # aluminium, INR/kg
    "MCX:ALUMINI26JUNFUT",     # aluminium mini
    "MCX:LEAD26JUNFUT",        # lead, INR/kg
    "MCX:LEADMINI26JUNFUT",    # lead mini
    "MCX:NICKEL26JUNFUT",      # nickel, INR/kg
    "MCX:NICKELM26JUNFUT",     # nickel mini
    # Agricultural
    "MCX:COTTON26JUNFUT",      # cotton, INR/bale
    "MCX:MENTHAOIL26JUNFUT",   # mentha oil, INR/kg
]

# ── Combined ─────────────────────────────────────────────────────
ALL_LEARNING_SYMBOLS = (
    LEARNING_NSE_EQUITIES
    + LEARNING_NSE_INDICES
    + LEARNING_MCX_COMMODITIES
)

# Symbols that work with commodity data — skip options strategies
COMMODITY_SYMBOLS = set(LEARNING_MCX_COMMODITIES)
