"""
alpaca_stream.py
────────────────
Connects to Alpaca WebSocket v2 and streams live US equity tick
data into the central DataStore. Mirrors the same interface as
FyersStream for unified consumption by the bot orchestrator.

Alpaca WebSocket docs:
https://docs.alpaca.markets/reference/streaming-market-data
"""

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

import pandas as pd
import alpaca_trade_api as alpaca
from alpaca_trade_api.stream import Stream

from config import settings
from config.watchlist import US_EQUITIES, US_ETFS
from data.data_store import store

logger = logging.getLogger(__name__)

US_SYMBOLS_ALL = list(set(US_EQUITIES + US_ETFS))


class AlpacaStream:
    """
    Manages the Alpaca WebSocket connection for live US market data.

    Usage:
        stream = AlpacaStream()
        stream.start()          # non-blocking, background thread
        stream.stop()
    """

    def __init__(self):
        self._rest_client: Optional[alpaca.REST] = None
        self._stream: Optional[Stream] = None
        self._running      = False
        self._thread: Optional[threading.Thread] = None

    # ─────────────────────────────────────────────────────────────
    # PUBLIC
    # ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start streaming in a background thread."""
        if self._running:
            logger.warning("AlpacaStream already running.")
            return

        if not settings.ALPACA_API_KEY or not settings.ALPACA_SECRET_KEY:
            logger.warning("Alpaca credentials not set — US stream disabled.")
            return

        self._running = True
        self._init_rest_client()
        self._seed_historical_data()

        self._thread = threading.Thread(target=self._run, daemon=True, name="AlpacaStream")
        self._thread.start()
        logger.info("AlpacaStream started.")

    def stop(self) -> None:
        """Gracefully stop the stream."""
        self._running = False
        if self._stream:
            try:
                self._stream.stop()
            except Exception:
                pass
        logger.info("AlpacaStream stopped.")

    # ─────────────────────────────────────────────────────────────
    # INTERNAL — connection
    # ─────────────────────────────────────────────────────────────

    def _run(self) -> None:
        """Connect and run the Alpaca async stream (blocks until stopped)."""
        while self._running:
            try:
                logger.info("Connecting to Alpaca WebSocket...")
                self._stream = Stream(
                    key_id=settings.ALPACA_API_KEY,
                    secret_key=settings.ALPACA_SECRET_KEY,
                    base_url=settings.ALPACA_BASE_URL,
                    data_feed="iex",        # 'iex' free | 'sip' paid consolidated
                    raw_data=False,
                )
                # Subscribe to minute bars and trades for all US symbols
                self._stream.subscribe_bars(self._on_bar, *US_SYMBOLS_ALL)
                self._stream.subscribe_trades(self._on_trade, *US_SYMBOLS_ALL)
                self._stream.run()          # blocks here

            except Exception as e:
                logger.error(f"AlpacaStream error: {e}")
                if self._running:
                    logger.info("Reconnecting in 10 seconds...")
                    time.sleep(10)

    def _init_rest_client(self) -> None:
        """Initialise Alpaca REST client for historical data."""
        self._rest_client = alpaca.REST(
            key_id=settings.ALPACA_API_KEY,
            secret_key=settings.ALPACA_SECRET_KEY,
            base_url=settings.ALPACA_BASE_URL,
        )
        logger.info("Alpaca REST client initialised.")

    # ─────────────────────────────────────────────────────────────
    # INTERNAL — stream callbacks
    # ─────────────────────────────────────────────────────────────

    async def _on_bar(self, bar) -> None:
        """
        Called on every completed minute bar from Alpaca.
        bar fields: symbol, open, high, low, close, volume, timestamp
        """
        try:
            tick = {
                "timestamp": bar.timestamp.to_pydatetime().replace(tzinfo=timezone.utc).astimezone(IST),
                "ltp":       float(bar.close),
                "volume":    int(bar.volume),
                "open":      float(bar.open),
                "high":      float(bar.high),
                "low":       float(bar.low),
            }
            store.on_tick(bar.symbol, tick)
        except Exception as e:
            logger.error(f"Error processing Alpaca bar: {e}")

    async def _on_trade(self, trade) -> None:
        """
        Called on every trade print from Alpaca.
        Used for more granular LTP updates between bar closes.
        """
        try:
            tick = {
                "timestamp": trade.timestamp.to_pydatetime().replace(tzinfo=timezone.utc).astimezone(IST),
                "ltp":       float(trade.price),
                "volume":    int(trade.size),
            }
            store.on_tick(trade.symbol, tick)
        except Exception as e:
            logger.error(f"Error processing Alpaca trade: {e}")

    # ─────────────────────────────────────────────────────────────
    # INTERNAL — historical seeding
    # ─────────────────────────────────────────────────────────────

    def _seed_historical_data(self) -> None:
        """
        Fetch historical OHLCV from Alpaca REST API on startup.
        Seeds priority US symbols across key timeframes.
        """
        if not self._rest_client:
            return

        from config.watchlist import PRIORITY_SYMBOLS
        us_priority = [s for s in PRIORITY_SYMBOLS if not s.startswith("NSE:")]

        timeframes_to_seed = {
            "15m": {"timeframe": "15Min", "days_back": 30},
            "1H":  {"timeframe": "1Hour", "days_back": 90},
            "1D":  {"timeframe": "1Day",  "days_back": 365},
        }

        for symbol in us_priority:
            for tf, params in timeframes_to_seed.items():
                try:
                    df = self._fetch_historical(symbol, params["timeframe"], params["days_back"])
                    if df is not None and len(df) > 0:
                        store.load_historical(symbol, tf, df)
                except Exception as e:
                    logger.error(f"Historical seed failed for {symbol} [{tf}]: {e}")
                time.sleep(0.2)    # rate limit

    def _fetch_historical(
        self, symbol: str, timeframe: str, days_back: int
    ) -> Optional[pd.DataFrame]:
        """Fetch OHLCV bars from Alpaca REST API."""
        end   = datetime.now(tz=IST)
        start = end - timedelta(days=days_back)

        bars = self._rest_client.get_bars(
            symbol,
            timeframe,
            start=start.isoformat(),
            end=end.isoformat(),
            adjustment="raw",
        ).df

        if bars.empty:
            return None

        bars = bars.reset_index()
        bars = bars.rename(columns={
            "timestamp": "timestamp",
            "open":      "open",
            "high":      "high",
            "low":       "low",
            "close":     "close",
            "volume":    "volume",
        })
        bars["timestamp"] = pd.to_datetime(bars["timestamp"], utc=True)
        return bars[["timestamp", "open", "high", "low", "close", "volume"]]
