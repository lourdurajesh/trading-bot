# ─────────────────────────────────────────────────────────────────
# WATCHLIST — verified valid Fyers symbols
# Format: NSE:SYMBOLNAME-EQ for equities
#         NSE:SYMBOLNAME-INDEX for indices
#
# 2026-04-08 revision:
#   Removed chronic backtest losers: WIPRO, LT, HCLTECH, ITC, ASIANPAINT, COFORGE
#   Added (better sector diversification): TATAMOTORS, SUNPHARMA, TATASTEEL,
#     ONGC, NTPC, BAJAJFINSV, ADANIPORTS, DRREDDY, DIVISLAB, MPHASIS
# ─────────────────────────────────────────────────────────────────

# ── NSE Large Cap (Nifty 50 — verified symbols) ───────────────────
NSE_EQUITIES = [
    # Banking & Finance
    "NSE:HDFCBANK-EQ",
    "NSE:ICICIBANK-EQ",
    "NSE:KOTAKBANK-EQ",
    "NSE:AXISBANK-EQ",
    "NSE:SBIN-EQ",
    "NSE:BAJFINANCE-EQ",
    "NSE:BAJAJFINSV-EQ",    # insurance + lending, less correlated to pure banking
    # IT
    "NSE:TCS-EQ",
    "NSE:INFY-EQ",
    # Telecom & Consumer
    "NSE:BHARTIARTL-EQ",
    "NSE:HINDUNILVR-EQ",
    "NSE:NESTLEIND-EQ",
    "NSE:TITAN-EQ",
    "NSE:MARUTI-EQ",
    # Energy & Industrial
    "NSE:RELIANCE-EQ",
    "NSE:NTPC-EQ",          # power utility, range-bound behaviour
    "NSE:ADANIPORTS-EQ",    # infra/logistics, high volume
    # Auto
    "NSE:TMCV-EQ",    # EV + exports, volatile and liquid
    "NSE:TMPV-EQ",    
    # Metals
    "NSE:TATASTEEL-EQ",     # cyclical, strong mean reversion patterns
    # Pharma
    "NSE:SUNPHARMA-EQ",     # largest pharma, less index-correlated
    "NSE:DRREDDY-EQ",       # exports + domestic pharma
    # Cement
    "NSE:ULTRACEMCO-EQ",
]

# ── NSE Mid Cap ───────────────────────────────────────────────────
# F&O-eligible, liquid, diverse earnings dates (avoids Q4 large-cap blackout).
# Selected on 3 criteria: (1) F&O lot exists, (2) ADV > 50Cr, (3) different
# results weeks than Nifty50 banks/IT cluster (avoid same-week veto cascade).
NSE_MIDCAP = [
    # Existing
    "NSE:POLICYBZR-EQ",
    "NSE:DIVISLAB-EQ",      # pharma, low mkt correlation
    "NSE:MPHASIS-EQ",       # IT mid-cap, US revenue base
    # IT mid-caps — results 2-3 weeks after TCS/Infy
    "NSE:PERSISTENT-EQ",    # product engineering IT, strong uptrend history
    "NSE:COFORGE-EQ",       # IT services, volatile, good for momentum
    "NSE:LTTS-EQ",          # L&T Technology Services, engineering R&D
    # Finance — NBFC, distinct from banking results calendar
    "NSE:ABCAPITAL-EQ",     # Aditya Birla Capital, diversified NBFC
    "NSE:MUTHOOTFIN-EQ",    # gold-loan NBFC, counter-cyclical
    # Pharma — different earnings window than SUNPHARMA/DRREDDY
    "NSE:AUROPHARMA-EQ",    # generics exporter, USD revenue sensitivity
    "NSE:ALKEM-EQ",         # domestic-focused pharma, less volatile
    # Consumer & Retail
    "NSE:HAVELLS-EQ",       # consumer electricals, India consumption theme
    "NSE:DMART-EQ",         # Avenue Supermarts, retail; low correlation to indices
    # Banking (smaller, independent results window)
    "NSE:IDFCFIRSTB-EQ",    # IDFC First Bank, high-beta, trending
    "NSE:FEDERALBNK-EQ",    # Federal Bank, Kerala-based, good range patterns
    # Diversified digital
    "NSE:ZOMATO-EQ",        # food-tech, high volume, mean-reversion patterns
    "NSE:NAUKRI-EQ",        # Info Edge, internet; independent sector cycle
]

# ── NSE Indices ───────────────────────────────────────────────────
NSE_INDICES = [
    "NSE:NIFTY50-INDEX",
    "NSE:NIFTYBANK-INDEX",
    "NSE:FINNIFTY-INDEX",
]

# ── Options universe ──────────────────────────────────────────────
NSE_OPTIONS_UNIVERSE = [
    "NSE:NIFTY50-INDEX",
    "NSE:NIFTYBANK-INDEX",
    "NSE:FINNIFTY-INDEX",
    "NSE:RELIANCE-EQ",
    "NSE:TCS-EQ",
    "NSE:HDFCBANK-EQ",
]

# ── US (disabled until Alpaca account ready) ──────────────────────
US_EQUITIES = []
US_ETFS     = []

# ── Combined lists ────────────────────────────────────────────────
ALL_NSE_SYMBOLS = NSE_EQUITIES + NSE_MIDCAP + NSE_INDICES
ALL_US_SYMBOLS  = US_EQUITIES + US_ETFS
ALL_SYMBOLS     = ALL_NSE_SYMBOLS + ALL_US_SYMBOLS

PRIORITY_SYMBOLS = [
    "NSE:NIFTYBANK-INDEX",  # institutional momentum — evaluated first (preferred over NIFTY)
    "NSE:NIFTY50-INDEX",
    "NSE:FINNIFTY-INDEX",   # added: now covered by institutional_momentum + directional_options
    "NSE:RELIANCE-EQ",
    "NSE:HDFCBANK-EQ",
    "NSE:TCS-EQ",
    "NSE:SBIN-EQ",
    "NSE:INFY-EQ",
    "NSE:TMPV-EQ",
    "NSE:TMCV-EQ",
    "NSE:BHARTIARTL-EQ",
]
