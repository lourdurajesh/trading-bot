"""
data_fetcher.py
───────────────
Fetches historical OHLCV data for backtesting.
Primary source: Fyers REST API (3 years)
Fallback source: Yahoo Finance (for symbols not on Fyers)

Data is cached locally in db/historical/ as CSV files.
Re-fetched if older than 1 day.
"""

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd
import requests

from config import settings

logger = logging.getLogger(__name__)

CACHE_DIR        = "db/historical"
CACHE_DAYS       = 1        # re-fetch if cache older than this
YEARS_BACK       = 3
FYERS_RATE_LIMIT = 0.3      # seconds between Fyers API calls

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

os.makedirs(CACHE_DIR, exist_ok=True)


def fetch_historical(
    symbol:     str,
    timeframe:  str = "1D",
    years_back: int = YEARS_BACK,
    force:      bool = False,
) -> Optional[pd.DataFrame]:
    """
    Fetch historical OHLCV for a symbol.

    symbol:    Fyers format — NSE:RELIANCE-EQ
    timeframe: 1D, 1H, 15m
    years_back: how many years of history

    Returns DataFrame with columns: timestamp, open, high, low, close, volume
    Returns None if fetch fails.
    """
    cache_path = _cache_path(symbol, timeframe)

    # Return cached data if fresh
    if not force and os.path.exists(cache_path):
        age_days = (datetime.now() - datetime.fromtimestamp(os.path.getmtime(cache_path))).days
        if age_days < CACHE_DAYS:
            try:
                df = pd.read_csv(cache_path, parse_dates=["timestamp"])
                if len(df) > 10:
                    logger.debug(f"[DataFetcher] Cache hit: {symbol} [{timeframe}] — {len(df)} rows")
                    return df
            except Exception:
                pass

    # Try Fyers first
    df = _fetch_fyers(symbol, timeframe, years_back)

    # Fallback to Yahoo Finance
    if df is None or len(df) < 30:
        yahoo_sym = _to_yahoo_symbol(symbol)
        df = _fetch_yahoo(yahoo_sym, timeframe, years_back)

    if df is None or len(df) < 30:
        logger.warning(f"[DataFetcher] Could not fetch {symbol} [{timeframe}]")
        return None

    # Standardise and save
    df = _standardise(df)
    df.to_csv(cache_path, index=False)
    logger.info(f"[DataFetcher] Fetched {symbol} [{timeframe}]: {len(df)} rows ({years_back}y)")
    return df


def fetch_all(
    symbols:    list[str],
    timeframe:  str = "1D",
    years_back: int = YEARS_BACK,
) -> dict[str, pd.DataFrame]:
    """
    Fetch historical data for a list of symbols.
    Returns dict: symbol → DataFrame
    """
    results = {}
    total   = len(symbols)
    for i, symbol in enumerate(symbols):
        logger.info(f"[DataFetcher] Fetching {i+1}/{total}: {symbol}")
        df = fetch_historical(symbol, timeframe, years_back)
        if df is not None:
            results[symbol] = df
        time.sleep(FYERS_RATE_LIMIT)
    logger.info(f"[DataFetcher] Done: {len(results)}/{total} symbols fetched")
    return results


# ─────────────────────────────────────────────────────────────────
# FYERS FETCHER
# ─────────────────────────────────────────────────────────────────

def _fetch_fyers(symbol: str, timeframe: str, years_back: int) -> Optional[pd.DataFrame]:
    """Fetch from Fyers REST API."""
    if not settings.FYERS_ACCESS_TOKEN:
        return None
    try:
        from fyers_apiv3 import fyersModel
        client = fyersModel.FyersModel(
            client_id = settings.FYERS_APP_ID,
            token     = settings.FYERS_ACCESS_TOKEN,
            is_async  = False,
        )
        resolution_map = {
            "1D":  "D",
            "1H":  "60",
            "15m": "15",
            "5m":  "5",
        }
        resolution = resolution_map.get(timeframe, "D")
        end_date   = datetime.now()
        start_date = end_date - timedelta(days=years_back * 365)

        data = {
            "symbol":      symbol,
            "resolution":  resolution,
            "date_format": "1",
            "range_from":  start_date.strftime("%Y-%m-%d"),
            "range_to":    end_date.strftime("%Y-%m-%d"),
            "cont_flag":   "1",
        }
        response = client.history(data=data)
        if response.get("s") != "ok":
            return None

        candles = response.get("candles", [])
        if not candles:
            return None

        df = pd.DataFrame(
            candles,
            columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
        return df

    except Exception as e:
        logger.debug(f"[DataFetcher] Fyers fetch failed for {symbol}: {e}")
        return None


# ─────────────────────────────────────────────────────────────────
# YAHOO FINANCE FALLBACK
# ─────────────────────────────────────────────────────────────────

def _fetch_yahoo(yahoo_symbol: str, timeframe: str, years_back: int) -> Optional[pd.DataFrame]:
    """Fetch from Yahoo Finance — no API key needed."""
    interval_map = {
        "1D":  ("1d",  f"{years_back}y"),
        "1H":  ("1h",  "730d"),     # Yahoo max for 1h is ~2 years
        "15m": ("15m", "60d"),      # Yahoo max for 15m is 60 days
    }
    interval, period = interval_map.get(timeframe, ("1d", "3y"))

    try:
        url  = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}"
            f"?interval={interval}&range={period}"
        )
        resp = requests.get(url, headers=HEADERS, timeout=15)
        data = resp.json()

        result = data["chart"]["result"][0]
        timestamps = result["timestamp"]
        ohlcv      = result["indicators"]["quote"][0]

        df = pd.DataFrame({
            "timestamp": pd.to_datetime(timestamps, unit="s", utc=True),
            "open":      ohlcv["open"],
            "high":      ohlcv["high"],
            "low":       ohlcv["low"],
            "close":     ohlcv["close"],
            "volume":    ohlcv["volume"],
        })
        df = df.dropna(subset=["close"])
        return df

    except Exception as e:
        logger.debug(f"[DataFetcher] Yahoo fetch failed for {yahoo_symbol}: {e}")
        return None


# ─────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────

def _standardise(df: pd.DataFrame) -> pd.DataFrame:
    """Ensure consistent column types and sort by timestamp."""
    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.sort_values("timestamp").reset_index(drop=True)
    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0).astype(int)
    df = df.dropna(subset=["open", "high", "low", "close"])
    return df


def _cache_path(symbol: str, timeframe: str) -> str:
    safe = symbol.replace(":", "_").replace("-", "_")
    return os.path.join(CACHE_DIR, f"{safe}_{timeframe}.csv")


def _to_yahoo_symbol(fyers_symbol: str) -> str:
    """Convert NSE:RELIANCE-EQ → RELIANCE.NS"""
    ticker = fyers_symbol.replace("NSE:", "").replace("-EQ", "").replace("-INDEX", "")
    if "INDEX" in fyers_symbol:
        mapping = {
            "NIFTY50":   "^NSEI",
            "NIFTYBANK": "^NSEBANK",
        }
        return mapping.get(ticker, f"{ticker}.NS")
    return f"{ticker}.NS"
