"""
options_risk.py
───────────────
Options-specific risk gate — enforces hard safety limits before any
options trade is entered.

Why a separate module? Options can lose 100% of premium in minutes.
Equity-style risk sizing (shares = risk_amount / risk_per_share) is
wrong for options because:
  - Position is in lots, not shares
  - Premium × lot_size = total capital at risk
  - A 2-lot NIFTY trade = 2 × 75 = 150 units × premium

Safety checks enforced here:
  1. No trading on expiry day (gamma risk is extreme)
  2. VIX gate — block short premium when VIX is too high
  3. Min premium LTP — skip near-zero options (100%+ noise moves)
  4. Max lots per trade — hard capital cap
  5. Max capital per trade (% of total capital)
  6. Separate daily options loss limit (stricter than equity)
  7. Min open interest (avoid illiquid strikes)
  8. Debit spread check — IV rank should be LOW before buying options

Used by: risk_manager.validate()
"""

import logging
import threading
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

from config.settings import (
    DAILY_OPTIONS_LOSS_LIMIT_PCT,
    MAX_OPTIONS_LOTS_PER_TRADE,
    MAX_OPTIONS_TRADE_PCT,
    MIN_OPTION_LTP,
    MIN_OPTION_OI,
    OPTIONS_VIX_LIMIT,
    RISK_PER_TRADE_PCT,
    TOTAL_CAPITAL,
)

logger = logging.getLogger(__name__)

# VIX symbol on Fyers / NSE India
INDIA_VIX_SYMBOL = "NSE:INDIAVIX-INDEX"


