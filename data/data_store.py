"""
data_store.py
─────────────
Central in-memory store for all live tick data.
Receives raw ticks from fyers_stream and alpaca_stream,
builds multi-timeframe OHLCV candles, and exposes a clean
read interface for strategy modules.

Thread-safe: uses threading.Lock for all write operations.
"""

import gzip
import json
import os
import threading
import logging
from collections import defaultdict, deque
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# Maximum raw ticks kept per symbol (older ticks are discarded).
# 1000 ticks ≈ 16 minutes of data at 1 tick/sec — more than enough to build
# all timeframe candles. 5000 was ~40 MB across 40 symbols on a 1 GB server.
MAX_TICKS = 1000

# Maximum candles kept per symbol per timeframe
MAX_CANDLES = 500

# Candle snapshot — persists across bot restarts so strategies have history immediately
SNAPSHOT_PATH = "db/candle_snapshot.json.gz"
_SNAPSHOT_TIMEFRAMES  = {"1H", "15m", "5m", "1D"}  # only timeframes strategies need
_SNAPSHOT_MAX_CANDLES = 200                          # enough for any strategy

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

        # Wall-clock time we last received an LTP update for each symbol
        self._ltp_received_at: dict[str, datetime] = {}

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
            self._ltp_received_at[symbol] = datetime.now(tz=IST)
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
        Returns the last `n` candles for a symbol/timeframe, including the
        current forming (open) bar so that indicators like RSI reflect live
        price movement within the current period rather than freezing at the
        previous bar's close.
        Returns None if insufficient data (< 30 candles).

        Columns: timestamp, open, high, low, close, volume
        """
        with self._lock:
            candles = self._candles[symbol].get(timeframe, [])
            open_candle = self._open_candle[symbol].get(timeframe)
            all_candles = list(candles)
            if open_candle is not None:
                all_candles.append(open_candle)
            if len(all_candles) < 30:
                logger.debug(f"Insufficient candles for {symbol} [{timeframe}]: {len(all_candles)}")
                return None
            df = pd.DataFrame(all_candles[-n:])
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert("Asia/Kolkata")
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

    def get_ltp_age(self, symbol: str) -> Optional[float]:
        """
        Seconds since this bot last received an LTP update for symbol.
        Based on wall-clock receipt time, not exchange timestamp, so slow
        markets (metals, midday) don't show false staleness.
        Returns None if we have never received a tick for this symbol.
        """
        with self._lock:
            received_at = self._ltp_received_at.get(symbol)
        if received_at is None:
            return None
        return (datetime.now(tz=IST) - received_at).total_seconds()

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

    # ─────────────────────────────────────────────────────────────
    # PERSISTENCE — snapshot across restarts
    # ─────────────────────────────────────────────────────────────

    def save_snapshot(self, path: str = SNAPSHOT_PATH) -> int:
        """
        Write closed candles to a gzip-compressed JSON file.
        Only stores timeframes in _SNAPSHOT_TIMEFRAMES to keep the file small.
        Returns the number of bytes written.
        """
        data: dict = {}
        with self._lock:
            for sym, tfs in self._candles.items():
                sym_data: dict = {}
                for tf, candles in tfs.items():
                    if tf not in _SNAPSHOT_TIMEFRAMES or not candles:
                        continue
                    slim = []
                    for c in candles[-_SNAPSHOT_MAX_CANDLES:]:
                        ts = c["timestamp"]
                        if isinstance(ts, datetime):
                            ts = ts.isoformat()
                        slim.append([ts, c["open"], c["high"], c["low"], c["close"], c["volume"]])
                    if slim:
                        sym_data[tf] = slim
                if sym_data:
                    data[sym] = sym_data
        raw = json.dumps(data, separators=(",", ":")).encode()
        compressed = gzip.compress(raw, compresslevel=6)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(compressed)
        logger.info(
            f"[DataStore] Snapshot saved: {len(data)} symbols, "
            f"{len(compressed) // 1024}KB → {path}"
        )
        return len(compressed)

    def load_snapshot(self, path: str = SNAPSHOT_PATH) -> int:
        """
        Pre-seed the store from a previously saved snapshot.
        Skips any (symbol, timeframe) pair already populated by a live broker seed.
        Call this BEFORE fyers_stream.start() so strategies have data immediately.
        Returns the number of symbols loaded.
        """
        if not os.path.exists(path):
            logger.debug(f"[DataStore] No snapshot at {path} — starting cold")
            return 0
        try:
            with gzip.open(path, "rb") as fh:
                data = json.loads(fh.read())
        except Exception as exc:
            logger.warning(f"[DataStore] Failed to load snapshot {path}: {exc}")
            return 0
        loaded = 0
        with self._lock:
            for sym, tfs in data.items():
                for tf, rows in tfs.items():
                    existing = self._candles[sym].get(tf, [])
                    if len(existing) >= len(rows):
                        continue   # broker already seeded more data — don't overwrite
                    candles = []
                    for row in rows:
                        ts_str, o, h, lo, c, v = row
                        ts = datetime.fromisoformat(ts_str)
                        candles.append({"timestamp": ts, "open": o, "high": h,
                                        "low": lo, "close": c, "volume": v})
                    self._candles[sym][tf] = candles[-MAX_CANDLES:]
                    self._initialised.add(f"{sym}_{tf}")
                loaded += 1
        logger.info(f"[DataStore] Snapshot loaded: {loaded} symbols from {path}")
        return loaded

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
