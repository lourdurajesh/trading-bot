"""
main.py
───────
Master orchestrator. Boots all systems and runs the trading bot.

Startup sequence:
  1. Logging setup
  2. Database init (portfolio_tracker)
  3. Broker connections (Fyers + Alpaca)
  4. Data streams (WebSocket consumers)
  5. FastAPI dashboard server (background thread)
  6. Strategy evaluation loop (main loop)

Shutdown: Ctrl+C → graceful stop (cancel pending orders, save state)
"""

from dotenv import load_dotenv
load_dotenv(override=True)   # MUST be first — loads fresh token before any other import

import logging
import os
import signal
import sys
import time
import threading
from datetime import datetime, timezone

from config.settings import (
    API_HOST, API_PORT, BOT_MODE, LOG_DIR, LOG_LEVEL, NSE_OPEN, NSE_CLOSE,
    validate_env,
)

# ── Logging setup (before any imports that log) ───────────────────
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level    = getattr(logging, LOG_LEVEL, logging.INFO),
    format   = "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(f"{LOG_DIR}/bot.log", encoding="utf-8"),
    ]
)
logger = logging.getLogger("main")

# ── Core imports (after logging) ─────────────────────────────────
from data.data_store import store
from data.fyers_stream import FyersStream
from data.alpaca_stream import AlpacaStream
from execution.fyers_broker import fyers_broker
from execution.alpaca_broker import alpaca_broker
from risk.portfolio_tracker import portfolio_tracker
from risk.risk_manager import risk_manager
from strategies.strategy_selector import strategy_selector
from execution.position_manager import position_manager

# ── Evaluation loop config ────────────────────────────────────────
EVAL_INTERVAL_SECONDS  = 60   # slow loop — full signal evaluation
FAST_LOOP_INTERVAL     = 5    # fast loop — position monitoring
WARMUP_SECONDS         = 30   # wait for data streams to populate


