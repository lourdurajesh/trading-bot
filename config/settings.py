import logging
import os
import sys
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
# OPTIONS-SPECIFIC RISK PARAMETERS
# These are separate, tighter limits because options can lose 100% very fast
# ─────────────────────────────────────────

# Hard cap on number of lots per single options trade
MAX_OPTIONS_LOTS_PER_TRADE   = int(os.getenv("MAX_OPTIONS_LOTS_PER_TRADE", "2"))

# Minimum option LTP — avoid near-zero options that move 100% on noise
MIN_OPTION_LTP               = float(os.getenv("MIN_OPTION_LTP", "5.0"))

# Minimum open interest — avoid illiquid strikes (low OI = wide spread = slippage)
MIN_OPTION_OI                = int(os.getenv("MIN_OPTION_OI", "500"))

# Force-close options positions when DTE drops below this (expiry risk)
OPTIONS_DTE_FORCE_EXIT       = int(os.getenv("OPTIONS_DTE_FORCE_EXIT", "3"))

# VIX ceiling for SHORT premium strategies (short strangle) — above this is too dangerous
# Set at 27 to give a small buffer above the EXTREME risk threshold (25)
# Short strangles are still viable up to ~27 VIX; above that gamma risk is too high
OPTIONS_VIX_LIMIT            = float(os.getenv("OPTIONS_VIX_LIMIT", "27.0"))

# Separate daily loss cap for options only — stricter than equity daily limit
DAILY_OPTIONS_LOSS_LIMIT_PCT = float(os.getenv("DAILY_OPTIONS_LOSS_LIMIT_PCT", "2.0"))

# Max capital deployed in a single options trade (% of total capital)
MAX_OPTIONS_TRADE_PCT        = float(os.getenv("MAX_OPTIONS_TRADE_PCT", "5.0"))

# ─────────────────────────────────────────
# PER-STRATEGY OPTIONS CONFIGURATION
# Override any value via .env without touching code.
# ─────────────────────────────────────────

OPTIONS_STRATEGY_CONFIG: dict = {

    # Short Strangle — sell OTM call + put, collect premium
    "short_strangle": {
        "enabled":        os.getenv("OPTIONS_STRANGLE_ENABLED", "true").lower() == "true",
        "min_dte":        int(os.getenv("STRANGLE_MIN_DTE",       "20")),
        "max_dte":        int(os.getenv("STRANGLE_MAX_DTE",       "45")),
        "call_delta":     float(os.getenv("STRANGLE_CALL_DELTA",  "0.16")),
        "put_delta":      float(os.getenv("STRANGLE_PUT_DELTA",   "0.16")),
        "min_iv_rank":    float(os.getenv("STRANGLE_MIN_IV_RANK", "50.0")),
        "max_iv_rank":    float(os.getenv("STRANGLE_MAX_IV_RANK", "90.0")),
        "profit_target":  float(os.getenv("STRANGLE_PROFIT_TARGET", "0.50")),
        "stop_mult":      float(os.getenv("STRANGLE_STOP_MULT",     "2.0")),
    },

    # Debit Spread — buy ATM call/put spread on directional move
    "debit_spread": {
        "enabled":          os.getenv("OPTIONS_SPREAD_ENABLED", "true").lower() == "true",
        "min_dte":          int(os.getenv("SPREAD_MIN_DTE",       "7")),
        "max_dte":          int(os.getenv("SPREAD_MAX_DTE",       "21")),
        "atm_delta":        float(os.getenv("SPREAD_ATM_DELTA",   "0.40")),
        "max_iv_rank":      float(os.getenv("SPREAD_MAX_IV_RANK", "40.0")),
        "stop_pct":         float(os.getenv("SPREAD_STOP_PCT",    "0.50")),
        "net_debit_ratio":  float(os.getenv("SPREAD_NET_DEBIT_RATIO", "0.65")),
    },

    # Iron Condor — defined-risk strangle (4 legs), best in moderate-IV ranging markets
    "iron_condor": {
        "enabled":            os.getenv("OPTIONS_IC_ENABLED", "true").lower() == "true",
        "min_dte":            int(os.getenv("IC_MIN_DTE",             "21")),
        "max_dte":            int(os.getenv("IC_MAX_DTE",             "45")),
        "short_call_delta":   float(os.getenv("IC_SHORT_CALL_DELTA",  "0.20")),
        "short_put_delta":    float(os.getenv("IC_SHORT_PUT_DELTA",   "0.20")),
        "wing_width_pct":     float(os.getenv("IC_WING_WIDTH_PCT",    "0.02")),   # 2% of spot
        "min_iv_rank":        float(os.getenv("IC_MIN_IV_RANK",       "30.0")),
        "max_iv_rank":        float(os.getenv("IC_MAX_IV_RANK",       "80.0")),
        "profit_target":      float(os.getenv("IC_PROFIT_TARGET",     "0.50")),
        "stop_mult":          float(os.getenv("IC_STOP_MULT",         "2.0")),
        "allow_equities":     os.getenv("IC_ALLOW_EQUITIES", "true").lower() == "true",
        "min_stock_price":    float(os.getenv("IC_MIN_STOCK_PRICE",   "500.0")),
    },
}

