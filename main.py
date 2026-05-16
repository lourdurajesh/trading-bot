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
_PREMARKT_IEP_TIME     = dtime(9, 10)   # re-score with IEP after NSE call auction finalises (9:08)
_PREMARKT_SCORE_END    = dtime(9, 30)   # window closes at 9:30
_OI_CLOSE_SNAP_TIME    = dtime(15, 25)  # save OI snapshot 5 min before close
_NSE_COLLECT_TIME      = dtime(17, 30)  # collect FII participant data after publish
_NSE_COLLECT_END       = dtime(18, 30)  # retry window closes at 6:30 PM
_EDGE_MONITOR_TIME     = dtime(8, 45)   # weekly edge check every Monday before market
_ANALYTICS_TIME        = dtime(15, 35)  # daily trade analytics report after close
_TOKEN_PREREFRESH_TIME = dtime(5, 30)   # proactive Fyers token refresh — expires at ~6 AM daily


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

        # Auto-refresh token before first init — covers the case where the token
        # expired overnight and nobody ran generate_token.py manually.
        from token_manager import token_manager
        fyers_broker.initialise()
        if not fyers_broker._initialised:
            logger.info("Fyers token invalid at startup — attempting auto-refresh...")
            token_manager.check_and_refresh_if_needed()
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

        # Wire MCX resubscription: when a rollover param change shifts the active
        # contract mid-run, commodity_options notifies the stream to resubscribe
        # and seed history for the new symbol without requiring a bot restart.
        from commodity_options_learning import commodity_options
        commodity_options._on_instrument_update = self._fyers_stream.refresh_mcx_subscriptions

        # Step 3: Start dashboard API in background thread
        self._start_dashboard()

        # Step 4: Load dynamic watchlist if available
        self._load_dynamic_watchlist()

        # Step 5: Warm up — wait for streams to fill data store
        logger.info(f"Warming up for {WARMUP_SECONDS} seconds...")
        time.sleep(WARMUP_SECONDS)

        # Step 6: Reconcile any PENDING_CLOSE positions from a previous crash (Bug 1)
        self._reconcile_pending_closes()

        # Step 7: Catch-up conviction score if bot started mid-day (after 9:30 AM)
        self._catch_up_conviction_score()

        # Step 7: Run main evaluation loop
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
        # sys.exit raises SystemExit which allows atexit handlers and finally blocks to run;
        # os._exit(0) would bypass them and could truncate in-flight DB writes.
        sys.exit(0)

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
        last_token_check     = 0          # periodic token health check
        TOKEN_CHECK_INTERVAL = 1800       # check token every 30 min
        _conviction_scored_date  = None   # date when score was last computed
        _conviction_iep_date     = None   # date when IEP-refined score was computed
        _oi_snap_saved_date      = None   # date when OI close snapshot was saved
        _nse_collected_date      = None   # date when FII data was collected
        _edge_monitor_week       = None   # ISO week when edge monitor last ran
        _analytics_saved_date    = None   # date when analytics report was saved
        _token_prerefreshed_date = None   # date of proactive 5:30 AM token refresh

        while self._running:
            try:
                now      = time.time()
                now_ist  = datetime.now(tz=IST)
                now_time = now_ist.time()
                today    = now_ist.date()

                # ── Proactive token refresh at 5:30 AM ───────────────
                # Fyers tokens expire at ~6:00 AM IST daily. The 30-min heartbeat
                # is reactive and would only catch this at 6:15 AM, leaving a
                # 15-minute gap where every API call fails. Force a refresh at
                # 5:30 AM so the token is renewed before expiry.
                if (now_time >= _TOKEN_PREREFRESH_TIME
                        and today != _token_prerefreshed_date):
                    _token_prerefreshed_date = today
                    try:
                        from token_manager import token_manager
                        logger.info("[Main] Proactive 5:30 AM token refresh starting...")
                        ok = token_manager.force_refresh("Proactive pre-6AM renewal")
                        if ok:
                            logger.info("[Main] Token pre-refresh successful — ready for market open")
                        else:
                            logger.warning("[Main] Token pre-refresh failed — will retry via 30-min heartbeat")
                    except Exception as te:
                        logger.error(f"[Main] Token pre-refresh error: {te}")

                # ── Edge monitor — every Monday at 08:45 IST ──────────
                current_week = now_ist.isocalendar()[1]
                if (now_ist.weekday() == 0
                        and now_time >= _EDGE_MONITOR_TIME
                        and current_week != _edge_monitor_week):
                    try:
                        from analysis.edge_monitor import edge_monitor
                        report = edge_monitor.run(send_alert=True)
                        _edge_monitor_week = current_week
                        logger.info(
                            f"[Main] Edge monitor: {report.overall_status}  "
                            f"({sum(1 for s in report.strategies if s.status != 'OK')} degraded)"
                        )
                    except Exception as e:
                        logger.error(f"[Main] Edge monitor error: {e}")

                # ── Pre-market conviction scoring (09:00–09:30 IST) ──
                if (_PREMARKT_SCORE_TIME <= now_time <= _PREMARKT_SCORE_END
                        and today != _conviction_scored_date
                        and now_ist.weekday() < 5):
                    try:
                        result = conviction_scorer.score("BANKNIFTY")
                        _conviction_scored_date = today
                        logger.info(
                            f"[Main] Conviction score (initial): {result.score:+d} "
                            f"{result.direction} IEP={result.iep_score:+d} — "
                            f"{'TRADE DAY' if result.tradeable else 'no trade (below threshold)'}"
                        )
                    except Exception as e:
                        logger.error(f"[Main] Conviction scorer error: {e}")

                # ── IEP-refined re-score at 09:10 IST ────────────────
                # NSE call auction finalises at ~9:08 AM. Re-compute the
                # conviction score at 9:10 AM so the IEP gap signal is included
                # before InstitutionalMomentum starts evaluating at 9:30 AM.
                if (_PREMARKT_IEP_TIME <= now_time <= _PREMARKT_SCORE_END
                        and today != _conviction_iep_date
                        and now_ist.weekday() < 5):
                    try:
                        result = conviction_scorer.score("BANKNIFTY")
                        _conviction_iep_date = today
                        logger.info(
                            f"[Main] Conviction score (IEP-refined): {result.score:+d} "
                            f"{result.direction} IEP={result.iep_score:+d} — "
                            f"{'TRADE DAY' if result.tradeable else 'no trade (below threshold)'}"
                        )
                    except Exception as e:
                        logger.error(f"[Main] IEP re-score error: {e}")

                # MCX exit check — runs every 5 seconds regardless of NSE hours
                # (MCX trades 09:00–23:30, well outside NSE session)
                try:
                    commodity_options.check_exits()
                except Exception as _ce:
                    logger.debug(f"[CommOpts] exit check error: {_ce}")

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

                # ── Daily trade analytics report at 15:35 IST ────────
                if (now_time >= _ANALYTICS_TIME
                        and today != _analytics_saved_date
                        and now_ist.weekday() < 5):
                    try:
                        from analysis.trade_analytics import trade_analytics
                        path = trade_analytics.save_report()
                        _analytics_saved_date = today
                        logger.info(f"[Main] Trade analytics saved: {path}")
                    except Exception as e:
                        logger.debug(f"[Main] Analytics save error: {e}")

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

                # ── Periodic Fyers token health check (every 30 min) ───
                if now - last_token_check >= TOKEN_CHECK_INTERVAL:
                    last_token_check = now
                    try:
                        from token_manager import token_manager
                        token_manager.check_and_refresh_if_needed()
                    except Exception as te:
                        logger.debug(f"[Main] Token check error: {te}")

                # Commodity options learning — separate 60s cadence,
                # MCX hours gate is enforced internally in run_cycle()
                if now - last_commodity_run >= EVAL_INTERVAL_SECONDS:
                    last_commodity_run = now
                    try:
                        commodity_options.run_cycle()
                    except Exception as ce:
                        logger.exception(f"[CommOpts] Cycle error: {ce}")

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

    def _reconcile_pending_closes(self) -> None:
        """
        Resolve any positions left in PENDING_CLOSE status from a previous crash (Bug 1).
        PENDING_CLOSE means: the exit order was placed at the broker before the crash,
        but portfolio_tracker.close_position() never ran. We check broker positions:
          - If broker still shows the position as open: revert to OPEN (order was cancelled
            or rejected during the crash) and let position_manager monitor it normally.
          - If broker no longer shows it: treat as closed and mark it CLOSED in DB.
        """
        # Collect symbols whose Position object has status=PENDING_CLOSE
        pending_symbols = [
            symbol for symbol, pos in portfolio_tracker._open_positions.items()
            if pos.status == "PENDING_CLOSE"
        ]
        if not pending_symbols:
            return

        logger.warning(
            f"[Main] Found {len(pending_symbols)} PENDING_CLOSE position(s) from previous crash — "
            f"reconciling with broker..."
        )
        pending = [{"symbol": s} for s in pending_symbols]

        try:
            discrepancies = fyers_broker.reconcile_positions()
        except Exception as e:
            logger.error(f"[Main] Could not fetch broker positions for reconciliation: {e}")
            discrepancies = {}

        for pos_dict in pending:
            symbol = pos_dict["symbol"]
            pos = portfolio_tracker.get_position(symbol)
            if not pos:
                continue

            broker_has_position = symbol not in discrepancies or \
                discrepancies.get(symbol, {}).get("issue") != "in_db_not_broker"

            if broker_has_position:
                # Broker still holds the position — the exit order probably didn't go through
                pos.status = "OPEN"
                portfolio_tracker._update_position_db(pos)
                logger.warning(
                    f"[Main] PENDING_CLOSE reverted to OPEN for {symbol}: "
                    f"broker still holds position. Exit will be retried by position_manager."
                )
            else:
                # Broker no longer has the position — exit completed during crash
                ltp = store.get_ltp(symbol) or pos.entry_price
                portfolio_tracker.close_position(symbol, ltp, "CRASH_RECONCILED")
                logger.info(
                    f"[Main] PENDING_CLOSE resolved for {symbol}: "
                    f"broker confirms exit. Closed at ≈₹{ltp:.2f}."
                )
                try:
                    from notifications.alert_service import alert_service
                    alert_service.info(
                        f"🔄 {symbol}: crash-exit reconciled — position closed at ≈₹{ltp:.2f}."
                    )
                except Exception:
                    pass

    def _catch_up_conviction_score(self) -> None:
        """
        If the bot starts (or restarts) mid-day after the 9:00–9:30 AM scoring
        window, InstitutionalMomentum will find _last_score=None and skip all
        index signals for the rest of the day. Run a catch-up score immediately
        so the strategy can trade during the remaining session.
        """
        from datetime import time as dtime
        from intelligence.conviction_scorer import conviction_scorer
        now_ist  = datetime.now(tz=IST)
        now_time = now_ist.time()
        if (dtime(9, 30) <= now_time <= dtime(14, 30)
                and now_ist.weekday() < 5
                and conviction_scorer.get_last_score() is None):
            logger.info("[Main] Bot started mid-day — computing catch-up conviction score...")
            try:
                result = conviction_scorer.score("BANKNIFTY")
                logger.info(
                    f"[Main] Catch-up conviction: {result.score:+d} {result.direction} — "
                    f"{'TRADE DAY' if result.tradeable else 'no trade (below threshold)'}"
                )
            except Exception as e:
                logger.error(f"[Main] Catch-up conviction score failed: {e}")

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
