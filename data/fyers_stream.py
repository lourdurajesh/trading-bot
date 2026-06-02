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

# Module-level set of Fyers symbols confirmed invalid by the broker (-300 error).
# Persists for the lifetime of the process so resubscribes and add-instrument
# validation can both consult it without circular imports.
#
# FyersStream._invalid_symbols is an alias of this same set object — any mutation
# (update / clear) via KNOWN_INVALID_SYMBOLS is immediately visible to the stream.
KNOWN_INVALID_SYMBOLS: set[str] = set()


# ── Module-level DB helpers ────────────────────────────────────────────────────
# Used by dashboard endpoints that don't have a reference to the stream instance.

_DB_PATH         = "db/trades.db"
_INVALID_SYM_KEY = "fyers.invalid_symbols"
_INVALID_SYM_TTL = 30   # days


def get_invalid_symbols_info() -> dict[str, str]:
    """
    Return all persisted invalid symbols with their first-seen ISO timestamp.
    Falls back to current in-memory set (no timestamps) if DB is unavailable.
    """
    try:
        import sqlite3, json
        with sqlite3.connect(_DB_PATH) as conn:
            row = conn.execute(
                "SELECT value FROM commodity_settings WHERE key=?",
                (_INVALID_SYM_KEY,),
            ).fetchone()
        return json.loads(row[0]) if row else {s: "" for s in KNOWN_INVALID_SYMBOLS}
    except Exception:
        return {s: "" for s in KNOWN_INVALID_SYMBOLS}


