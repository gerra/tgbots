"""Turn `/padel` arguments into a London date + time.

Accepted (in either order, case-insensitive):
  day:  today, tomorrow, mon..sun (or full names), dd/mm, dd/mm/yyyy
  time: 18:00, 18, 18.30, 6pm, 6:30pm, 6 pm
Or the single word `next`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")

WEEKDAYS = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}
TODAY_WORDS = {"today", "tonight"}
TOMORROW_WORDS = {"tomorrow", "tmrw", "tmr", "tom"}

_DATE_RE = re.compile(r"^(\d{1,2})[/.](\d{1,2})(?:[/.](\d{2}|\d{4}))?$")
_TIME_RE = re.compile(r"^(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)?$")


class ParseError(ValueError):
    """Input we can't understand; the bot answers with usage help."""


@dataclass(frozen=True)
class SlotQuery:
    """A specific day and start time, as a London-aware datetime."""
    when: datetime


@dataclass(frozen=True)
class NextQuery:
    """`/padel next`: earliest free slot from now."""


def parse(text: str | list[str], now: datetime) -> SlotQuery | NextQuery:
    """Parse command arguments. `now` must be timezone-aware."""
    tokens = text.split() if isinstance(text, str) else list(text)
    tokens = [t.strip().lower() for t in tokens if t.strip()]
    if not tokens:
        raise ParseError("empty")
    if tokens == ["next"]:
        return NextQuery()

    # "6 pm" -> "6pm"
    merged: list[str] = []
    for tok in tokens:
        if tok in ("am", "pm") and merged and re.fullmatch(r"\d{1,2}(?:[:.]\d{2})?", merged[-1]):
            merged[-1] += tok
        else:
            merged.append(tok)
    if len(merged) != 2:
        raise ParseError("need a day and a time")

    now = now.astimezone(LONDON)
    a, b = merged
    t = parse_time(b)
    if t is not None:
        day_tok = a
    else:
        t = parse_time(a)
        day_tok = b
        if t is None:
            raise ParseError(f"bad time: {b}")
    d = parse_day(day_tok, t, now)
    return SlotQuery(datetime.combine(d, t, tzinfo=LONDON))


def parse_time(tok: str) -> time | None:
    m = _TIME_RE.match(tok)
    if not m:
        return None
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if minute > 59:
        return None
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    elif hour > 23:
        return None
    return time(hour, minute)


def parse_day(tok: str, t: time, now: datetime) -> date:
    today = now.date()
    if tok in TODAY_WORDS:
        return today
    if tok in TOMORROW_WORDS:
        return today + timedelta(days=1)
    if tok in WEEKDAYS:
        ahead = (WEEKDAYS[tok] - today.weekday()) % 7
        if ahead == 0 and t <= now.time().replace(tzinfo=None):
            ahead = 7  # it's that day but the time has gone: next week
        return today + timedelta(days=ahead)
    m = _DATE_RE.match(tok)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year_s = m.group(3)
        year = (2000 + int(year_s) if len(year_s) == 2 else int(year_s)) if year_s else today.year
        try:
            d = date(year, month, day)
        except ValueError as e:
            raise ParseError(f"bad date: {tok}") from e
        if not year_s and d < today:
            try:
                d = d.replace(year=today.year + 1)  # 05/01 in December means next January
            except ValueError as e:  # 29/02 with no leap year next
                raise ParseError(f"bad date: {tok}") from e
        return d
    raise ParseError(f"bad day: {tok}")
