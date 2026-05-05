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
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

from config.settings import (
    API_HOST, API_PORT, BOT_MODE, LOG_DIR, LOG_LEVEL, NSE_OPEN, NSE_CLOSE,
    validate_env,
)

# ── Logging setup (before any imports that log) ───────────────────
os.makedirs(LOG_DIR, exist_ok=True)

from config.logging_ist import setup_logging
setup_logging(
    level    = getattr(logging, LOG_LEVEL, logging.INFO),
    fmt      = "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    log_file = f"{LOG_DIR}/bot.log",
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

# ── Institutional F&O system schedule ────────────────────────────
from datetime import time as dtime
_PREMARKT_SCORE_TIME   = dtime(9,  0)   # run conviction_scorer once before open
_PREMARKT_SCORE_END    = dtime(9, 30)   # window closes at 9:30
_OI_CLOSE_SNAP_TIME    = dtime(15, 25)  # save OI snapshot 5 min before close
_NSE_COLLECT_TIME      = dtime(17, 30)  # collect FII participant data after publish
_NSE_COLLECT_END       = dtime(18, 30)  # retry window closes at 6:30 PM


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
        logger.info(f"  Time: {datetime.now(tz=IST).strftime('%Y-%m-%d %H:%M:%S IST')}")
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

        # Init OI analyzer (same Fyers session, must come after broker init)
        from analysis.oi_analyzer import oi_analyzer
        oi_analyzer.initialise()


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
                        — also runs learning paper trades on every cycle

        Timed hooks (run once per day at specific times):
          09:00–09:30  conviction_scorer.score()   — pre-market F&O conviction
          15:25        oi_analyzer.save_close_snapshot()
          17:30–18:30  nse_participant_collector.collect()  — FII daily data
        """
        from learning_engine import learning_engine
        from commodity_options_learning import commodity_options
        from analysis.oi_analyzer import oi_analyzer
        from intelligence.conviction_scorer import conviction_scorer
        from intelligence.nse_participant_collector import nse_participant_collector

        last_slow_run        = 0
        last_commodity_run   = 0
        _conviction_scored_date  = None   # date when score was last computed
        _oi_snap_saved_date      = None   # date when OI close snapshot was saved
        _nse_collected_date      = None   # date when FII data was collected

        while self._running:
            try:
                now      = time.time()
                now_ist  = datetime.now(tz=IST)
                now_time = now_ist.time()
                today    = now_ist.date()

                # ── Pre-market conviction scoring (09:00–09:30 IST) ──
                if (_PREMARKT_SCORE_TIME <= now_time <= _PREMARKT_SCORE_END
                        and today != _conviction_scored_date
                        and now_ist.weekday() < 5):
                    try:
                        result = conviction_scorer.score("BANKNIFTY")
                        _conviction_scored_date = today
                        logger.info(
                            f"[Main] Conviction score: {result.score:+d} "
                            f"{result.direction} — "
                            f"{'TRADE DAY' if result.tradeable else 'no trade (below threshold)'}"
                        )
                    except Exception as e:
                        logger.error(f"[Main] Conviction scorer error: {e}")

                if self._is_market_hours():
                    # ── Fast loop — runs every 5 seconds ──────────
                    position_manager.check_all()

                    # ── Slow loop — runs every 60 seconds ─────────
                    if now - last_slow_run >= EVAL_INTERVAL_SECONDS:
                        last_slow_run = now

                        if strategy_selector._cycle_count % 10 == 0:
                            self._log_portfolio_snapshot()

                        # Refresh OI analyzer (feeds conviction_scorer OI signal)
                        try:
                            oi_analyzer.refresh("BANKNIFTY")
                            oi_analyzer.refresh("NIFTY")
                        except Exception as e:
                            logger.debug(f"[Main] OI analyzer refresh error: {e}")

                        # Production strategies (institutional_momentum is highest priority)
                        strategy_selector.run_cycle()

                        # NSE learning paper trades — parallel, isolated
                        try:
                            learning_engine.run_cycle()
                        except Exception as le:
                            logger.debug(f"Learning cycle error: {le}")

                    # ── OI close snapshot at 15:25 IST ────────────
                    if (now_time >= _OI_CLOSE_SNAP_TIME
                            and today != _oi_snap_saved_date
                            and now_ist.weekday() < 5):
                        try:
                            oi_analyzer.save_close_snapshot()
                            _oi_snap_saved_date = today
                            logger.info("[Main] OI close snapshot saved")
                        except Exception as e:
                            logger.error(f"[Main] OI snapshot save error: {e}")

                else:
                    logger.debug("Outside market hours — skipping.")

                # ── NSE FII data collection at 17:30 IST ──────────
                if (_NSE_COLLECT_TIME <= now_time <= _NSE_COLLECT_END
                        and today != _nse_collected_date
                        and now_ist.weekday() < 5):
                    try:
                        rows = nse_participant_collector.collect()
                        _nse_collected_date = today
                        if rows:
                            score, reason = nse_participant_collector.get_fii_signal()
                            logger.info(
                                f"[Main] FII data collected: "
                                f"net={rows[0].fii_net:+,} "
                                f"change={rows[0].fii_net_change:+,} "
                                f"signal={score:+d}"
                            )
                        else:
                            logger.warning("[Main] FII data unavailable today (holiday?)")
                    except Exception as e:
                        logger.error(f"[Main] NSE collector error: {e}")

                # Commodity options learning — separate 60s cadence,
                # MCX hours gate is enforced internally in run_cycle()
                if now - last_commodity_run >= EVAL_INTERVAL_SECONDS:
                    last_commodity_run = now
                    try:
                        commodity_options.run_cycle()
                    except Exception as ce:
                        logger.debug(f"Commodity options cycle error: {ce}")

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
        Returns True only during NSE trading hours (09:15 – 15:30 IST)
        on non-weekend, non-holiday days.
        """
        from datetime import time as dtime
        from config.market_holidays import is_trading_holiday
        now_ist = datetime.now(tz=IST)
        current_time = now_ist.time()
        weekday = now_ist.weekday()   # 0=Mon … 6=Sun

        if weekday >= 5:
            return False

        if is_trading_holiday(now_ist.date()):
            return False

        nse_open  = dtime(9, 15)
        nse_close = dtime(15, 30)
        return nse_open <= current_time <= nse_close

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