class TradingBot:

    def __init__(self):
        self._running       = False
        self._fyers_stream  = FyersStream()
        self._alpaca_stream = AlpacaStream()

    # ─────────────────────────────────────────────────────────────
    # LIFECYCLE
    # ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        logger.info("=" * 60)
        logger.info("  AlphaLens Trading Bot — Starting")
        logger.info(f"  Mode: {BOT_MODE}")
        logger.info(f"  Time: {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
        logger.info("=" * 60)

        # Validate environment on every startup — catches misconfigurations early
        validate_env()

        # Audit trail — log bot start
        try:
            from audit_log import audit_log
            import os
            audit_log.bot_event("BOT_START", {
                "mode":           BOT_MODE,
                "paper_trading":  os.getenv("PAPER_TRADING", "false"),
            })
        except Exception:
            pass

        self._running = True

        # Register shutdown handler
        signal.signal(signal.SIGINT,  self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

        # Step 1: Init brokers
        logger.info("Connecting to brokers...")
        fyers_broker.initialise()
        alpaca_broker.initialise()

        # Init options engine (connects Fyers client for chain fetching)
        from analysis.options_engine import options_engine
        options_engine.initialise()


        # Step 2: Start data streams
        logger.info("Starting data streams...")
        self._fyers_stream.start()
        self._alpaca_stream.start()

        # Step 3: Start dashboard API in background thread
        self._start_dashboard()

        # Step 4: Load dynamic watchlist if available
        self._load_dynamic_watchlist()

        # Step 5: Warm up — wait for streams to fill data store
        logger.info(f"Warming up for {WARMUP_SECONDS} seconds...")
        time.sleep(WARMUP_SECONDS)

        # Step 5: Run main evaluation loop
        logger.info("Bot is live. Starting evaluation loop.")
        self._run_loop()

    def _shutdown(self, *args) -> None:
        logger.info("Shutdown signal received. Stopping bot...")
        self._running = False
        try:
            from audit_log import audit_log
            audit_log.bot_event("BOT_STOP")
        except Exception:
            pass
        # Stop streams with timeout
        try:
            self._fyers_stream.stop()
        except Exception:
            pass
        try:
            self._alpaca_stream.stop()
        except Exception:
            pass
        logger.info("Bot stopped.")
        # Force exit — don't wait for daemon threads
        os._exit(0)

    # ─────────────────────────────────────────────────────────────
    # MAIN LOOP
    # ─────────────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        """
        Two-loop architecture:
        Fast loop (5s)  — position monitoring, stop/target hits
        Slow loop (60s) — full signal evaluation + intelligence pipeline
        """
        last_slow_run = 0

        while self._running:
            try:
                if self._is_market_hours():
                    # ── Fast loop — runs every 5 seconds ──────────
                    position_manager.check_all()

                    # ── Slow loop — runs every 60 seconds ─────────
                    now = time.time()
                    if now - last_slow_run >= EVAL_INTERVAL_SECONDS:
                        last_slow_run = now
                        if strategy_selector._cycle_count % 10 == 0:
                            self._log_portfolio_snapshot()
                        strategy_selector.run_cycle()
                else:
                    logger.debug("Outside market hours — skipping.")

            except Exception as e:
                logger.error(f"Loop error: {e}", exc_info=True)

            # Fast sleep — Ctrl+C interrupts within 1 second
            for _ in range(FAST_LOOP_INTERVAL):
                if not self._running:
                    break
                time.sleep(1)

    # ─────────────────────────────────────────────────────────────
    # DASHBOARD
    # ─────────────────────────────────────────────────────────────

    def _start_dashboard(self) -> None:
        """Start FastAPI dashboard server in a daemon thread."""
        def run_api():
            try:
                import uvicorn
                from api.dashboard_api import app
                uvicorn.run(app, host=API_HOST, port=API_PORT, log_level="warning")
            except Exception as e:
                logger.error(f"Dashboard API failed to start: {e}")

        thread = threading.Thread(target=run_api, daemon=True, name="DashboardAPI")
        thread.start()
        logger.info(f"Dashboard API starting at http://{API_HOST}:{API_PORT}")

    # ─────────────────────────────────────────────────────────────
    # HELPERS
    # ─────────────────────────────────────────────────────────────

    def _load_dynamic_watchlist(self) -> None:
        """Load dynamic watchlist generated by nightly agent if available."""
        import json
        path = "db/dynamic_watchlist.json"
        if os.path.exists(path):
            try:
                with open(path) as f:
                    data = json.load(f)
                symbols = data.get("symbols", [])
                if symbols:
                    import config.watchlist as wl
                    wl.ALL_NSE_SYMBOLS = [s for s in symbols if s.startswith("NSE:")]
                    logger.info(f"Loaded dynamic watchlist: {len(wl.ALL_NSE_SYMBOLS)} NSE symbols")
            except Exception as e:
                logger.warning(f"Could not load dynamic watchlist: {e}")

    def _is_market_hours(self) -> bool:
        """
        Returns True if either NSE or US markets are open.
        Simple time-based check — extend with holiday calendar if needed.
        """
        from datetime import time as dtime
        now_ist = datetime.now()   # assumes server is in IST; adjust for UTC offset if needed
        current_time = now_ist.time()
        weekday = now_ist.weekday()   # 0=Mon … 6=Sun

        if weekday >= 5:   # weekend
            return False

        # NSE hours: 09:15 – 15:30 IST
        nse_open  = dtime(9, 15)
        nse_close = dtime(15, 30)
        if nse_open <= current_time <= nse_close:
            return True

        # US market hours in IST: ~19:00 – 01:30 (next day)
        us_open  = dtime(19, 0)
        us_close = dtime(23, 59)    # simplified — handles up to midnight
        if current_time >= us_open or current_time <= dtime(1, 30):
            return True

        return False

    def _log_portfolio_snapshot(self) -> None:
        """Log a brief portfolio status every 10 cycles."""
        stats = portfolio_tracker.get_stats()
        risk  = risk_manager.status()
        logger.info(
            f"[Portfolio] Value: ₹{stats['portfolio_value']:,.0f} | "
            f"P&L: ₹{stats['total_pnl']:+,.0f} ({stats['total_pnl_pct']:+.1f}%) | "
            f"Open: {stats['open_positions_count']} | "
            f"Win rate: {stats['win_rate']:.0f}% | "
            f"DD: {stats['drawdown_pct']:.1f}% | "
            f"Kill switch: {'ON' if risk['kill_switch_active'] else 'off'}"
        )


# ── Entry point ───────────────────────────────────────────────────
if __name__ == "__main__":
    bot = TradingBot()
    bot.start()
