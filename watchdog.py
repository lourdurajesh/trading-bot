"""
watchdog.py
───────────
Monitors the trading bot and handles:
  1. Crash detection and auto-restart of main.py
  2. Token auto-refresh at 11:45 PM daily
  3. Position reconciliation after restart
  4. Telegram alert on every crash + restart

Run this INSTEAD of main.py:
    python watchdog.py

It starts main.py as a subprocess and monitors it continuously.
"""

import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

from config.logging_ist import setup_logging
setup_logging(
    level    = logging.INFO,
    fmt      = "%(asctime)s [%(levelname)s] watchdog: %(message)s",
    log_file = "logs/watchdog.log",
)
logger = logging.getLogger("watchdog")

PYTHON          = sys.executable
BOT_SCRIPT      = os.path.join(os.path.dirname(__file__), "main.py")
TOKEN_SCRIPT    = os.path.join(os.path.dirname(__file__), "generate_token.py")
RESTART_DELAY_BASE = 10  # base delay in seconds (doubles each crash: 10, 20, 40, 80…)
RESTART_DELAY_MAX  = 300 # cap at 5 minutes
MAX_RESTARTS       = 20  # increased — exponential backoff makes this safer
TOKEN_REFRESH_HOUR   = 23    # 11 PM IST
TOKEN_REFRESH_MINUTE = 45    # 11:45 PM IST
HEALTH_CHECK_INTERVAL = 5    # seconds between health checks
os.makedirs("logs", exist_ok=True)


