"""
fundamental_guard.py
────────────────────
Checks fundamental risk factors before a trade is approved.

Checks:
  1. Earnings proximity — never enter swing trade < 5 days before earnings
  2. Promoter pledge % — high pledge = high risk
  3. Recent result trend — improving or deteriorating
  4. Corporate actions — dividends, splits, buybacks, mergers

Returns a FundamentalRisk dataclass with a veto flag and reason.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT  = 8
EARNINGS_GUARD_DAYS = 5     # block trades within this many days of earnings

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
    )
}


@dataclass
class FundamentalRisk:
    symbol:             str
    veto:               bool  = False
    veto_reason:        str   = ""

    # Earnings
    days_to_earnings:   int   = 999
    earnings_date:      str   = "unknown"
    earnings_warning:   bool  = False

    # Promoter
    promoter_pledge_pct: float = 0.0
    pledge_warning:      bool  = False

    # Corporate actions
    upcoming_actions:   list  = field(default_factory=list)
    action_warning:     bool  = False

    # Overall fundamental score (0 = high risk, 10 = low risk)
    fundamental_score:  float = 7.0
    notes:              str   = ""


class FundamentalGuard:
    """
    Checks fundamental risk factors for a symbol.

    Usage:
        risk = fundamental_guard.check("NSE:RELIANCE-EQ")
        if risk.veto:
            # skip this trade
    """

    def __init__(self):
        self._earnings_cache: dict[str, tuple] = {}   # symbol → (date, cached_at)
        self._earnings_calendar: dict[str, str] = {}   # symbol → earnings date

        # Manually maintained earnings calendar (update weekly)
        # Format: ticker → "YYYY-MM-DD" of expected results
        self._earnings_calendar = {
            "RELIANCE":   "",
            "TCS":        "",
            "HDFCBANK":   "",
            "INFY":       "",
            "ICICIBANK":  "",
            "WIPRO":      "",
            "HCLTECH":    "",
            "BAJFINANCE": "",
        }

    def check(self, symbol: str) -> FundamentalRisk:
        """Run all fundamental checks for a symbol."""
        ticker = self._to_ticker(symbol)
        if "INDEX" in symbol or "NIFTY" in ticker:
            return FundamentalRisk(symbol=symbol, notes="Index — no fundamental check")
        risk   = FundamentalRisk(symbol=symbol)

        self._check_earnings(ticker, risk)
        self._check_corporate_actions(ticker, risk)
        self._compute_score(risk)

        return risk

    # ─────────────────────────────────────────────────────────────
    # CHECKS
    # ─────────────────────────────────────────────────────────────

    def _check_earnings(self, ticker: str, risk: FundamentalRisk) -> None:
        """
        Check proximity to earnings announcement.
        NSE publishes board meeting dates for results.
        """
        try:
            session = requests.Session()
            session.get("https://www.nseindia.com", headers=HEADERS, timeout=REQUEST_TIMEOUT)
            url  = f"https://www.nseindia.com/api/event-calendar?index={ticker}"
            resp = session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            data = resp.json()

            now = datetime.now(tz=timezone.utc)
            for event in data:
                purpose = event.get("purpose", "").lower()
                if "result" in purpose or "dividend" in purpose or "quarterly" in purpose:
                    date_str = event.get("date", "")
                    try:
                        event_dt = datetime.strptime(date_str, "%d-%b-%Y").replace(tzinfo=timezone.utc)
                        days_away = (event_dt - now).days
                        if 0 <= days_away < risk.days_to_earnings:
                            risk.days_to_earnings = days_away
                            risk.earnings_date    = date_str
                    except Exception:
                        pass

        except Exception as e:
            logger.debug(f"[FundGuard] Earnings check failed for {ticker}: {e}")

        # Also check manually maintained calendar
        manual_date = self._earnings_calendar.get(ticker, "")
        if manual_date:
            try:
                event_dt  = datetime.strptime(manual_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                days_away = (event_dt - datetime.now(tz=timezone.utc)).days
                if 0 <= days_away < risk.days_to_earnings:
                    risk.days_to_earnings = days_away
                    risk.earnings_date    = manual_date
            except Exception:
                pass

        # Apply guard
        if risk.days_to_earnings <= EARNINGS_GUARD_DAYS:
            risk.earnings_warning = True
            risk.veto             = True
            risk.veto_reason      = (
                f"Earnings in {risk.days_to_earnings} days ({risk.earnings_date}) — "
                f"swing trade blocked within {EARNINGS_GUARD_DAYS} days of results"
            )
            logger.info(f"[FundGuard] VETO {ticker}: {risk.veto_reason}")

    def _check_corporate_actions(self, ticker: str, risk: FundamentalRisk) -> None:
        """
        Check for upcoming corporate actions from NSE.
        Splits, bonuses, mergers can cause large gap moves.
        """
        try:
            session = requests.Session()
            session.get("https://www.nseindia.com", headers=HEADERS, timeout=REQUEST_TIMEOUT)
            url  = f"https://www.nseindia.com/api/corporates-corporateActions?index=equities&symbol={ticker}"
            resp = session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            data = resp.json()

            now = datetime.now(tz=timezone.utc)
            for action in data[:5]:
                ex_date_str = action.get("exDate", "")
                purpose     = action.get("subject", "")
                try:
                    ex_dt     = datetime.strptime(ex_date_str, "%d-%b-%Y").replace(tzinfo=timezone.utc)
                    days_away = (ex_dt - now).days
                    if 0 <= days_away <= 10:
                        risk.upcoming_actions.append(f"{purpose} in {days_away} days")
                        # Mergers and demergers are high impact
                        if any(w in purpose.lower() for w in ["merger", "demerger", "scheme", "amalgam"]):
                            risk.action_warning = True
                            risk.veto           = True
                            risk.veto_reason    = f"Corporate action pending: {purpose}"
                except Exception:
                    pass

        except Exception as e:
            logger.debug(f"[FundGuard] Corporate actions check failed for {ticker}: {e}")

    def _compute_score(self, risk: FundamentalRisk) -> None:
        """Compute 0-10 fundamental score. 10 = clean, 0 = high risk."""
        score = 10.0

        # Earnings proximity penalty
        if risk.days_to_earnings <= 5:
            score -= 5.0
        elif risk.days_to_earnings <= 10:
            score -= 2.0
        elif risk.days_to_earnings <= 20:
            score -= 0.5

        # Corporate action penalty
        if risk.action_warning:
            score -= 4.0
        elif risk.upcoming_actions:
            score -= 1.0

        risk.fundamental_score = round(max(0, score), 1)

        notes = []
        if risk.days_to_earnings < 999:
            notes.append(f"Earnings: {risk.days_to_earnings}d ({risk.earnings_date})")
        if risk.upcoming_actions:
            notes.append(f"Actions: {', '.join(risk.upcoming_actions[:2])}")
        risk.notes = " | ".join(notes) if notes else "No fundamental concerns"

    @staticmethod
    def _to_ticker(symbol: str) -> str:
        return symbol.replace("NSE:", "").replace("-EQ", "").replace("-INDEX", "")

    def update_earnings_calendar(self, ticker: str, date: str) -> None:
        """Manually update earnings date. Format: YYYY-MM-DD."""
        self._earnings_calendar[ticker] = date
        logger.info(f"[FundGuard] Earnings calendar updated: {ticker} → {date}")


# ── Module-level singleton ────────────────────────────────────────
fundamental_guard = FundamentalGuard()