def clear_invalid_symbols() -> list[str]:
    """
    Clear all persisted invalid symbols from memory AND DB so they are retried
    on the next WebSocket subscription.

    Call after a Fyers plan upgrade, a symbol rename, or adding a new contract.
    """
    cleared = list(KNOWN_INVALID_SYMBOLS)
    KNOWN_INVALID_SYMBOLS.clear()
    try:
        import sqlite3
        with sqlite3.connect(_DB_PATH) as conn:
            conn.execute(
                "DELETE FROM commodity_settings WHERE key=?",
                (_INVALID_SYM_KEY,),
            )
        if cleared:
            logger.info(
                f"[FyersStream] Cleared {len(cleared)} invalid symbol(s) "
                f"from memory + DB: {cleared}"
            )
    except Exception as exc:
        logger.debug(f"[FyersStream] DB clear of invalid symbols failed: {exc}")
    return cleared


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
        self._reconnect_delay     = 5
        self._max_reconnects      = 10
        self._consecutive_failures = 0   # Bug 9: tracks consecutive reconnect failures
        self._gap_start            = None
        # Signals _connect() to exit its sleep loop when the socket closes unexpectedly
        self._ws_closed = threading.Event()
        # Alias to the module-level KNOWN_INVALID_SYMBOLS set — any mutation here
        # is immediately visible everywhere (dashboard, add-instrument validation,
        # resubscription logic) without needing a reference to this instance.
        self._invalid_symbols: set[str] = KNOWN_INVALID_SYMBOLS

    # ─────────────────────────────────────────────────────────────
    # PUBLIC
    # ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start streaming in a background thread."""
        if self._running:
            logger.warning("FyersStream already running.")
            return

        # Load previously-known invalid symbols from DB before the first subscribe
        # so the bot never re-tries symbols Fyers confirmed invalid on a prior run.
        self._load_invalid_from_db()

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
        """Main loop — connects and reconnects on disconnect with exponential backoff."""
        self._init_rest_client()
        self._seed_historical_data()

        attempt = 0
        while self._running:
            logger.info(f"[FyersStream] Connecting (attempt {attempt + 1})...")
            self._connect()
            if not self._running:
                break
            attempt += 1
            self._consecutive_failures += 1
            delay = min(self._reconnect_delay * (2 ** min(attempt - 1, 4)), 60)
            logger.warning(
                f"[FyersStream] WebSocket closed — reconnecting in {delay}s "
                f"(#{attempt}, consecutive failures: {self._consecutive_failures})"
            )

            # Bug 9: alert and set system health when feed stays down too long
            if self._consecutive_failures >= self._max_reconnects:
                msg = (
                    f"🔴 Fyers data feed DEAD — {consecutive_failures} consecutive "
                    f"reconnect failures. Position monitoring is on STALE prices. "
                    f"Check NSE/Fyers status immediately."
                )
                logger.critical(f"[FyersStream] {msg}")
                try:
                    from system_health import system_health
                    system_health.set_alert("fyers_feed", msg, severity="critical")
                except Exception:
                    pass
                try:
                    from notifications.alert_service import alert_service
                    alert_service.info(msg)
                except Exception:
                    pass

            time.sleep(delay)

    def _on_connect_success(self) -> None:
        """Called when WebSocket reconnects successfully — clears the feed-dead alert."""
        try:
            from system_health import system_health
            system_health.clear_alert("fyers_feed")
        except Exception:
            pass

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
        """Create WebSocket client and start streaming. Returns when WS closes."""
        if not settings.FYERS_ACCESS_TOKEN:
            logger.error("Cannot connect: FYERS_ACCESS_TOKEN is empty.")
            return

        self._ws_closed.clear()
        self._ws_client = data_ws.FyersDataSocket(
            access_token=settings.FYERS_ACCESS_TOKEN,
            log_path="logs/",
            litemode=True,
            write_to_file=False,
            reconnect=False,        # we handle reconnect in _run()
            on_connect=self._on_connect,
            on_close=self._on_close,
            on_error=self._on_error,
            on_message=self._on_message,
        )
        self._ws_client.connect()
        # Block until shutdown OR socket closes (set by _on_close)
        while self._running and not self._ws_closed.is_set():
            time.sleep(1)

    # ─────────────────────────────────────────────────────────────
    # INTERNAL — WebSocket callbacks
    # ─────────────────────────────────────────────────────────────

    def _on_connect(self) -> None:
        logger.info("Fyers WebSocket connected. Subscribing to symbols...")
        self._subscribe()
        self.subscribe_open_option_positions()
        # Successful connection — reset failure counter and clear any dead-feed alert (Bug 9)
        if self._consecutive_failures > 0:
            self._consecutive_failures = 0
            self._on_connect_success()
        # Fill any data gap since last disconnect
        if hasattr(self, "_gap_start") and self._gap_start:
            self._fill_gap(self._gap_start)
            self._gap_start = None

    def _on_close(self, code: int = 0) -> None:
        if self._running:
            logger.warning(f"Fyers WebSocket closed: [{code}] — will reconnect")
            self._gap_start = datetime.now(tz=IST)
            self._ws_closed.set()   # wake up _connect()'s sleep loop
        else:
            logger.info("FyersStream: closed cleanly on shutdown.")

    # ── Invalid-symbol persistence ────────────────────────────────
    # Symbols confirmed invalid by Fyers (-300) are stored in the DB with a
    # timestamp.  On the next restart they are excluded from the initial
    # subscription so the ERROR / WARNING never re-fires for the same symbols.
    # TTL: 30 days — symbols are retried after that (handles plan upgrades,
    # new contracts being listed, etc.).

    def _load_invalid_from_db(self) -> None:
        """Seed _invalid_symbols (= KNOWN_INVALID_SYMBOLS) from DB at startup."""
        try:
            import sqlite3, json
            from datetime import datetime, timedelta
            cutoff = (datetime.now() - timedelta(days=_INVALID_SYM_TTL)).isoformat()
            with sqlite3.connect(_DB_PATH) as conn:
                row = conn.execute(
                    "SELECT value FROM commodity_settings WHERE key=?",
                    (_INVALID_SYM_KEY,),
                ).fetchone()
            if not row:
                return
            data = json.loads(row[0])   # {"symbol": "iso-timestamp", ...}
            fresh = {sym for sym, ts in data.items() if ts >= cutoff}
            if fresh:
                # self._invalid_symbols IS KNOWN_INVALID_SYMBOLS — one update suffices
                self._invalid_symbols.update(fresh)
                logger.info(
                    f"[FyersStream] Loaded {len(fresh)} persisted invalid symbol(s) "
                    f"from DB — will skip on subscribe: {sorted(fresh)}"
                )
        except Exception as exc:
            logger.debug(f"[FyersStream] Could not load invalid symbols from DB: {exc}")

    def _save_invalid_to_db(self) -> None:
        """Persist current invalid symbols to DB so they survive restarts."""
        try:
            import sqlite3, json
            from datetime import datetime, timedelta
            cutoff = (datetime.now() - timedelta(days=_INVALID_SYM_TTL)).isoformat()
            existing: dict[str, str] = {}
            with sqlite3.connect(_DB_PATH) as conn:
                row = conn.execute(
                    "SELECT value FROM commodity_settings WHERE key=?",
                    (_INVALID_SYM_KEY,),
                ).fetchone()
                if row:
                    existing = json.loads(row[0])
            existing = {sym: ts for sym, ts in existing.items() if ts >= cutoff}
            now_iso = datetime.now().isoformat()
            for sym in self._invalid_symbols:
                existing.setdefault(sym, now_iso)   # first-seen timestamp wins
            with sqlite3.connect(_DB_PATH) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO commodity_settings (key, value) VALUES (?,?)",
                    (_INVALID_SYM_KEY, json.dumps(existing)),
                )
        except Exception as exc:
            logger.debug(f"[FyersStream] Could not persist invalid symbols to DB: {exc}")

    # ─────────────────────────────────────────────────────────────

    def _on_error(self, error) -> None:
        logger.error(f"Fyers WebSocket error: {error}")
        if isinstance(error, dict) and error.get("code") == -300:
            invalid = error.get("invalid_symbols", [])
            if invalid:
                self._invalid_symbols.update(invalid)
                # self._invalid_symbols IS KNOWN_INVALID_SYMBOLS — no double-update needed
                self._save_invalid_to_db()   # persist so restart skips these symbols
                logger.warning(
                    f"[FyersStream] Invalid symbols dropped and persisted: {invalid}. "
                    f"Resubscribing without them."
                )
                # Resubscribe immediately with the bad symbols removed
                threading.Thread(
                    target=self._resubscribe_clean,
                    daemon=True,
                    name="FyersResubscribe",
                ).start()
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
        """Subscribe to NSE + MCX symbols, excluding any known-invalid symbols."""
        import config.watchlist as _wl
        from config.learning_watchlist import LEARNING_NSE_EQUITIES
        from commodity_options_learning import _fyers_sym, ALL_MCX_SHORTS
        mcx_symbols = [_fyers_sym(s) for s in ALL_MCX_SHORTS]
        # Union of production watchlist AND learning watchlist — the learning engine
        # evaluates symbols not in the production list (e.g. WIPRO, HCLTECH, ONGC)
        # and needs tick data for them to function.
        nse_symbols = list(set(_wl.ALL_NSE_SYMBOLS + LEARNING_NSE_EQUITIES))
        all_symbols = list(set(nse_symbols + mcx_symbols))
        # Drop symbols already confirmed invalid by a prior -300 error
        if self._invalid_symbols:
            before = len(all_symbols)
            all_symbols = [s for s in all_symbols if s not in self._invalid_symbols]
            if before > len(all_symbols):
                logger.info(f"[FyersStream] Skipped {before - len(all_symbols)} known-invalid symbol(s).")
        self._ws_client.subscribe(symbols=all_symbols, data_type="SymbolUpdate")
        logger.info(
            f"Subscribed to {len(all_symbols)} symbols — "
            f"NSE: {len(nse_symbols)}, MCX: {len(mcx_symbols)} {mcx_symbols}"
        )

    def _resubscribe_clean(self) -> None:
        """Called after a -300 error to re-issue subscription without invalid symbols."""
        try:
            if self._ws_client is None:
                return
            import config.watchlist as _wl
            from config.learning_watchlist import LEARNING_NSE_EQUITIES
            from commodity_options_learning import _fyers_sym, ALL_MCX_SHORTS
            mcx_symbols = [_fyers_sym(s) for s in ALL_MCX_SHORTS]
            nse_symbols = set(_wl.ALL_NSE_SYMBOLS + LEARNING_NSE_EQUITIES)
            all_symbols = [
                s for s in set(nse_symbols) | set(mcx_symbols)
                if s not in self._invalid_symbols
            ]
            self._ws_client.subscribe(symbols=all_symbols, data_type="SymbolUpdate")
            logger.info(f"[FyersStream] Resubscribed to {len(all_symbols)} valid symbols.")
        except Exception as e:
            logger.error(f"[FyersStream] Resubscribe failed: {e}")

    def subscribe_extra(self, symbols: list) -> None:
        """Subscribe to additional symbols that aren't in the static watchlist.

        Used for options NFO contracts (e.g. NSE:NIFTY2562524750CE) so that
        position_manager can read live option premium LTP from the data store.
        """
        if not self._running or self._ws_client is None:
            return
        clean = [s for s in symbols if s and s not in self._invalid_symbols]
        if not clean:
            return
        try:
            self._ws_client.subscribe(symbols=clean, data_type="SymbolUpdate")
            logger.info(f"[FyersStream] Extra subscribe (options NFO): {clean}")
        except Exception as e:
            logger.warning(f"[FyersStream] Extra subscribe failed: {e}")

    def subscribe_open_option_positions(self) -> None:
        """Subscribe NFO symbols for any already-open options positions on startup.

        Reads monitor_symbol and entry_legs from each open position so that
        position_manager has live LTP data for stop/target checks immediately
        after reconnect.
        """
        try:
            from risk.portfolio_tracker import portfolio_tracker
            positions = portfolio_tracker.get_open_positions()
            nfo_syms = []
            for pos in positions:
                # monitor_symbol: single symbol for single-leg strategies
                ms = pos.get("monitor_symbol")
                if ms and ms not in self._invalid_symbols:
                    nfo_syms.append(ms)
                # entry_legs: covers multi-leg strategies (short strangle, etc.)
                meta = pos.get("options_meta") or {}
                for leg in meta.get("entry_legs", []):
                    s = leg.get("symbol")
                    if s and s not in self._invalid_symbols:
                        nfo_syms.append(s)
                # monitor_symbols: explicit list for sum-combine positions
                for s in meta.get("monitor_symbols", []):
                    if s and s not in self._invalid_symbols:
                        nfo_syms.append(s)
            unique = list(dict.fromkeys(nfo_syms))  # deduplicate preserving order
            if unique:
                self.subscribe_extra(unique)
        except Exception as e:
            logger.warning(f"[FyersStream] Could not subscribe open option positions: {e}")

    def refresh_mcx_subscriptions(self) -> None:
        """Subscribe to any new MCX symbols and seed their history.

        Called by CommodityOptionsLearning when a rollover change shifts the
        active contract mid-run (e.g. SILVER26MAYFUT → SILVER26JULFUT).
        Runs in a background thread so it never blocks the evaluation loop.
        """
        if not self._running or self._ws_client is None:
            return
        threading.Thread(
            target=self._do_refresh_mcx,
            daemon=True,
            name="MCXRefresh",
        ).start()

    def _do_refresh_mcx(self) -> None:
        import config.watchlist as _wl
        from config.learning_watchlist import LEARNING_NSE_EQUITIES
        from commodity_options_learning import _fyers_sym, ALL_MCX_SHORTS

        mcx_symbols = [_fyers_sym(s) for s in ALL_MCX_SHORTS]
        new_symbols  = [s for s in mcx_symbols if s not in self._invalid_symbols and store.get_ltp(s) is None]

        if not new_symbols:
            return

        logger.info(f"[FyersStream] MCX refresh — new symbols: {new_symbols}")

        # Re-subscribe to full set (includes new symbols, keeps existing ones active)
        try:
            nse_all = set(_wl.ALL_NSE_SYMBOLS + LEARNING_NSE_EQUITIES)
            all_symbols = list({s for s in nse_all | set(mcx_symbols) if s not in self._invalid_symbols})
            self._ws_client.subscribe(symbols=all_symbols, data_type="SymbolUpdate")
            logger.info(f"[FyersStream] MCX refresh — resubscribed {len(all_symbols)} symbols")
        except Exception as e:
            logger.error(f"[FyersStream] MCX refresh subscribe failed: {e}")

        if not self._fyers_client:
            return

        for sym in new_symbols:
            try:
                df = self._fetch_historical(sym, "60", 90)
                if df is not None and len(df) > 0:
                    store.load_historical(sym, "1H", df)
                    logger.info(f"[FyersStream] MCX refresh — seeded {len(df)} 1H bars for {sym}")
                else:
                    logger.warning(f"[FyersStream] MCX refresh — no history returned for {sym}")
            except Exception as e:
                logger.warning(f"[FyersStream] MCX refresh seed failed for {sym}: {e}")
            time.sleep(0.3)

    # ─────────────────────────────────────────────────────────────
    # INTERNAL — historical data seeding
    # ─────────────────────────────────────────────────────────────

    def _seed_historical_data(self) -> None:
        """
        Fetch historical OHLCV from Fyers REST API on startup.
        Seeds DataStore so strategies have data immediately on first tick.
        Runs for NSE and MCX symbols so every symbol is tradeable from the first cycle.
        """
        if not self._fyers_client:
            logger.info("Skipping historical seeding (no REST client).")
            return

        import config.watchlist as _wl
        from commodity_options_learning import _fyers_sym, ALL_MCX_SHORTS

        nse_priority = [s for s in _wl.ALL_NSE_SYMBOLS if s.startswith("NSE:")]

        nse_timeframes = {
            "15m": {"resolution": "15", "days_back": 30},
            "1H":  {"resolution": "60", "days_back": 90},
            "1D":  {"resolution": "D",  "days_back": 365},
        }

        # Equities — silent skip on empty is acceptable (many mid-caps have thin history)
        equity_symbols = [s for s in nse_priority if "-INDEX" not in s]
        for symbol in equity_symbols:
            for tf, params in nse_timeframes.items():
                try:
                    df = self._fetch_historical(symbol, params["resolution"], params["days_back"])
                    if df is not None and len(df) > 0:
                        store.load_historical(symbol, tf, df)
                except Exception as e:
                    logger.error(f"Historical seed failed for {symbol} [{tf}]: {e}")
                time.sleep(0.3)   # rate limit — Fyers allows ~10 req/sec

        # Indices — seeded separately so failures are always visible.
        # Regime detector needs ≥50 1H candles; without them all index options strategies
        # return regime=UNKNOWN and never fire.
        from config.watchlist import NSE_INDICES
        logger.info(f"[FyersStream] Seeding index historical data for {NSE_INDICES} ...")
        for symbol in NSE_INDICES:
            for tf, params in nse_timeframes.items():
                try:
                    df = self._fetch_historical(symbol, params["resolution"], params["days_back"])
                    if df is not None and len(df) > 0:
                        store.load_historical(symbol, tf, df)
                        logger.info(f"[FyersStream] Index seed OK: {symbol} [{tf}] — {len(df)} candles")
                    else:
                        logger.warning(
                            f"[FyersStream] Index seed EMPTY: {symbol} [{tf}] — "
                            f"regime detector will return UNKNOWN until live ticks build candles"
                        )
                except Exception as e:
                    logger.error(f"[FyersStream] Index seed FAILED: {symbol} [{tf}]: {e}")
                time.sleep(0.3)

        # Seed MCX futures — 1H candles are enough for the EMA/RSI trend check
        logger.info("Seeding MCX historical data...")
        for short in ALL_MCX_SHORTS:
            symbol = _fyers_sym(short)
            try:
                df = self._fetch_historical(symbol, "60", 90)
                if df is not None and len(df) > 0:
                    store.load_historical(symbol, "1H", df)
                    logger.info(f"[MCX] Seeded {len(df)} 1H candles for {symbol}")
                else:
                    logger.warning(f"[MCX] No historical data returned for {symbol}")
            except Exception as e:
                logger.error(f"[MCX] Historical seed failed for {symbol}: {e}")
            time.sleep(0.3)

    def _fetch_historical(
        self, symbol: str, resolution: str, days_back: int
    ) -> Optional[pd.DataFrame]:
        """Fetch OHLCV history from Fyers REST API."""
        end_date   = datetime.now(tz=IST)
        start_date = end_date - timedelta(days=days_back)

        # cont_flag="1" is only meaningful for futures continuous contracts.
        # Fyers rejects or returns empty data for index/equity symbols when it is set.
        is_index = "-INDEX" in symbol
        data = {
            "symbol":     symbol,
            "resolution": resolution,
            "date_format": "1",
            "range_from": start_date.strftime("%Y-%m-%d"),
            "range_to":   end_date.strftime("%Y-%m-%d"),
            "cont_flag":  "0" if is_index else "1",
        }

        response = self._fyers_client.history(data=data)

        if response.get("s") != "ok":
            logger.warning(f"Historical fetch failed for {symbol}: {response.get('message')}")
            return None

        candles = response.get("candles", [])
        if not candles:
            return None

        df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
        return df