class Watchdog:

    def __init__(self):
        self._process       = None
        self._restart_count = 0
        self._consecutive_crashes = 0   # resets on a clean run of >60s
        self._last_start_time = 0.0
        self._token_refreshed_today = False
        self._last_token_refresh_date = None
        self._running       = True

    def start(self) -> None:
        logger.info("=" * 55)
        logger.info("  AlphaLens Watchdog — Starting")
        logger.info(f"  Python: {PYTHON}")
        logger.info(f"  Bot:    {BOT_SCRIPT}")
        logger.info("=" * 55)

        self._send_alert("🐕 Watchdog started — bot is monitored")

        while self._running:
            try:
                self._check_token_refresh()
                self._ensure_bot_running()
                time.sleep(HEALTH_CHECK_INTERVAL)
            except KeyboardInterrupt:
                logger.info("Watchdog stopped by user.")
                self._stop_bot()
                break
            except Exception as e:
                logger.error(f"Watchdog error: {e}")
                time.sleep(5)

    def _ensure_bot_running(self) -> None:
        """Check if bot process is alive. Restart if dead."""
        if self._process is None:
            logger.info("Starting bot...")
            self._start_bot()
            return

        poll = self._process.poll()
        if poll is not None:
            # Process has exited
            exit_code = poll
            self._restart_count += 1

            # If bot ran for > 60 seconds before exiting, treat as clean run and
            # reset the consecutive crash counter (transient issue, not boot loop).
            uptime = time.time() - self._last_start_time
            if uptime > 60:
                self._consecutive_crashes = 0

            if exit_code == 0:
                logger.info(f"Bot exited cleanly (code 0). Restart #{self._restart_count}")
                self._consecutive_crashes = 0
            else:
                self._consecutive_crashes += 1
                # Exponential backoff: 10s, 20s, 40s, 80s … capped at 5 min
                delay = min(RESTART_DELAY_BASE * (2 ** (self._consecutive_crashes - 1)), RESTART_DELAY_MAX)
                logger.error(
                    f"Bot CRASHED (exit {exit_code}) — #{self._restart_count}, "
                    f"consecutive: {self._consecutive_crashes}. "
                    f"Waiting {delay}s before restart."
                )
                self._send_alert(
                    f"🔴 Bot crashed (exit code {exit_code})\n"
                    f"Consecutive crashes: {self._consecutive_crashes}\n"
                    f"Restarting in {delay}s... (restart #{self._restart_count})"
                )

            if self._restart_count > MAX_RESTARTS:
                logger.critical(
                    f"Max restarts ({MAX_RESTARTS}) reached — pausing 30 min before retry."
                )
                # Bug 10: list open positions so the user knows what needs manual attention
                open_positions_msg = self._get_open_positions_summary()
                self._send_alert(
                    f"🚨 Bot failed {MAX_RESTARTS} times.\n"
                    f"Watchdog pausing 30 min then resetting counters.\n"
                    f"Check logs for root cause — bot is NOT running during this window.\n"
                    f"{open_positions_msg}"
                )
                # Sleep in 30-second increments so Ctrl+C still exits cleanly
                for _ in range(60):
                    if not self._running:
                        return
                    time.sleep(30)
                # Reset and try again from scratch
                self._restart_count = 0
                self._consecutive_crashes = 0
                logger.info("Watchdog recovery pause done — resuming restart attempts.")
                return

            # Reconcile positions before restart
            self._reconcile_positions()

            delay = min(RESTART_DELAY_BASE * (2 ** max(0, self._consecutive_crashes - 1)), RESTART_DELAY_MAX)
            time.sleep(delay)
            self._start_bot()

    def _start_bot(self) -> None:
        """Start main.py as a subprocess."""
        self._last_start_time = time.time()
        try:
            # Redirect stdout/stderr to DEVNULL — the bot writes to logs/bot.log
            # via its own logging setup. Piping through the watchdog risks the pipe
            # buffer filling up if the reader thread dies, which would freeze the bot
            # even though the watchdog still sees the process as alive.
            self._process = subprocess.Popen(
                [PYTHON, BOT_SCRIPT],
                stdout = subprocess.DEVNULL,
                stderr = subprocess.DEVNULL,
                cwd    = os.path.dirname(BOT_SCRIPT),
            )
            logger.info(f"Bot started — PID {self._process.pid}")
            self._send_alert(
                f"✅ Bot started (PID {self._process.pid})\n"
                f"Restart count: {self._restart_count}"
            )

        except Exception as e:
            logger.error(f"Failed to start bot: {e}")
            self._process = None

    def _stop_bot(self) -> None:
        """Gracefully stop the bot process."""
        if self._process and self._process.poll() is None:
            logger.info("Stopping bot...")
            self._process.terminate()
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()
            logger.info("Bot stopped.")

    def _forward_logs(self) -> None:
        """Forward bot stdout to watchdog log."""
        if not self._process or not self._process.stdout:
            return
        try:
            for line in self._process.stdout:
                line = line.rstrip()
                if line:
                    # Forward to watchdog log with [BOT] prefix
                    logging.getLogger("bot").info(line)
        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────
    # TOKEN AUTO-REFRESH
    # ─────────────────────────────────────────────────────────────

    def _check_token_refresh(self) -> None:
        """Auto-refresh Fyers token at 11:45 PM daily."""
        now   = datetime.now(tz=IST)
        today = now.date()

        # Reset daily flag at midnight
        if self._last_token_refresh_date != today:
            self._token_refreshed_today = False

        if (
            now.hour == TOKEN_REFRESH_HOUR
            and now.minute >= TOKEN_REFRESH_MINUTE
            and not self._token_refreshed_today
        ):
            logger.info("Token refresh time — running generate_token.py...")
            self._refresh_token()
            self._token_refreshed_today      = True
            self._last_token_refresh_date    = today

    def _refresh_token(self) -> bool:
        """Run generate_token.py and verify new token works."""
        try:
            logger.info("Refreshing Fyers access token...")
            result = subprocess.run(
                [PYTHON, TOKEN_SCRIPT],
                capture_output = True,
                text           = True,
                timeout        = 60,
                cwd            = os.path.dirname(BOT_SCRIPT),
            )

            if result.returncode == 0:
                logger.info("Token refreshed successfully")

                # Bug 12: warn if swing positions are open during the restart window
                swing_msg = self._get_swing_positions_warning()
                self._send_alert(
                    f"🔑 Fyers token refreshed for tomorrow\n{swing_msg}"
                )

                # Restart bot with new token
                logger.info("Restarting bot with new token...")
                self._stop_bot()
                time.sleep(3)
                self._start_bot()
                return True
            else:
                logger.error(f"Token refresh failed: {result.stderr}")
                self._send_alert(
                    f"⚠️ Token refresh FAILED\n"
                    f"Error: {result.stderr[:200]}\n"
                    f"Bot may fail after midnight — check manually."
                )
                return False

        except subprocess.TimeoutExpired:
            logger.error("Token refresh timed out after 60 seconds")
            return False
        except Exception as e:
            logger.error(f"Token refresh exception: {e}")
            return False

    # ─────────────────────────────────────────────────────────────
    # POSITION RECONCILIATION
    # ─────────────────────────────────────────────────────────────

    def _reconcile_positions(self) -> None:
        """
        After a crash, compare broker positions vs local DB.
        Log any discrepancies so they can be manually resolved.
        """
        try:
            from dotenv import load_dotenv
            load_dotenv(override=True)

            from execution.fyers_broker import fyers_broker
            fyers_broker.initialise()
            discrepancies = fyers_broker.reconcile_positions()

            if discrepancies:
                msg = (
                    f"⚠️ Position discrepancies after crash:\n" +
                    "\n".join([
                        f"  {sym}: {d['issue']}"
                        for sym, d in discrepancies.items()
                    ])
                )
                logger.warning(msg)
                self._send_alert(msg)
            else:
                logger.info("Position reconciliation: no discrepancies found")

        except Exception as e:
            logger.error(f"Position reconciliation failed: {e}")

    # ─────────────────────────────────────────────────────────────
    # ALERTS
    # ─────────────────────────────────────────────────────────────

    def _get_open_positions_summary(self) -> str:
        """Read open positions from trades.db for the watchdog alert (Bug 10)."""
        try:
            import sqlite3
            db_path = os.path.join(os.path.dirname(BOT_SCRIPT), "db", "trades.db")
            if not os.path.exists(db_path):
                return "No trades.db found — no open position data available."
            with sqlite3.connect(db_path) as conn:
                rows = conn.execute(
                    "SELECT symbol, direction, position_size, entry_price "
                    "FROM trades WHERE status IN ('OPEN', 'PENDING_CLOSE')"
                ).fetchall()
            if not rows:
                return "No open positions in DB."
            lines = ["⚠️ Open positions requiring manual monitoring:"]
            for sym, direction, qty, entry in rows:
                lines.append(f"  {direction} {sym} × {qty} @ ₹{entry:.2f}")
            return "\n".join(lines)
        except Exception as e:
            return f"Could not read open positions: {e}"

    def _get_swing_positions_warning(self) -> str:
        """Return a warning string if swing positions are open (Bug 12)."""
        try:
            import sqlite3
            db_path = os.path.join(os.path.dirname(BOT_SCRIPT), "db", "trades.db")
            if not os.path.exists(db_path):
                return ""
            with sqlite3.connect(db_path) as conn:
                rows = conn.execute(
                    "SELECT symbol FROM trades WHERE status='OPEN' AND hold_type='swing'"
                ).fetchall()
            if rows:
                syms = ", ".join(r[0] for r in rows)
                return (
                    f"⚠️ Swing positions held overnight: {syms}\n"
                    f"Bot will be dark for ~60s during restart. Positions are safe at broker."
                )
        except Exception:
            pass
        return ""

    def _send_alert(self, message: str) -> None:
        """Send Telegram alert directly (without importing full bot)."""
        try:
            from dotenv import load_dotenv
            load_dotenv(override=True)
            import os
            import requests

            token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
            chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

            if not token or not chat_id:
                return

            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json    = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
                timeout = 5,
            )
        except Exception:
            pass   # alerts are non-critical


if __name__ == "__main__":
    watchdog = Watchdog()
    watchdog.start()
