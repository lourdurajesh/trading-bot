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

logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s [%(levelname)s] watchdog: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/watchdog.log", encoding="utf-8"),
    ]
)
logger = logging.getLogger("watchdog")

PYTHON          = sys.executable
BOT_SCRIPT      = os.path.join(os.path.dirname(__file__), "main.py")
TOKEN_SCRIPT    = os.path.join(os.path.dirname(__file__), "generate_token.py")
RESTART_DELAY   = 10     # seconds before restarting after crash
MAX_RESTARTS    = 10     # max restarts before giving up
TOKEN_REFRESH_HOUR   = 23    # 11 PM IST
TOKEN_REFRESH_MINUTE = 45    # 11:45 PM IST
HEALTH_CHECK_INTERVAL = 5    # seconds between health checks
os.makedirs("logs", exist_ok=True)


class Watchdog:

    def __init__(self):
        self._process       = None
        self._restart_count = 0
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

            if exit_code == 0:
                logger.info(f"Bot exited cleanly (code 0). "
                            f"Restart #{self._restart_count}")
            else:
                logger.error(f"Bot CRASHED with code {exit_code}. "
                             f"Restart #{self._restart_count}")
                self._send_alert(
                    f"🔴 Bot crashed (exit code {exit_code})\n"
                    f"Restarting in {RESTART_DELAY}s... "
                    f"(restart #{self._restart_count})"
                )

            if self._restart_count > MAX_RESTARTS:
                logger.critical(
                    f"Max restarts ({MAX_RESTARTS}) reached. "
                    f"Stopping watchdog."
                )
                self._send_alert(
                    f"🚨 Bot failed {MAX_RESTARTS} times — "
                    f"watchdog giving up. Manual intervention required."
                )
                self._running = False
                return

            # Reconcile positions before restart
            self._reconcile_positions()

            time.sleep(RESTART_DELAY)
            self._start_bot()

    def _start_bot(self) -> None:
        """Start main.py as a subprocess."""
        try:
            self._process = subprocess.Popen(
                [PYTHON, BOT_SCRIPT],
                stdout = subprocess.PIPE,
                stderr = subprocess.STDOUT,
                text   = True,
                bufsize = 1,
                cwd    = os.path.dirname(BOT_SCRIPT),
            )
            logger.info(f"Bot started — PID {self._process.pid}")
            self._send_alert(
                f"✅ Bot started (PID {self._process.pid})\n"
                f"Restart count: {self._restart_count}"
            )

            # Start log forwarder thread
            import threading
            t = threading.Thread(
                target = self._forward_logs,
                daemon = True,
            )
            t.start()

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
        now   = datetime.now()
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
                self._send_alert("🔑 Fyers token refreshed for tomorrow")

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