# ─────────────────────────────────────────
# STRATEGY SETTINGS
# ─────────────────────────────────────────

# Minimum signal confidence (0.0 – 1.0) to pass to risk manager
MIN_SIGNAL_CONFIDENCE = float(os.getenv("MIN_SIGNAL_CONFIDENCE", "0.65"))

# ─────────────────────────────────────────
# INSTITUTIONAL F&O — conviction-scored ATM options
# ─────────────────────────────────────────

# Minimum abs(score) to fire an institutional trade (-10 to +10 scale)
# 7 = FII signal (3) + OI signal (2) + any 2 of the 3 remaining signals
CONVICTION_THRESHOLD   = int(os.getenv("CONVICTION_THRESHOLD", "7"))

# Max % of capital deployed in a single institutional trade (score 9-10 uses this)
# Score 7-8 always uses 35%. Hard ceiling enforced by options_risk gate.
MAX_FO_CAPITAL_PCT     = int(os.getenv("MAX_FO_CAPITAL_PCT", "50"))

# Cooldown period (minutes) on a symbol after a losing trade
SYMBOL_COOLDOWN_MINUTES = int(os.getenv("SYMBOL_COOLDOWN_MINUTES", "60"))

# Minimum Risk:Reward ratio — signals below this are discarded
MIN_RISK_REWARD         = float(os.getenv("MIN_RISK_REWARD",         "1.5"))
# Separate threshold for options signals (debit/credit spreads have different R:R profiles)
MIN_RISK_REWARD_OPTIONS = float(os.getenv("MIN_RISK_REWARD_OPTIONS", "0.8"))

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


# ─────────────────────────────────────────
# STARTUP VALIDATION
# ─────────────────────────────────────────

def validate_env() -> None:
    """
    Called once on startup from main.py / watchdog.py.
    Logs warnings for missing credentials and errors for truly fatal gaps.
    Does NOT crash the bot — allows paper trading without live broker creds.
    """
    _log = logging.getLogger("config.settings")
    warnings = []
    errors   = []

    # Fyers required for live NSE trading
    if not os.getenv("FYERS_APP_ID"):
        warnings.append("FYERS_APP_ID not set — live NSE trading disabled")
    if not os.getenv("FYERS_ACCESS_TOKEN"):
        warnings.append("FYERS_ACCESS_TOKEN not set — live NSE trading disabled")

    # Anthropic required for intelligence layer
    if not os.getenv("ANTHROPIC_API_KEY"):
        warnings.append("ANTHROPIC_API_KEY not set — analyst agent will run in simulation mode")

    # Telegram optional but useful
    if not os.getenv("TELEGRAM_BOT_TOKEN"):
        warnings.append("TELEGRAM_BOT_TOKEN not set — no trade alerts will be sent")

    # Capital sanity check
    cap = float(os.getenv("TOTAL_CAPITAL", "500000"))
    if cap < 10000:
        errors.append(f"TOTAL_CAPITAL={cap} is dangerously low (< ₹10,000) — check .env")

    risk_pct = float(os.getenv("RISK_PER_TRADE_PCT", "1.5"))
    if risk_pct > 3.0:
        errors.append(f"RISK_PER_TRADE_PCT={risk_pct}% exceeds 3% hard limit — check .env")

    for w in warnings:
        _log.warning(f"[Config] {w}")
    for e in errors:
        _log.error(f"[Config] FATAL: {e}")

    if errors:
        _log.error("[Config] Fatal configuration errors detected — review .env before trading")
        # Don't sys.exit() — let paper trading work without live creds
