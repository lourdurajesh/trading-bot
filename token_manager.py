"""
token_manager.py
─────────────────
Autonomous Fyers token lifecycle manager.

Detects expired / invalid tokens at startup and on a 30-min heartbeat,
then silently refreshes using the TOTP+PIN flow already in generate_token.py.
No human interaction required — fully autonomous.

Required in .env (same as generate_token.py):
  FYERS_APP_ID, FYERS_SECRET_KEY, FYERS_REDIRECT_URI
  FYERS_CLIENT_ID, FYERS_PIN, FYERS_TOTP_SECRET

After a successful refresh the new token is:
  1. Written back to .env (FYERS_ACCESS_TOKEN)
  2. Set in os.environ and config.settings in-process
  3. Used to re-initialise FyersBroker, OIAnalyzer, OptionsEngine
  4. The system_health "fyers_token" alert is cleared
"""

import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)

# Minimum gap between refresh attempts — avoids hammering the Fyers auth API
_REFRESH_COOLDOWN_MINUTES = 15


class TokenManager:

    def __init__(self):
        self._last_attempt:   datetime | None = None
        self._last_success:   datetime | None = None
        self._refresh_count:  int = 0
        self._fail_count:     int = 0

    # ─────────────────────────────────────────────────────────────
    # PUBLIC
    # ─────────────────────────────────────────────────────────────

    def check_and_refresh_if_needed(self) -> bool:
        """
        Verify the current Fyers token is valid.
        If not, auto-refresh via TOTP.
        Returns True when token is valid (was already valid or refresh succeeded).
        Call this at startup and every 30 min from the main loop.
        """
        from execution.fyers_broker import fyers_broker

        # If broker is not initialised at all, try to init first (token might be present)
        if not fyers_broker._initialised:
            if fyers_broker.initialise():
                from system_health import system_health
                system_health.clear_alert("fyers_token")
                return True
            # Init failed — try refreshing
            return self._do_refresh("Broker failed to initialise")

        # Broker is initialised — verify token is still alive
        try:
            resp = fyers_broker._client.get_profile()
            if resp.get("s") == "ok":
                return True
            return self._do_refresh(f"get_profile returned: {resp.get('message','unknown')}")
        except Exception as exc:
            return self._do_refresh(f"get_profile exception: {exc}")

    def notify_token_failure(self, component: str, message: str) -> None:
        """
        Called by OIAnalyzer / OptionsEngine when they hit "Please provide valid token".
        Raises a system_health alert and attempts a refresh (rate-limited).
        """
        from system_health import system_health
        system_health.set_alert(
            "fyers_token",
            f"Token error in {component}: {message} — attempting auto-refresh",
            severity="error",
        )
        logger.warning(f"[TokenManager] Token failure reported by {component}: {message}")
        self._do_refresh(f"{component}: {message}")

    def get_status(self) -> dict:
        return {
            "last_success":    self._last_success.isoformat() if self._last_success else None,
            "last_attempt":    self._last_attempt.isoformat() if self._last_attempt else None,
            "refresh_count":   self._refresh_count,
            "fail_count":      self._fail_count,
        }

    # ─────────────────────────────────────────────────────────────
    # INTERNAL
    # ─────────────────────────────────────────────────────────────

    def _do_refresh(self, reason: str) -> bool:
        from system_health import system_health

        # Rate-limit
        if self._last_attempt:
            elapsed_min = (datetime.now(tz=IST) - self._last_attempt).total_seconds() / 60
            if elapsed_min < _REFRESH_COOLDOWN_MINUTES:
                logger.debug(
                    f"[TokenManager] Refresh skipped — cooldown "
                    f"({elapsed_min:.0f}/{_REFRESH_COOLDOWN_MINUTES} min). Reason: {reason}"
                )
                return False

        self._last_attempt = datetime.now(tz=IST)
        logger.info(f"[TokenManager] Auto-refreshing Fyers token — reason: {reason}")

        try:
            # Import lazily so generate_token.py's module-level load_dotenv runs fresh
            from generate_token import (
                validate_config,
                step1_send_otp,
                step2_verify_totp,
                step3_verify_pin,
                step4_get_access_token,
                step5_generate_token,
                save_token,
            )

            if not validate_config():
                msg = (
                    "TOTP auto-refresh requires FYERS_TOTP_SECRET, FYERS_PIN, "
                    "FYERS_CLIENT_ID in .env — token refresh not possible"
                )
                logger.error(f"[TokenManager] {msg}")
                system_health.set_alert("fyers_token", msg, severity="critical")
                self._fail_count += 1
                return False

            request_key  = step1_send_otp()
            request_key  = step2_verify_totp(request_key)
            login_token  = step3_verify_pin(request_key)
            auth_code    = step4_get_access_token(login_token)
            access_token = step5_generate_token(auth_code)

            # Persist to .env
            save_token(access_token)

            # Update in-process environment + settings module
            import config.settings as settings_module
            settings_module.FYERS_ACCESS_TOKEN = access_token
            os.environ["FYERS_ACCESS_TOKEN"]   = access_token

            # Re-initialise all Fyers-dependent components
            ok = self._reinit_fyers(access_token)

            if ok:
                self._last_success  = datetime.now(tz=IST)
                self._refresh_count += 1
                system_health.clear_alert("fyers_token")
                logger.info(
                    f"[TokenManager] Token refreshed successfully "
                    f"(total refreshes today: {self._refresh_count})"
                )
                return True
            else:
                msg = "Token refreshed but broker re-init failed (check logs)"
                logger.error(f"[TokenManager] {msg}")
                system_health.set_alert("fyers_token", msg, severity="error")
                self._fail_count += 1
                return False

        except Exception as exc:
            msg = f"Token refresh failed: {exc}"
            logger.error(f"[TokenManager] {msg}", exc_info=True)
            system_health.set_alert("fyers_token", msg, severity="error")
            self._fail_count += 1
            return False

    def _reinit_fyers(self, new_token: str) -> bool:
        """Re-initialise broker + OI analyzer + options engine with the fresh token."""
        try:
            from execution.fyers_broker import fyers_broker
            fyers_broker._client      = None
            fyers_broker._initialised = False
            ok = fyers_broker.initialise()
            if not ok:
                return False

            try:
                from analysis.oi_analyzer import oi_analyzer
                oi_analyzer._fyers = None
                oi_analyzer._consecutive_failures.clear()
                oi_analyzer.initialise()
            except Exception as e:
                logger.warning(f"[TokenManager] OI analyzer reinit warning: {e}")

            try:
                from analysis.options_engine import options_engine
                options_engine.initialise()
            except Exception as e:
                logger.warning(f"[TokenManager] Options engine reinit warning: {e}")

            return True
        except Exception as e:
            logger.error(f"[TokenManager] Re-init failed: {e}")
            return False


# Module-level singleton
token_manager = TokenManager()
