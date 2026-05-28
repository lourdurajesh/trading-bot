"""
market_holidays.py
──────────────────
NSE equity and MCX commodity market holidays.

Holiday data is fetched from the NSE holiday-master API at bot startup and
cached in the DB so it stays current automatically — no manual code changes
needed when NSE publishes its annual calendar.

Flow
────
  sync_holidays()          ← called once at bot startup
      │
      ├─ DB cache hit?  → load from commodity_settings table
      │
      ├─ DB miss       → fetch from NSE holiday-master API → write to DB
      │
      └─ API failure   → fall back to hardcoded list (last resort)

Public API (unchanged for all callers)
───────────────────────────────────────
  NSE_HOLIDAYS       — set-like, `d in NSE_HOLIDAYS`
  MCX_EXTRA_HOLIDAYS — set-like, `d in MCX_EXTRA_HOLIDAYS`
  is_trading_holiday(d)  → bool
  is_mcx_holiday(d)      → bool

Dashboard helpers
─────────────────
  sync_holidays()             — called at startup
  refresh_holidays()          — force re-fetch from NSE API (dashboard button)
  get_holidays(year)          — list for dashboard display
  add_mcx_extra_holiday(d)    — add MCX-only closure via dashboard
  remove_mcx_extra_holiday(d) — remove MCX-only closure via dashboard
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH        = "db/trades.db"
_NSE_KEY       = "holidays.nse.{year}"   # commodity_settings key per calendar year
_MCX_EXTRA_KEY = "holidays.mcx_extra"    # small set of MCX-only closures (not in NSE list)

# ── NSE API ───────────────────────────────────────────────────────────────────
# Same session-cookie approach as premarket_analyzer — NSE rejects bare requests.
_NSE_HOME        = "https://www.nseindia.com/"
_NSE_HOLIDAY_URL = "https://www.nseindia.com/api/holiday-master?type=trading"
_NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept":          "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer":         "https://www.nseindia.com/",
    "Connection":      "keep-alive",
}

# ── Hardcoded fallback ────────────────────────────────────────────────────────
# Used ONLY when both the DB cache AND the NSE API are unavailable (first run,
# network down, NSE rate-limiting).  Keep this list current as a safety net.
_HARDCODED_NSE: set[date] = {
    # ── 2025 ─────────────────────────────────────────────────────────────────
    date(2025, 2, 26),   # Mahashivratri
    date(2025, 3, 14),   # Holi
    date(2025, 4, 14),   # Dr. Baba Saheb Ambedkar Jayanti
    date(2025, 4, 18),   # Good Friday
    date(2025, 5,  1),   # Maharashtra Day
    date(2025, 8, 15),   # Independence Day
    date(2025, 8, 27),   # Ganesh Chaturthi
    date(2025, 10, 2),   # Gandhi Jayanti / Dussehra
    date(2025, 10, 20),  # Diwali – Laxmi Puja
    date(2025, 10, 21),  # Diwali – Balipratipada
    date(2025, 11,  5),  # Guru Nanak Jayanti
    date(2025, 12, 25),  # Christmas
    # ── 2026 ─────────────────────────────────────────────────────────────────
    date(2026, 1, 26),   # Republic Day
    date(2026, 2, 26),   # Mahashivratri
    date(2026, 3,  3),   # Holi
    date(2026, 4,  3),   # Good Friday
    date(2026, 4, 14),   # Dr. Baba Saheb Ambedkar Jayanti
    date(2026, 5,  1),   # Maharashtra Day
    date(2026, 5, 28),   # Buddha Purnima
    date(2026, 8, 15),   # Independence Day
    date(2026, 8, 19),   # Ganesh Chaturthi
    date(2026, 10, 22),  # Dussehra
    date(2026, 11,  9),  # Diwali – Laxmi Puja
    date(2026, 11, 10),  # Diwali – Balipratipada
    date(2026, 11, 23),  # Guru Nanak Jayanti
    date(2026, 12, 25),  # Christmas
}

# ── Live sets — mutated by sync_holidays() ────────────────────────────────────
# Pre-populated with the hardcoded list so callers work correctly even before
# sync_holidays() is invoked (e.g. during module-level imports at startup).
_live_nse:       set[date] = set(_HARDCODED_NSE)
_live_mcx_extra: set[date] = set()


class _HolidayProxy:
    """
    Thin proxy around a live set[date].

    All callers use `d in NSE_HOLIDAYS` — only __contains__, __iter__, __len__
    are needed.  Because we hold a reference to the SAME set object that
    sync_holidays() mutates in-place, the proxy always reflects the latest data
    without any rebinding.
    """
    __slots__ = ("_ref",)

    def __init__(self, live_ref: set[date]) -> None:
        self._ref = live_ref

    def __contains__(self, item: object) -> bool:
        return item in self._ref

    def __iter__(self):
        return iter(self._ref)

    def __len__(self) -> int:
        return len(self._ref)

    def __repr__(self) -> str:  # useful for debug
        return f"HolidayProxy({len(self._ref)} dates)"


# ── Public names — drop-in replacements for the old plain sets ────────────────
NSE_HOLIDAYS:       _HolidayProxy = _HolidayProxy(_live_nse)
MCX_EXTRA_HOLIDAYS: _HolidayProxy = _HolidayProxy(_live_mcx_extra)


# ── DB helpers ────────────────────────────────────────────────────────────────

def _db_read(key: str) -> Optional[str]:
    try:
        import sqlite3
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                "SELECT value FROM commodity_settings WHERE key=?", (key,)
            ).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def _db_write(key: str, value: str) -> None:
    try:
        import sqlite3
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO commodity_settings (key, value) VALUES (?,?)",
                (key, value),
            )
    except Exception:
        pass


# ── NSE API fetch ─────────────────────────────────────────────────────────────

def _fetch_nse_holidays_api(year: int) -> set[date]:
    """
    Fetch NSE Capital Market trading holidays for `year` from the NSE API.

    NSE returns a JSON object keyed by segment (CM, FO, CD, …).  We read the
    CM (Capital Market) segment — the same holidays NSE equity & Nifty observe.
    Date strings are in "DD-Mon-YYYY" format (e.g. "26-Jan-2026").

    Returns an empty set on any failure — callers fall back to the DB cache or
    the hardcoded list.
    """
    try:
        import requests
        session = requests.Session()
        # NSE requires a valid session cookie — visiting the homepage first sets it.
        session.get(_NSE_HOME, headers=_NSE_HEADERS, timeout=8)
        resp = session.get(_NSE_HOLIDAY_URL, headers=_NSE_HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        holidays: set[date] = set()
        # CM = Capital Market is the authoritative segment for equity holidays.
        # Fall through to FO if CM is absent (both should be identical).
        for segment in ("CM", "FO"):
            rows = data.get(segment, [])
            if not rows:
                continue
            for row in rows:
                raw = row.get("tradingDate", "").strip()
                if not raw:
                    continue
                # Try both common NSE date formats
                for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d"):
                    try:
                        d = datetime.strptime(raw, fmt).date()
                        if d.year == year:
                            holidays.add(d)
                        break
                    except ValueError:
                        pass
            if holidays:
                break   # CM was populated — no need for FO

        logger.info(
            f"[Holidays] NSE API returned {len(holidays)} holidays for {year}"
        )
        return holidays

    except Exception as exc:
        logger.warning(f"[Holidays] NSE API fetch failed for {year}: {exc}")
        return set()


# ── Public sync / refresh ─────────────────────────────────────────────────────

def sync_holidays() -> None:
    """
    Populate NSE_HOLIDAYS and MCX_EXTRA_HOLIDAYS from DB cache / NSE API.

    Call once at bot startup.  Idempotent — safe to call multiple times.

    Priority per calendar year:
      1. DB cache (written on first successful API fetch; persists across restarts)
      2. NSE holiday-master API (fetched when DB has no entry for this year)
      3. Hardcoded fallback (when both DB and API are unavailable)

    Also loads next year's holidays in December so the bot is ready on Jan 1.
    """
    today = date.today()
    years_needed = {today.year}
    if today.month == 12:
        years_needed.add(today.year + 1)   # pre-fetch next year in December

    # Always start from the hardcoded list so known dates survive even when the
    # DB cache is stale or was written before a holiday was added to the list.
    # The DB / NSE API can only ADD dates on top of this baseline.
    merged: set[date] = set(_HARDCODED_NSE)

    for year in sorted(years_needed):
        cache_key = _NSE_KEY.format(year=year)
        raw = _db_read(cache_key)
        if raw:
            try:
                loaded = {date.fromisoformat(s) for s in json.loads(raw)}
                merged |= loaded
                logger.info(
                    f"[Holidays] {year}: {len(loaded)} holidays loaded from DB cache"
                )
                continue
            except Exception:
                pass   # corrupt cache — fall through to API

        # DB miss (or corrupt) — fetch from NSE API
        fetched = _fetch_nse_holidays_api(year)
        if fetched:
            _db_write(cache_key, json.dumps([d.isoformat() for d in fetched]))
            merged |= fetched
            logger.info(
                f"[Holidays] {year}: {len(fetched)} holidays fetched from NSE API and cached"
            )
        else:
            # Both DB and API failed — fall back to hardcoded for this year
            fallback = {d for d in _HARDCODED_NSE if d.year == year}
            merged |= fallback
            logger.warning(
                f"[Holidays] {year}: NSE API unavailable — using {len(fallback)} "
                f"hardcoded holidays as fallback"
            )

    # Mutate in-place so the _HolidayProxy objects see the update immediately
    _live_nse.clear()
    _live_nse.update(merged)

    # MCX extra holidays (Muharram etc.) — small DB-editable list
    raw_mcx = _db_read(_MCX_EXTRA_KEY)
    if raw_mcx:
        try:
            extras = {date.fromisoformat(s) for s in json.loads(raw_mcx)}
            _live_mcx_extra.clear()
            _live_mcx_extra.update(extras)
            logger.info(f"[Holidays] MCX extra: {len(extras)} holidays loaded from DB")
        except Exception:
            pass


def refresh_holidays() -> dict:
    """
    Force-refresh holidays from NSE API for the current (and next, if Dec) year,
    bypassing the DB cache.  Writes fresh data to DB cache on success.
    Called from the dashboard /holidays/refresh endpoint.

    Returns {"year": count, ...} mapping.
    """
    today = date.today()
    years_needed = {today.year}
    if today.month == 12:
        years_needed.add(today.year + 1)

    result: dict[int, int] = {}
    merged: set[date] = set()

    # Keep holidays for years we're NOT refreshing
    merged.update(d for d in _live_nse if d.year not in years_needed)

    for year in sorted(years_needed):
        fetched = _fetch_nse_holidays_api(year)
        if fetched:
            _db_write(
                _NSE_KEY.format(year=year),
                json.dumps([d.isoformat() for d in fetched]),
            )
            merged |= fetched
            result[year] = len(fetched)
            logger.info(
                f"[Holidays] Force-refreshed {year}: {len(fetched)} holidays"
            )
        else:
            result[year] = 0
            logger.warning(f"[Holidays] Force-refresh failed for {year}")

    _live_nse.clear()
    _live_nse.update(merged)
    return result


def get_holidays(year: Optional[int] = None) -> list[dict]:
    """
    Return all holidays for `year` (defaults to current year) for the dashboard.
    Each entry: {date, type, label (if known), year}
    """
    if year is None:
        year = date.today().year
    result = []
    for d in sorted(_live_nse):
        if d.year == year:
            result.append({"date": d.isoformat(), "type": "NSE", "year": year})
    for d in sorted(_live_mcx_extra):
        if d.year == year:
            result.append({"date": d.isoformat(), "type": "MCX_EXTRA", "year": year})
    return sorted(result, key=lambda x: x["date"])


def add_mcx_extra_holiday(d: date, label: str = "") -> None:
    """
    Add an MCX-specific holiday (e.g. Muharram) that is NOT in the NSE list.
    Persists to DB immediately — survives restarts.
    """
    _live_mcx_extra.add(d)
    _db_write(
        _MCX_EXTRA_KEY,
        json.dumps([x.isoformat() for x in sorted(_live_mcx_extra)]),
    )
    logger.info(f"[Holidays] MCX extra holiday added: {d} ({label or 'no label'})")


def remove_mcx_extra_holiday(d: date) -> bool:
    """
    Remove an MCX-specific holiday.  Returns True if it existed.
    Persists to DB immediately.
    """
    existed = d in _live_mcx_extra
    _live_mcx_extra.discard(d)
    _db_write(
        _MCX_EXTRA_KEY,
        json.dumps([x.isoformat() for x in sorted(_live_mcx_extra)]),
    )
    if existed:
        logger.info(f"[Holidays] MCX extra holiday removed: {d}")
    return existed


# ── Convenience helpers (unchanged API for all existing callers) ──────────────

def is_trading_holiday(d: date) -> bool:
    """True if NSE equity market is closed on weekday `d`."""
    return d in NSE_HOLIDAYS


def is_mcx_holiday(d: date) -> bool:
    """True if MCX commodity market is closed on weekday `d`.
    Combines national holidays (shared with NSE) and MCX-specific closures.
    """
    return d in NSE_HOLIDAYS or d in MCX_EXTRA_HOLIDAYS