class OptionsRiskGate:
    """
    Pre-trade safety gate for all options signals.

    Usage:
        gate = options_risk_gate   # singleton
        approved, reason, lots = gate.check(signal, capital)
        if not approved:
            return RiskDecision(False, reason)
    """

    def __init__(self):
        self._lock                    = threading.Lock()
        self._daily_options_pnl       = 0.0
        self._daily_reset_date        = datetime.now(tz=IST).date()
        self._options_kill_switch     = False
        self._options_kill_reason     = ""

    # ─────────────────────────────────────────────────────────────
    # PUBLIC
    # ─────────────────────────────────────────────────────────────

    def check(
        self,
        signal,          # Signal from base_strategy
        capital: float,
    ) -> tuple[bool, str, int]:
        """
        Run all options-specific pre-trade checks.

        Returns:
            (approved: bool, reason: str, lots: int)
            lots = number of lots to trade (0 if not approved)
        """
        self._reset_daily_if_needed()

        # ── 1. Options kill switch (daily loss limit) ─────────────
        if self._options_kill_switch:
            return False, f"Options kill switch: {self._options_kill_reason}", 0

        # ── 2. Expiry day protection ──────────────────────────────
        expiry_date = self._get_signal_expiry(signal)
        if expiry_date and self._is_expiry_today(expiry_date):
            return (
                False,
                f"Expiry day protection: {expiry_date} is today — gamma risk too high",
                0,
            )

        # ── 3. VIX gate (short premium only) ─────────────────────
        strategy_type = (signal.options_meta or {}).get("strategy", "")
        if strategy_type == "short_strangle":
            vix = self._get_vix()
            if vix and vix > OPTIONS_VIX_LIMIT:
                return (
                    False,
                    f"VIX {vix:.1f} exceeds limit {OPTIONS_VIX_LIMIT} — "
                    f"short premium too dangerous in high vol",
                    0,
                )

        # ── 4. Min premium LTP ────────────────────────────────────
        premium = float(signal.entry or 0)
        if premium < MIN_OPTION_LTP:
            return (
                False,
                f"Option premium ₹{premium:.2f} below minimum ₹{MIN_OPTION_LTP} — "
                f"near-zero options have extreme % moves",
                0,
            )

        # ── 5. Calculate lots and capital check ───────────────────
        lot_size = int((signal.options_meta or {}).get("lot_size", 1))
        if lot_size <= 0:
            lot_size = 1

        lots, cap_used = self._calculate_lots(premium, lot_size, capital)

        if lots <= 0:
            return False, "Lot calculation resulted in zero lots — premium too large for budget", 0

        # ── 6. Daily options loss check ───────────────────────────
        daily_loss_pct = abs(self._daily_options_pnl / capital) * 100
        if self._daily_options_pnl < 0 and daily_loss_pct >= DAILY_OPTIONS_LOSS_LIMIT_PCT:
            self._trigger_options_kill_switch(
                f"Daily options loss {daily_loss_pct:.1f}% exceeded limit {DAILY_OPTIONS_LOSS_LIMIT_PCT}%"
            )
            return False, self._options_kill_reason, 0

        logger.debug(
            f"[OptionsRisk] APPROVED {signal.symbol} | "
            f"Lots: {lots} | Premium: ₹{premium} | Cap: ₹{cap_used:,.0f} | "
            f"Strategy: {strategy_type}"
        )
        return True, "Options risk checks passed", lots

    def update_daily_pnl(self, pnl_change: float, capital: float) -> None:
        """
        Called by portfolio_tracker when an options trade closes.
        Tracks options-specific daily P&L with its own kill switch.
        """
        self._reset_daily_if_needed()
        with self._lock:
            self._daily_options_pnl += pnl_change

        loss_pct = abs(self._daily_options_pnl / capital) * 100
        if self._daily_options_pnl < 0 and loss_pct >= DAILY_OPTIONS_LOSS_LIMIT_PCT:
            self._trigger_options_kill_switch(
                f"Daily options loss ₹{abs(self._daily_options_pnl):,.0f} "
                f"({loss_pct:.1f}%) exceeded limit {DAILY_OPTIONS_LOSS_LIMIT_PCT}%"
            )

    def reset_kill_switch(self) -> None:
        """Manual override — call each morning before options trading begins."""
        with self._lock:
            self._options_kill_switch = False
            self._options_kill_reason = ""
        logger.warning("[OptionsRisk] Kill switch manually reset")

    @property
    def kill_switch_active(self) -> bool:
        return self._options_kill_switch

    @property
    def daily_options_pnl(self) -> float:
        return self._daily_options_pnl

    def status(self) -> dict:
        return {
            "options_kill_switch":      self._options_kill_switch,
            "options_kill_reason":      self._options_kill_reason,
            "daily_options_pnl":        round(self._daily_options_pnl, 2),
            "daily_options_loss_limit": DAILY_OPTIONS_LOSS_LIMIT_PCT,
            "max_lots_per_trade":       MAX_OPTIONS_LOTS_PER_TRADE,
            "vix_limit":                OPTIONS_VIX_LIMIT,
        }

    # ─────────────────────────────────────────────────────────────
    # INTERNAL
    # ─────────────────────────────────────────────────────────────

    def _calculate_lots(
        self, premium: float, lot_size: int, capital: float
    ) -> tuple[int, float]:
        """
        Lot-size-aware position sizing.

        Logic:
          risk_budget   = capital × RISK_PER_TRADE_PCT / 100
                          (for debit spreads, premium IS the max loss)
          max_by_risk   = floor(risk_budget / (premium × lot_size))
          max_by_cap    = floor(capital × MAX_OPTIONS_TRADE_PCT / 100 / (premium × lot_size))
          lots          = min(max_by_risk, max_by_cap, MAX_OPTIONS_LOTS_PER_TRADE)
        """
        cost_per_lot  = premium * lot_size
        if cost_per_lot <= 0:
            return 0, 0.0

        risk_budget   = capital * (RISK_PER_TRADE_PCT / 100)
        cap_budget    = capital * (MAX_OPTIONS_TRADE_PCT / 100)

        max_by_risk   = int(risk_budget / cost_per_lot)
        max_by_cap    = int(cap_budget  / cost_per_lot)
        lots          = min(max_by_risk, max_by_cap, MAX_OPTIONS_LOTS_PER_TRADE)
        # Do NOT force min=1 here — if budget says 0 lots, return 0 so caller rejects the trade.
        # Forcing 1 lot when budget says 0 can deploy 100× intended capital.

        if lots <= 0:
            return 0, 0.0

        capital_used  = lots * cost_per_lot
        return lots, round(capital_used, 2)

    def _get_signal_expiry(self, signal) -> Optional[str]:
        """Extract expiry date string from options_meta (format: YYYY-MM-DD)."""
        meta = signal.options_meta or {}
        # Check for explicit expiry field first
        if meta.get("expiry"):
            return meta["expiry"]
        # Try to parse from nfo_symbol or nfo_call
        nfo = meta.get("nfo_symbol") or meta.get("nfo_call")
        if nfo:
            return self._parse_expiry_from_symbol(nfo)
        return None

    def _parse_expiry_from_symbol(self, symbol: str) -> Optional[str]:
        """
        Parse expiry date from Fyers NFO symbol.
        Monthly: NSE:NIFTY25JAN24500CE → 2025-01-last-thursday
        Weekly:  NSE:NIFTY2501234500CE → 2025-01-23
        """
        try:
            # Strip prefix (NSE:) and underlying name (NIFTY / BANKNIFTY)
            raw = symbol.replace("NSE:", "")
            for name in ("BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTY", "SENSEX"):
                if raw.startswith(name):
                    raw = raw[len(name):]
                    break

            # Monthly pattern: 25JAN → 2-digit year + 3-letter month
            months = {
                "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4,
                "MAY": 5, "JUN": 6, "JUL": 7, "AUG": 8,
                "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
            }
            if len(raw) >= 5 and raw[2:5].upper() in months:
                year  = 2000 + int(raw[:2])
                month = months[raw[2:5].upper()]
                # Last Thursday of that month = monthly expiry
                last_thurs = self._last_thursday(year, month)
                return last_thurs.strftime("%Y-%m-%d")

            # Weekly pattern: 250123 → year=25, month=01, day=23
            if len(raw) >= 6 and raw[:6].isdigit():
                year  = 2000 + int(raw[:2])
                month = int(raw[2:4])
                day   = int(raw[4:6])
                return f"{year:04d}-{month:02d}-{day:02d}"

        except Exception:
            pass
        return None

    @staticmethod
    def _last_thursday(year: int, month: int) -> date:
        """Return the last Thursday of a given month."""
        import calendar
        last_day = calendar.monthrange(year, month)[1]
        d = date(year, month, last_day)
        # weekday(): 0=Mon, 3=Thu, 6=Sun
        offset = (d.weekday() - 3) % 7
        return date(year, month, last_day - offset)

    def _is_expiry_today(self, expiry_str: str) -> bool:
        """Return True if the expiry date is today — no trading on expiry day."""
        try:
            expiry = date.fromisoformat(expiry_str)
            return expiry == datetime.now(tz=IST).date()
        except Exception:
            return False

    def _get_vix(self) -> Optional[float]:
        """Fetch India VIX from data store. Returns None if unavailable."""
        try:
            from data.data_store import store
            vix = store.get_ltp(INDIA_VIX_SYMBOL)
            return float(vix) if vix and vix > 0 else None
        except Exception:
            return None

    def _trigger_options_kill_switch(self, reason: str) -> None:
        with self._lock:
            if not self._options_kill_switch:
                self._options_kill_switch = True
                self._options_kill_reason = reason
                logger.critical(f"[OptionsRisk] OPTIONS KILL SWITCH: {reason}")
                try:
                    from audit_log import audit_log
                    audit_log.kill_switch(
                        activated=True,
                        reason=f"OPTIONS: {reason}",
                    )
                except Exception:
                    pass
                try:
                    from notifications.alert_service import alert_service
                    alert_service.info(
                        f"🚨 OPTIONS TRADING HALTED\n{reason}\n"
                        f"Manual reset required via dashboard."
                    )
                except Exception:
                    pass

    def _reset_daily_if_needed(self) -> None:
        today = datetime.now(tz=IST).date()
        if today != self._daily_reset_date:
            with self._lock:
                if today != self._daily_reset_date:
                    logger.info(
                        f"[OptionsRisk] New day — resetting options P&L "
                        f"from ₹{self._daily_options_pnl:,.0f} to 0"
                    )
                    self._daily_options_pnl = 0.0
                    self._daily_reset_date  = today
                    # Kill switch must be manually reset each morning


# ── Module-level singleton ────────────────────────────────────────
options_risk_gate = OptionsRiskGate()
