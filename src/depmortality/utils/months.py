"""Calendar-month arithmetic. Months are integer indices: year * 12 + (month - 1)."""

from __future__ import annotations

import datetime as dt


def month_index(day: dt.date) -> int:
    return day.year * 12 + (day.month - 1)


def month_start(index: int) -> dt.date:
    return dt.date(index // 12, index % 12 + 1, 1)


def month_label(index: int) -> str:
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def add_months(day: dt.date, n: int) -> dt.date:
    """Same day-of-month n months later, clamped to the month's last day."""
    idx = month_index(day) + n
    first = month_start(idx)
    nxt = month_start(idx + 1)
    last_day = (nxt - dt.timedelta(days=1)).day
    return first.replace(day=min(day.day, last_day))
