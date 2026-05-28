"""
mcx_calendar.py — dynamic MCX session calendar.

MCX runs a FULL session on normal trading days (09:00–23:30 IST) and an
EVENING-ONLY session on Indian national holidays (17:00–23:30 IST, as US
markets open). MCX-specific full-day holidays are a separate, smaller list.

SessionStatus is the single source of truth consumed by both the engine
(run_cycle guard) and the dashboard (/commodity/session endpoint).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, date, time as dtime, timedelta
from enum import Enum
from typing import Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# ── Regulatory MCX session boundaries (exchange-mandated, not user-configurable) ──
_NORMAL_OPEN  = dtime(9,  0)    # full-session open (all days)
_EVENING_OPEN = dtime(17, 0)    # national-holiday evening/US session open
_MARKET_CLOSE = dtime(23, 30)   # MCX mandated close

# _ENTRY_CUTOFF and _OPEN_WAIT_MINUTES are user preferences and are read at
# call time from engine_settings so changes take effect without a restart.


class SessionPhase(str, Enum):
    CLOSED           = "CLOSED"           # outside all trading hours or weekend
    FULL_HOLIDAY     = "FULL_HOLIDAY"     # MCX-specific whole-day closure
    HOLIDAY_MORNING  = "HOLIDAY_MORNING"  # national holiday, morning session closed
    OPENING_BLACKOUT = "OPENING_BLACKOUT" # first N min of session — no new entries
    OPEN             = "OPEN"             # normal trading, entries allowed
    PAST_CUTOFF      = "PAST_CUTOFF"      # after entry cutoff, exits monitored only


@dataclass
class SessionStatus:
    phase:         SessionPhase
    allows_entry:  bool
    message:       str
    next_open_at:  Optional[datetime] = None


class MCXSessionCalendar:
    """
    Determines MCX session phase for any datetime.
    Pure function — no state, thread-safe. Import the singleton `mcx_calendar`.

    Session rules
    ─────────────
    Normal weekday:
      CLOSED          before 09:00
      OPENING_BLACKOUT 09:00–09:29  (first 30 min — false-breakout filter)
      OPEN            09:30–20:00
      PAST_CUTOFF     20:01–23:30   (exits only, spreads widen)
      CLOSED          after 23:30

    Indian national holiday (date in NSE_HOLIDAYS):
      HOLIDAY_MORNING  09:00–16:59  (morning session closed)
      OPENING_BLACKOUT 17:00–17:29  (opening blackout for US session)
      OPEN             17:30–20:00
      PAST_CUTOFF      20:01–23:30
      CLOSED           after 23:30

    MCX full-day holiday (date in MCX_EXTRA_HOLIDAYS):
      FULL_HOLIDAY all day
    """

    def get_status(self, dt: Optional[datetime] = None) -> SessionStatus:
        """Return the SessionStatus for `dt` (defaults to now IST)."""
        if dt is None:
            dt = datetime.now(tz=IST)
        elif dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)

        d = dt.date()
        t = dt.time()

        from config.market_holidays import NSE_HOLIDAYS, MCX_EXTRA_HOLIDAYS
        # Read user-configurable values at call time so dashboard changes take effect immediately
        from config.mcx_engine_settings import engine_settings as _es
        _OPEN_WAIT_MINUTES = _es.mcx_open_wait_minutes()
        _ENTRY_CUTOFF      = _es.mcx_entry_cutoff()

        # Weekend — always closed
        if d.weekday() >= 5:
            return SessionStatus(
                phase=SessionPhase.CLOSED,
                allows_entry=False,
                message="MCX closed — weekend",
                next_open_at=self._next_open(dt),
            )

        # MCX full-day holiday
        if d in MCX_EXTRA_HOLIDAYS:
            return SessionStatus(
                phase=SessionPhase.FULL_HOLIDAY,
                allows_entry=False,
                message=f"MCX closed — exchange holiday ({d.strftime('%d %b %Y')})",
                next_open_at=self._next_open(dt),
            )

        national_holiday = d in NSE_HOLIDAYS

        # Determine session_start for this day
        if national_holiday:
            if t < _EVENING_OPEN:
                # Morning — closed, evening opens at 17:00
                nxt = datetime.combine(d, _EVENING_OPEN, tzinfo=IST)
                bk_end_h = _EVENING_OPEN.hour
                bk_end_m = _EVENING_OPEN.minute + _OPEN_WAIT_MINUTES
                if bk_end_m >= 60:
                    bk_end_h += bk_end_m // 60
                    bk_end_m  = bk_end_m % 60
                bk_str = f"{bk_end_h:02d}:{bk_end_m:02d}"
                return SessionStatus(
                    phase=SessionPhase.HOLIDAY_MORNING,
                    allows_entry=False,
                    message=(
                        f"MCX closed — national holiday morning session. "
                        f"Evening (US) session opens 17:00 IST "
                        f"(entries from {bk_str} IST)"
                    ),
                    next_open_at=nxt,
                )
            session_start = _EVENING_OPEN
            session_label = "Evening/US session (national holiday)"
        else:
            if t < _NORMAL_OPEN:
                return SessionStatus(
                    phase=SessionPhase.CLOSED,
                    allows_entry=False,
                    message="MCX opens at 09:00 IST",
                    next_open_at=datetime.combine(d, _NORMAL_OPEN, tzinfo=IST),
                )
            session_start = _NORMAL_OPEN
            session_label = "Morning session"

        # Past market close
        if t > _MARKET_CLOSE:
            return SessionStatus(
                phase=SessionPhase.CLOSED,
                allows_entry=False,
                message="MCX closed — market closed at 23:30 IST",
                next_open_at=self._next_open(dt),
            )

        # Opening blackout: first _OPEN_WAIT_MINUTES of this session
        session_start_dt = datetime.combine(d, session_start, tzinfo=IST)
        blackout_end_dt  = session_start_dt + timedelta(minutes=_OPEN_WAIT_MINUTES)
        if dt < blackout_end_dt:
            rem = max(1, int((blackout_end_dt - dt).total_seconds() / 60) + 1)
            start_str    = session_start.strftime("%H:%M")
            bk_end_str   = blackout_end_dt.strftime("%H:%M")
            return SessionStatus(
                phase=SessionPhase.OPENING_BLACKOUT,
                allows_entry=False,
                message=(
                    f"MCX opening blackout — {rem}min until entry scan begins "
                    f"({start_str}–{bk_end_str} false-breakout filter)"
                ),
                next_open_at=blackout_end_dt,
            )

        # Past entry cutoff — exits only
        if t > _ENTRY_CUTOFF:
            return SessionStatus(
                phase=SessionPhase.PAST_CUTOFF,
                allows_entry=False,
                message="MCX entry cutoff passed — monitoring exits only (spreads widen after 20:00)",
            )

        # Normal open window
        return SessionStatus(
            phase=SessionPhase.OPEN,
            allows_entry=True,
            message=f"MCX {session_label} active — entries open",
        )

    def _next_open(self, dt: datetime) -> Optional[datetime]:
        """Next datetime when MCX opens for trading."""
        from config.market_holidays import NSE_HOLIDAYS, MCX_EXTRA_HOLIDAYS
        candidate = dt.date()
        for _ in range(10):
            candidate += timedelta(days=1)
            if candidate.weekday() >= 5:
                continue
            if candidate in MCX_EXTRA_HOLIDAYS:
                continue
            if candidate in NSE_HOLIDAYS:
                return datetime.combine(candidate, _EVENING_OPEN, tzinfo=IST)
            return datetime.combine(candidate, _NORMAL_OPEN, tzinfo=IST)
        return None

    def open_wait_minutes(self) -> int:
        from config.mcx_engine_settings import engine_settings
        return engine_settings.mcx_open_wait_minutes()

    def entry_cutoff(self) -> dtime:
        from config.mcx_engine_settings import engine_settings
        return engine_settings.mcx_entry_cutoff()


# Module-level singleton
mcx_calendar = MCXSessionCalendar()
