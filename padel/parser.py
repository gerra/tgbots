"""Turn `/padel` arguments into a London date + time (or time range).

Accepted (in any order, case-insensitive):
  day:   today, tomorrow, mon..sun (or full names), dd/mm, dd/mm/yyyy
  time:  18:00, 18, 18.30, 6pm, 6:30pm, 6 pm
  range: 4pm-6pm, 16:00-18:00, 4-6pm, 4pm to 6pm (start times, both ends included)
  kind:  indoor / outdoor (also indoors, inside, outdoors, outside), optional
  for:   1h, 1.5h, 2h, 90m, 1h30, or a span like 1-2h / 60-120m, optional (default 60-90 min)
Or `next`, optionally with a kind and length.
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
KINDS = {"indoor": "indoor", "indoors": "indoor", "inside": "indoor",
         "outdoor": "outdoor", "outdoors": "outdoor", "outside": "outdoor"}

_UNIT = r"(h|hr|hrs|hour|hours|m|min|mins)"
_LENGTH_RE = re.compile(rf"^(\d+(?:\.\d+)?){_UNIT}?(?:-(\d+(?:\.\d+)?){_UNIT})?$")
_H_M_RE = re.compile(r"^(\d)h(\d{1,2})m?$")  # 1h30
_DATE_RE = re.compile(r"^(\d{1,2})[/.](\d{1,2})(?:[/.](\d{2}|\d{4}))?$")
_TIME_RE = re.compile(r"^(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)?$")


class ParseError(ValueError):
    """Input we can't understand; the bot answers with usage help."""


Minutes = tuple[int, int]  # (shortest, longest) booking length wanted, inclusive


@dataclass(frozen=True)
class SlotQuery:
    """A specific day and start time, as a London-aware datetime."""
    when: datetime
    kind: str | None = None  # "indoor" / "outdoor" / None for any
    minutes: Minutes | None = None  # None: the default 60-90


@dataclass(frozen=True)
class RangeQuery:
    """Every free start time from `start` to `end`, both included."""
    start: datetime
    end: datetime
    kind: str | None = None
    minutes: Minutes | None = None


@dataclass(frozen=True)
class NextQuery:
    """`/padel next`: earliest free slot from now."""
    kind: str | None = None
    minutes: Minutes | None = None


def parse(text: str | list[str], now: datetime) -> SlotQuery | RangeQuery | NextQuery:
    """Parse command arguments. `now` must be timezone-aware."""
    tokens = text.split() if isinstance(text, str) else list(text)
    tokens = [t.strip().lower() for t in tokens if t.strip()]
    kinds = {KINDS[t] for t in tokens if t in KINDS}
    if len(kinds) > 1:
        raise ParseError("indoor or outdoor, not both")
    kind = kinds.pop() if kinds else None
    tokens = [t for t in tokens if t not in KINDS]
    lengths = [m for t in tokens if (m := parse_length(t))]
    if len(lengths) > 1:
        raise ParseError("one length, please")
    minutes = lengths[0] if lengths else None
    tokens = [t for t in tokens if not parse_length(t)]
    if not tokens:
        raise ParseError("empty")
    if tokens == ["next"]:
        return NextQuery(kind, minutes)

    joined = " ".join(tokens)
    joined = re.sub(r"\s*(?:-|–|—|\bto\b)\s*", "-", joined)  # "4pm - 6pm", "4pm to 6pm" -> "4pm-6pm"
    joined = re.sub(r"(\d)\s+(am|pm)\b", r"\1\2", joined)  # "6 pm" -> "6pm"
    parts = joined.split()
    if len(parts) != 2:
        raise ParseError("need a day and a time")

    now = now.astimezone(LONDON)
    a, b = parts
    if span := parse_span(b):
        day_tok = a
    elif span := parse_span(a):
        day_tok = b
    else:
        raise ParseError(f"bad time: {joined}")
    start, end = span
    d = parse_day(day_tok, end or start, now)
    if end is None:
        return SlotQuery(datetime.combine(d, start, tzinfo=LONDON), kind, minutes)
    return RangeQuery(datetime.combine(d, start, tzinfo=LONDON), datetime.combine(d, end, tzinfo=LONDON), kind, minutes)


def parse_length(tok: str) -> Minutes | None:
    """"2h" -> (120, 120), "1.5h"/"90m"/"1h30" -> (90, 90), "1-2h" -> (60, 120). None if it isn't one."""
    if m := _H_M_RE.match(tok):
        n = int(m.group(1)) * 60 + int(m.group(2))
        return n, n
    m = _LENGTH_RE.match(tok)
    if not m:
        return None
    lo, lo_unit, hi, hi_unit = m.groups()
    unit = hi_unit or lo_unit
    if unit is None or (hi is not None and lo_unit is not None and hi_unit is None):
        return None  # "18", "18-20": times, not lengths
    to_min = lambda v, u: round(float(v) * (60 if u.startswith("h") else 1))  # noqa: E731
    lo_min = to_min(lo, lo_unit or unit)
    hi_min = to_min(hi, unit) if hi is not None else lo_min
    if not 15 <= lo_min <= hi_min <= 300:
        raise ParseError(f"bad length: {tok}")
    return lo_min, hi_min


def parse_span(tok: str) -> tuple[time, time | None] | None:
    """"18:00" -> (18:00, None); "4-6pm" -> (16:00, 18:00). None if it isn't a time."""
    if "-" not in tok:
        t = parse_time(tok)
        return (t, None) if t else None
    a, b = tok.split("-", 1)
    end = parse_time(b)
    start = parse_time(a)
    if end is None or start is None:
        return None
    suffix = re.search(r"(am|pm)$", b)
    if suffix and not re.search(r"(am|pm)$", a):
        # "4-6pm" is 4pm-6pm, but "11-1pm" is 11am-1pm
        for s in (suffix.group(1), "am"):
            t = parse_time(a + s)
            if t is not None and t < end:
                start = t
                break
    elif not suffix and end <= start and end.hour < 12:
        end = end.replace(hour=end.hour + 12)  # "6pm-8" means until 8pm
    if end <= start:
        raise ParseError(f"range ends before it starts: {tok}")
    return start, end


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
