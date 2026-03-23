import os
from dotenv import load_dotenv

load_dotenv(override=True)

# ─────────────────────────────────────────
# BROKER CREDENTIALS
# ─────────────────────────────────────────

FYERS_APP_ID       = os.getenv("FYERS_APP_ID", "")
FYERS_SECRET_KEY   = os.getenv("FYERS_SECRET_KEY", "")
FYERS_REDIRECT_URI = os.getenv("FYERS_REDIRECT_URI", "https://127.0.0.1")
FYERS_ACCESS_TOKEN = os.getenv("FYERS_ACCESS_TOKEN", "")   # refreshed daily
FYERS_PAN          = os.getenv("FYERS_PAN", "")
FYERS_PIN          = os.getenv("FYERS_PIN", "")

ALPACA_API_KEY     = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY  = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_PAPER       = os.getenv("ALPACA_PAPER", "true").lower() == "true"
ALPACA_BASE_URL    = (
    "https://paper-api.alpaca.markets"
    if ALPACA_PAPER else
    "https://api.alpaca.markets"
)

# ─────────────────────────────────────────
# BOT MODE
# ─────────────────────────────────────────

# "AUTO"   → signals fire and execute immediately after risk checks
# "MANUAL" → signals queue in dashboard, you confirm each trade
BOT_MODE = os.getenv("BOT_MODE", "MANUAL")   # start safe, switch to AUTO when confident

# ─────────────────────────────────────────
# CAPITAL & RISK PARAMETERS
# ─────────────────────────────────────────

TOTAL_CAPITAL         = float(os.getenv("TOTAL_CAPITAL", "500000"))   # INR (adjust to your capital)
RISK_PER_TRADE_PCT    = float(os.getenv("RISK_PER_TRADE_PCT", "1.5")) # % of capital risked per trade
MAX_OPEN_POSITIONS    = int(os.getenv("MAX_OPEN_POSITIONS", "10"))
MAX_PORTFOLIO_HEAT    = float(os.getenv("MAX_PORTFOLIO_HEAT", "60"))  # % — no new trades beyond this

# Kill switch — halts all trading for the day if daily loss exceeds this %
DAILY_LOSS_LIMIT_PCT  = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "3.0"))

# Maximum allocation to options strategies (% of total capital)
MAX_OPTIONS_ALLOCATION_PCT = float(os.getenv("MAX_OPTIONS_ALLOCATION_PCT", "30.0"))

# ─────────────────────────────────────────
# STRATEGY SETTINGS
# ─────────────────────────────────────────

# Minimum signal confidence (0.0 – 1.0) to pass to risk manager
MIN_SIGNAL_CONFIDENCE = float(os.getenv("MIN_SIGNAL_CONFIDENCE", "0.65"))

# Cooldown period (minutes) on a symbol after a losing trade
SYMBOL_COOLDOWN_MINUTES = int(os.getenv("SYMBOL_COOLDOWN_MINUTES", "60"))

# Minimum Risk:Reward ratio — signals below this are discarded
MIN_RISK_REWARD = float(os.getenv("MIN_RISK_REWARD", "1.5"))

# ─────────────────────────────────────────
# TIMEFRAMES (used by data_store + strategies)
# ─────────────────────────────────────────

TIMEFRAMES = ["1m", "5m", "15m", "1H", "4H", "1D"]

# Primary signal timeframe per strategy (can be overridden per strategy)
TREND_SIGNAL_TF     = "1H"
REVERSION_SIGNAL_TF = "15m"
OPTIONS_SIGNAL_TF   = "1D"

# ─────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────

DB_PATH = os.getenv("DB_PATH", "db/trades.db")

# ─────────────────────────────────────────
# DASHBOARD / API
# ─────────────────────────────────────────

API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "8000"))

# ─────────────────────────────────────────
# NOTIFICATIONS
# ─────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

# ─────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_DIR   = os.getenv("LOG_DIR", "logs")

# ─────────────────────────────────────────
# MARKET HOURS (IST)
# ─────────────────────────────────────────

NSE_OPEN  = "09:15"
NSE_CLOSE = "15:30"

# US market in IST (EST + 5:30)
NYSE_OPEN_IST  = "19:00"
NYSE_CLOSE_IST = "01:30"  # next day
