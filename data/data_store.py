"""
data_store.py
─────────────
Central in-memory store for all live tick data.
Receives raw ticks from fyers_stream and alpaca_stream,
builds multi-timeframe OHLCV candles, and exposes a clean
read interface for strategy modules.

Thread-safe: uses threading.Lock for all write operations.
"""

import threading
import logging
from collections import defaultdict, deque
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# Maximum raw ticks kept per symbol (older ticks are discarded)
MAX_TICKS = 5000

# Maximum candles kept per symbol per timeframe
MAX_CANDLES = 500

# Timeframe → seconds mapping
TF_SECONDS = {
    "1m":  60,
    "5m":  300,
    "15m": 900,
    "1H":  3600,
    "4H":  14400,
    "1D":  86400,
}

# IST offset in seconds (UTC+5:30). Used to align daily candle boundaries to
# midnight IST instead of midnight UTC (which would split NSE sessions across
# two candles).
_IST_OFFSET_SEC = 5 * 3600 + 30 * 60   # 19800 seconds


class DataStore:
    """
    Singleton data store shared across all bot modules.

    Usage:
        from data.data_store import store

        # Write (called by stream consumers)
        store.on_tick(symbol, tick_data)

        # Read (called by strategies / indicators)
        df = store.get_ohlcv("NSE:RELIANCE-EQ", "15m")
        price = store.get_ltp("NSE:RELIANCE-EQ")
    """

    def __init__(self):
        self._lock       = threading.Lock()

        # Raw ticks:  symbol → deque of tick dicts
        self._ticks: dict[str, deque] = defaultdict(lambda: deque(maxlen=MAX_TICKS))

        # OHLCV candles: symbol → timeframe → list of candle dicts
        self._candles: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))

        # Current open (forming) candle per symbol per timeframe
        self._open_candle: dict[str, dict[str, Optional[dict]]] = (
            defaultdict(lambda: defaultdict(lambda: None))
        )

        # Latest traded price (LTP) per symbol
        self._ltp: dict[str, float] = {}

        # Track which symbols have been initialised with historical data
        self._initialised: set = set()

    # ─────────────────────────────────────────────────────────────
    # WRITE — called by stream consumers
    # ─────────────────────────────────────────────────────────────

    def on_tick(self, symbol: str, tick: dict) -> None:
        """
        Process an incoming live tick.

        tick must contain:
            timestamp (datetime or unix float),
            ltp       (last traded price, float),
            volume    (traded volume this tick, int)

        Optional:
            bid, ask, oi (open interest)
        """
        with self._lock:
            # Normalise timestamp to UTC datetime
            ts = tick.get("timestamp")
            if isinstance(ts, (int, float)):
                ts = datetime.fromtimestamp(ts, tz=IST)
            tick["timestamp"] = ts

            ltp = float(tick["ltp"])
            self._ltp[symbol] = ltp
            self._ticks[symbol].append(tick)

            # Update OHLCV candles for every timeframe
            for tf, seconds in TF_SECONDS.items():
                self._update_candle(symbol, tf, seconds, ts, ltp, tick.get("volume", 0))

    def load_historical(self, symbol: str, timeframe: str, df: pd.DataFrame) -> None:
        """
        Seed the store with historical OHLCV data fetched from broker REST API.
        df must have columns: timestamp, open, high, low, close, volume
        """
        candles = df.to_dict("records")
        with self._lock:
            self._candles[symbol][timeframe] = candles[-MAX_CANDLES:]
            self._initialised.add(f"{symbol}_{timeframe}")
        logger.info(f"Loaded {len(candles)} historical candles for {symbol} [{timeframe}]")

    # ─────────────────────────────────────────────────────────────
    # READ — called by strategies and indicators
    # ─────────────────────────────────────────────────────────────

    def get_ohlcv(self, symbol: str, timeframe: str, n: int = 200) -> Optional[pd.DataFrame]:
        """
        Returns the last `n` closed OHLCV candles for a symbol/timeframe.
        Returns None if insufficient data (< 30 candles).

        Columns: timestamp, open, high, low, close, volume
        """
        with self._lock:
            candles = self._candles[symbol].get(timeframe, [])
            if len(candles) < 30:
                logger.debug(f"Insufficient candles for {symbol} [{timeframe}]: {len(candles)}")
                return None
            df = pd.DataFrame(candles[-n:])
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            df = df.sort_values("timestamp").reset_index(drop=True)
            return df

    def get_ltp(self, symbol: str) -> Optional[float]:
        """Returns the last traded price for a symbol."""
        # dict.get() is atomic in CPython (GIL), but use lock for consistency
        with self._lock:
            return self._ltp.get(symbol)

    def get_latest_tick(self, symbol: str) -> Optional[dict]:
        """Returns the most recent raw tick for a symbol."""
        with self._lock:
            ticks = self._ticks.get(symbol)
            return ticks[-1] if ticks else None

    def get_active_symbols(self) -> list[str]:
        """Returns all symbols currently receiving ticks."""
        return list(self._ltp.keys())

    def is_ready(self, symbol: str, timeframe: str, min_candles: int = 50) -> bool:
        """True if the symbol has enough candle history for reliable signals."""
        with self._lock:
            return len(self._candles[symbol].get(timeframe, [])) >= min_candles

    # ─────────────────────────────────────────────────────────────
    # INTERNAL — candle building logic
    # ─────────────────────────────────────────────────────────────

    def _update_candle(
        self,
        symbol: str,
        tf: str,
        tf_seconds: int,
        ts: datetime,
        price: float,
        volume: int,
    ) -> None:
        """
        Update or close the forming candle for a given timeframe.
        Called inside the write lock — do not acquire lock again here.
        """
        # Bucket this tick into the correct candle slot.
        # For daily (86400s) candles we align to midnight IST rather than
        # midnight UTC so that a full NSE trading session (9:15–15:30 IST)
        # always falls inside a single candle.
        epoch = ts.timestamp()
        if tf_seconds >= 86400:
            # Shift epoch to IST, floor to candle boundary, shift back
            candle_start_epoch = ((epoch + _IST_OFFSET_SEC) // tf_seconds) * tf_seconds - _IST_OFFSET_SEC
        else:
            candle_start_epoch = (epoch // tf_seconds) * tf_seconds
        candle_start = datetime.fromtimestamp(candle_start_epoch, tz=IST)

        open_candle = self._open_candle[symbol][tf]

        if open_candle is None or open_candle["timestamp"] != candle_start:
            # Close the previous candle (add to completed list)
            if open_candle is not None:
                candles = self._candles[symbol][tf]
                candles.append(open_candle)
                if len(candles) > MAX_CANDLES:
                    self._candles[symbol][tf] = candles[-MAX_CANDLES:]

            # Open a new forming candle
            self._open_candle[symbol][tf] = {
                "timestamp": candle_start,
                "open":      price,
                "high":      price,
                "low":       price,
                "close":     price,
                "volume":    volume,
            }
        else:
            # Update the forming candle
            open_candle["high"]   = max(open_candle["high"], price)
            open_candle["low"]    = min(open_candle["low"], price)
            open_candle["close"]  = price
            open_candle["volume"] += volume

    def summary(self) -> dict:
        """Returns a snapshot summary for logging/debugging."""
        with self._lock:
            return {
                sym: {
                    tf: len(candles)
                    for tf, candles in tfs.items()
                }
                for sym, tfs in self._candles.items()
            }


# ── Module-level singleton ────────────────────────────────────────
# Import this everywhere:  from data.data_store import store
store = DataStore()
