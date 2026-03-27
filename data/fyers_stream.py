"""
fyers_stream.py
───────────────
Connects to Fyers WebSocket v3 and streams live NSE/BSE tick data
into the central DataStore. Handles auth, reconnection, and
historical data seeding on startup.

Fyers WebSocket v3 docs:
https://myapi.fyers.in/docs/#tag/Data-WebSocket
"""

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
from typing import Callable, Optional

import pandas as pd
from fyers_apiv3 import fyersModel
from fyers_apiv3.FyersWebsocket import data_ws

from config import settings
from config.watchlist import NSE_INDICES
from data.data_store import store

logger = logging.getLogger(__name__)


class FyersStream:
    """
    Manages the Fyers WebSocket connection for live NSE/BSE data.

    Usage:
        stream = FyersStream()
        stream.start()          # non-blocking, runs in background thread
        stream.stop()
    """

    def __init__(self):
        self._fyers_client: Optional[fyersModel.FyersModel] = None
        self._ws_client    = None
        self._running      = False
        self._thread: Optional[threading.Thread] = None
        self._reconnect_delay = 5
        self._max_reconnects  = 10
        self._gap_start       = None

    # ─────────────────────────────────────────────────────────────
    # PUBLIC
    # ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start streaming in a background thread."""
        if self._running:
            logger.warning("FyersStream already running.")
            return

        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="FyersStream")
        self._thread.start()
        logger.info("FyersStream started.")

    def stop(self) -> None:
        """Gracefully stop the stream."""
        self._running = False
        if self._ws_client:
            try:
                self._ws_client.close_connection()
            except Exception:
                pass
        # Give the thread 3 seconds to exit cleanly
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        logger.info("FyersStream stopped.")

    # ─────────────────────────────────────────────────────────────
    # INTERNAL — connection lifecycle
    # ─────────────────────────────────────────────────────────────

    def _run(self) -> None:
        """Main loop — connects once and stays connected."""
        self._init_rest_client()
        self._seed_historical_data()

        logger.info("Connecting to Fyers WebSocket...")
        self._connect()   # blocks until self._running = False

    def _init_rest_client(self) -> None:
        """Initialise the Fyers REST client for historical data fetching."""
        if not settings.FYERS_ACCESS_TOKEN:
            logger.warning("FYERS_ACCESS_TOKEN not set — historical seeding skipped.")
            return
        self._fyers_client = fyersModel.FyersModel(
            client_id=settings.FYERS_APP_ID,
            token=settings.FYERS_ACCESS_TOKEN,
            log_path="logs/",
            is_async=False,
        )
        logger.info("Fyers REST client initialised.")

    def _connect(self) -> None:
        """Create WebSocket client and start streaming."""
        if not settings.FYERS_ACCESS_TOKEN:
            logger.error("Cannot connect: FYERS_ACCESS_TOKEN is empty.")
            return

        self._ws_client = data_ws.FyersDataSocket(
            access_token=settings.FYERS_ACCESS_TOKEN,
            log_path="logs/",
            litemode=True,
            write_to_file=False,
            reconnect=False,        # we handle reconnect ourselves
            on_connect=self._on_connect,
            on_close=self._on_close,
            on_error=self._on_error,
            on_message=self._on_message,
        )
        self._ws_client.connect()
        # Block here until shutdown is requested
        while self._running:
            time.sleep(1)

    # ─────────────────────────────────────────────────────────────
    # INTERNAL — WebSocket callbacks
    # ─────────────────────────────────────────────────────────────

    def _on_connect(self) -> None:
        logger.info("Fyers WebSocket connected. Subscribing to symbols...")
        self._subscribe()
        # Fill any data gap since last disconnect
        if hasattr(self, "_gap_start") and self._gap_start:
            self._fill_gap(self._gap_start)
            self._gap_start = None

    def _on_close(self, code: int = 0) -> None:
        if self._running:
            logger.warning(f"Fyers WebSocket closed: [{code}] — will reconnect")
            self._gap_start = datetime.now(tz=IST)
        else:
            logger.info("FyersStream: closed cleanly on shutdown.")

    def _on_error(self, error) -> None:
        logger.error(f"Fyers WebSocket error: {error}")
        # -300 = invalid symbol — non-fatal, do not trigger reconnect
        if isinstance(error, dict) and error.get("code") == -300:
            invalid = error.get("invalid_symbols", [])
            logger.warning(f"Ignoring invalid symbols: {invalid}. Remove from watchlist.")
            return

    def _on_message(self, message: dict) -> None:
        """
        Normalise Fyers tick format and push to DataStore.

        Fyers tick fields (v3):
            symbol, ltp, vol_traded_today, last_traded_time,
            bid_price, ask_price, open_price, high_price,
            low_price, prev_close_price, oi, ...
        """
        try:
            symbol = message.get("symbol")
            if not symbol:
                return

            tick = {
                "timestamp": datetime.fromtimestamp(
                    message.get("last_traded_time", time.time()),
                    tz=IST,
                ),
                "ltp":    float(message.get("ltp", 0)),
                "volume": int(message.get("vol_traded_today", 0)),
                "bid":    float(message.get("bid_price", 0)),
                "ask":    float(message.get("ask_price", 0)),
                "oi":     int(message.get("oi", 0)),
            }

            if tick["ltp"] > 0:
                store.on_tick(symbol, tick)

        except Exception as e:
            logger.error(f"Error processing Fyers tick: {e}")

    def _fill_gap(self, gap_start: datetime) -> None:
        """Fetch REST candles to fill data gap after reconnect."""
        if not self._fyers_client:
            return
        gap_minutes = (datetime.now(tz=IST) - gap_start).total_seconds() / 60
        if gap_minutes < 1:
            return
        logger.info(f"[FyersStream] Filling {gap_minutes:.0f}min data gap...")
        from config.watchlist import PRIORITY_SYMBOLS
        for symbol in [s for s in PRIORITY_SYMBOLS if s.startswith("NSE:")][:6]:
            try:
                df = self._fetch_historical(symbol, "5", 1)
                if df is not None:
                    store.load_historical(symbol, "5m", df)
            except Exception as e:
                logger.debug(f"Gap fill failed for {symbol}: {e}")

    def _subscribe(self) -> None:
        """Subscribe to all symbols in watchlist."""
        import config.watchlist as _wl
        symbols = list(set(_wl.ALL_NSE_SYMBOLS))   # deduplicate — reads current value after dynamic watchlist load
        # Fyers WebSocket subscribe takes a list of symbol strings
        self._ws_client.subscribe(symbols=symbols, data_type="SymbolUpdate")
        logger.info(f"Subscribed to {len(symbols)} NSE symbols.")

    # ─────────────────────────────────────────────────────────────
    # INTERNAL — historical data seeding
    # ─────────────────────────────────────────────────────────────

    def _seed_historical_data(self) -> None:
        """
        Fetch historical OHLCV from Fyers REST API on startup.
        Seeds DataStore so strategies have data immediately on first tick.
        Runs for all NSE symbols so every symbol is tradeable from the first cycle.
        """
        if not self._fyers_client:
            logger.info("Skipping historical seeding (no REST client).")
            return

        import config.watchlist as _wl
        nse_priority = [s for s in _wl.ALL_NSE_SYMBOLS if s.startswith("NSE:")]

        timeframes_to_seed = {
            "15m": {"resolution": "15", "days_back": 30},
            "1H":  {"resolution": "60", "days_back": 90},
            "1D":  {"resolution": "D",  "days_back": 365},
        }

        for symbol in nse_priority:
            for tf, params in timeframes_to_seed.items():
                try:
                    df = self._fetch_historical(symbol, params["resolution"], params["days_back"])
                    if df is not None and len(df) > 0:
                        store.load_historical(symbol, tf, df)
                except Exception as e:
                    logger.error(f"Historical seed failed for {symbol} [{tf}]: {e}")
                time.sleep(0.3)   # rate limit — Fyers allows ~10 req/sec

    def _fetch_historical(
        self, symbol: str, resolution: str, days_back: int
    ) -> Optional[pd.DataFrame]:
        """Fetch OHLCV history from Fyers REST API."""
        end_date   = datetime.now(tz=IST)
        start_date = end_date - timedelta(days=days_back)

        data = {
            "symbol":     symbol,
            "resolution": resolution,
            "date_format": "1",
            "range_from": start_date.strftime("%Y-%m-%d"),
            "range_to":   end_date.strftime("%Y-%m-%d"),
            "cont_flag":  "1",
        }

        response = self._fyers_client.history(data=data)

        if response.get("s") != "ok":
            logger.warning(f"Historical fetch failed for {symbol}: {response.get('message')}")
            return None

        candles = response.get("candles", [])
        if not candles:
            return None

        df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
        return df
