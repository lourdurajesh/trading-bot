"""
market_holidays.py
──────────────────
NSE equity segment trading holidays — and MCX commodity holidays.

NSE publishes its official calendar each December:
  https://www.nseindia.com/resources/exchange-communication-holidays

MCX publishes its holiday list at:
  https://www.mcxindia.com/market-data/market-holidays

Most national holidays are shared between NSE and MCX.
MCX_EXTRA_HOLIDAYS captures MCX-only closures (e.g. Muharram) that
are NOT in the NSE list.

Weekends are already excluded by _is_market_hours() and run_cycle() —
only add weekday holidays here.
"""

from datetime import date

NSE_HOLIDAYS: set[date] = {
    # ── 2025 ─────────────────────────────────────────────────────
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

    # ── 2026 ─────────────────────────────────────────────────────
    date(2026, 1, 26),   # Republic Day
    date(2026, 2, 26),   # Mahashivratri
    date(2026, 3,  3),   # Holi
    date(2026, 4,  3),   # Good Friday
    date(2026, 4, 14),   # Dr. Baba Saheb Ambedkar Jayanti
    date(2026, 5,  1),   # Maharashtra Day
    date(2026, 8, 15),   # Independence Day (Saturday — weekend, harmless)
    date(2026, 8, 19),   # Ganesh Chaturthi       ← verify NSE calendar
    date(2026, 10, 22),  # Dussehra               ← verify NSE calendar
    date(2026, 11,  9),  # Diwali – Laxmi Puja    ← verify NSE calendar
    date(2026, 11, 10),  # Diwali – Balipratipada ← verify NSE calendar
    date(2026, 11, 23),  # Guru Nanak Jayanti      ← verify NSE calendar
    date(2026, 12, 25),  # Christmas
}


def is_trading_holiday(d: date) -> bool:
    """True if NSE equity market is closed on weekday `d`."""
    return d in NSE_HOLIDAYS


# ── MCX commodity market holidays ──────────────────────────────────
# Most major holidays are the same as NSE. Add MCX-specific closures
# here (e.g. Muharram, which MCX observes but NSE sometimes does not).
# Format: date(YYYY, MM, DD),  # Holiday name
MCX_EXTRA_HOLIDAYS: set[date] = {
    # ── 2026 ─────────────────────────────────────────────────────
    # date(2026, 7, 6),   # Muharram — add if MCX publishes a closure
}


def is_mcx_holiday(d: date) -> bool:
    """True if MCX commodity market is closed on weekday `d`.
    Combines national holidays (shared with NSE) and MCX-specific closures.
    """
    return d in NSE_HOLIDAYS or d in MCX_EXTRA_HOLIDAYS
