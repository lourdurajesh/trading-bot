# ─────────────────────────────────────────────────────────────────
# WATCHLIST — verified valid Fyers symbols
# Format: NSE:SYMBOLNAME-EQ for equities
#         NSE:SYMBOLNAME-INDEX for indices
# ─────────────────────────────────────────────────────────────────

# ── NSE Large Cap (Nifty 50 — verified symbols) ───────────────────
NSE_EQUITIES = [
    "NSE:RELIANCE-EQ",
    "NSE:TCS-EQ",
    "NSE:HDFCBANK-EQ",
    "NSE:INFY-EQ",
    "NSE:ICICIBANK-EQ",
    "NSE:HINDUNILVR-EQ",
    "NSE:ITC-EQ",
    "NSE:SBIN-EQ",
    "NSE:BHARTIARTL-EQ",
    "NSE:KOTAKBANK-EQ",
    "NSE:LT-EQ",
    "NSE:AXISBANK-EQ",
    "NSE:WIPRO-EQ",
    "NSE:HCLTECH-EQ",
    "NSE:ASIANPAINT-EQ",
    "NSE:MARUTI-EQ",
    "NSE:BAJFINANCE-EQ",
    "NSE:TITAN-EQ",
    "NSE:ULTRACEMCO-EQ",
    "NSE:NESTLEIND-EQ",
]

# ── NSE Mid Cap ───────────────────────────────────────────────────
NSE_MIDCAP = [
    "NSE:PERSISTENT-EQ",
    "NSE:COFORGE-EQ",
    # "NSE:LTIMINDTEC-EQ",  # removed — verify symbol on Fyers
    "NSE:POLICYBZR-EQ",
]

# ── NSE Indices ───────────────────────────────────────────────────
NSE_INDICES = [
    "NSE:NIFTY50-INDEX",
    "NSE:NIFTYBANK-INDEX",
]

# ── Options universe ──────────────────────────────────────────────
NSE_OPTIONS_UNIVERSE = [
    "NSE:NIFTY50-INDEX",
    "NSE:NIFTYBANK-INDEX",
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
    "NSE:NIFTY50-INDEX",
    "NSE:RELIANCE-EQ",
    "NSE:HDFCBANK-EQ",
    "NSE:TCS-EQ",
    "NSE:SBIN-EQ",
    "NSE:INFY-EQ",
]