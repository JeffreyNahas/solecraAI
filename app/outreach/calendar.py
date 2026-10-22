"""Québec business days: weekdays except statutory holidays (Loi sur les normes du travail).

Used for the outreach timetable (no drafting or sending on holidays) and draft expiry
(N business days). Decided 2026-10-22: weekdays without Québec holidays.
"""

from datetime import date, timedelta


def _easter(year: int) -> date:
    """Gregorian Easter Sunday (anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month, day = divmod(h + l_ - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _nth_monday(year: int, month: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(7 - first.weekday()) % 7 + 7 * (n - 1))


def quebec_holidays(year: int) -> set[date]:
    easter = _easter(year)
    may_24 = date(year, 5, 24)
    return {
        date(year, 1, 1),  # Jour de l'An
        easter - timedelta(days=2),  # Vendredi saint
        easter + timedelta(days=1),  # Lundi de Pâques
        may_24 - timedelta(days=may_24.weekday()),  # Journée nationale des patriotes (Monday before May 25)
        date(year, 6, 24),  # Fête nationale
        date(year, 7, 1),  # Fête du Canada
        _nth_monday(year, 9, 1),  # Fête du Travail
        _nth_monday(year, 10, 2),  # Action de grâce
        date(year, 12, 25),  # Noël
    }


def is_business_day(day: date) -> bool:
    return day.weekday() < 5 and day not in quebec_holidays(day.year)


def business_days_between(start: date, end: date) -> int:
    """Business days after `start` up to and including `end` (0 if end <= start)."""
    count, day = 0, start
    while day < end:
        day += timedelta(days=1)
        count += is_business_day(day)
    return count
