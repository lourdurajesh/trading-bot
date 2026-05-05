"""
market_holidays.py
──────────────────
NSE equity segment trading holidays.

Update this list each year — NSE publishes the official calendar in
December for the following year at: https://www.nseindia.com/resources/exchange-communication-holidays

Weekends are already excluded by _is_market_hours() — only add
weekday holidays here.
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
    return d in NSE_HOLIDAYS
