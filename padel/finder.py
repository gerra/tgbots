"""Match free slots against a request, and render the Telegram HTML reply."""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from html import escape

from padelmates import Club, Court, Slot

DURATIONS = (60, 90)
NEAREST_EACH_SIDE = 3
NEXT_DAY_STARTS = 3
RANGE_MAX_STARTS = 8  # a long range lists this many start times, then "and N more"
CURRENCY = {"GBP": "£", "EUR": "€", "USD": "$"}


@dataclass
class Offer:
    """One court free at one start time, with a price per bookable duration."""
    court: Court
    prices: dict[int, str | None] = field(default_factory=dict)


Groups = dict[datetime, list[Offer]]


@dataclass
class Match:
    when: datetime
    exact: list[Offer]  # courts free at exactly `when`
    nearest: list[tuple[datetime, int]]  # (start, courts) on the same day, closest first
    fully_booked: bool  # nothing left at all on that day


@dataclass
class RangeMatch:
    start: datetime
    end: datetime
    found: list[tuple[datetime, list[Offer]]]  # free start times within [start, end]
    nearest: list[tuple[datetime, int]]  # if none within: closest outside, closest first
    fully_booked: bool


# --- matching --------------------------------------------------------------

def only(slots: list[Slot], kind: str | None) -> list[Slot]:
    """Keep courts of one kind (indoor/outdoor); None keeps all."""
    return slots if kind is None else [s for s in slots if s.court.kind == kind]


def group(slots: list[Slot], now: datetime) -> Groups:
    """Free 60/90 min slots from `now` on, grouped by start time (sorted)."""
    by_start: dict[datetime, dict[str, Offer]] = {}
    for s in slots:
        if s.duration not in DURATIONS or s.start < now:
            continue
        offers = by_start.setdefault(s.start, {})
        offer = offers.setdefault(s.court.id, Offer(s.court))
        offer.prices[s.duration] = s.price
    return {
        start: sorted(offers.values(), key=lambda o: _natural(o.court.name))
        for start, offers in sorted(by_start.items())
    }


def match(slots: list[Slot], when: datetime, now: datetime) -> Match:
    groups = group(slots, now)
    exact = groups.get(when, [])
    nearest: list[tuple[datetime, int]] = []
    if not exact:
        before = [t for t in groups if t < when][-NEAREST_EACH_SIDE:]
        after = [t for t in groups if t > when][:NEAREST_EACH_SIDE]
        nearest = [
            (t, len(groups[t]))
            for t in sorted(before + after, key=lambda t: (abs(t - when), t))
        ]
    return Match(when, exact, nearest, fully_booked=not groups)


def match_range(slots: list[Slot], start: datetime, end: datetime, now: datetime) -> RangeMatch:
    groups = group(slots, now)
    found = [(t, offers) for t, offers in groups.items() if start <= t <= end]
    nearest: list[tuple[datetime, int]] = []
    if not found:
        before = [t for t in groups if t < start][-NEAREST_EACH_SIDE:]
        after = [t for t in groups if t > end][:NEAREST_EACH_SIDE]
        distance = lambda t: start - t if t < start else t - end  # noqa: E731
        nearest = [(t, len(groups[t])) for t in sorted(before + after, key=lambda t: (distance(t), t))]
    return RangeMatch(start, end, found, nearest, fully_booked=not groups)


def first_free(slots: list[Slot], now: datetime, n: int = NEXT_DAY_STARTS) -> list[tuple[datetime, int]]:
    """The first `n` free start times with how many courts each."""
    return [(t, len(offers)) for t, offers in list(group(slots, now).items())[:n]]


def earliest(slots: list[Slot], now: datetime) -> tuple[datetime, list[Offer], list[tuple[datetime, int]]] | None:
    """Earliest free start, its courts, and the next few starts that day."""
    groups = group(slots, now)
    if not groups:
        return None
    starts = list(groups)
    return starts[0], groups[starts[0]], [(t, len(groups[t])) for t in starts[1:1 + NEAREST_EACH_SIDE]]


# --- rendering (Telegram HTML) ---------------------------------------------

def header(club: Club, d: date | None = None, kind: str | None = None) -> str:
    title = f"🎾 <b>{escape(club.name)}</b>" + (f", {fmt_day(d)}" if d else "")
    return f"{title} · {kind}" if kind else title


def none_left(d: date, kind: str | None, too: bool = False) -> str:
    """"Sat 24 Oct is fully booked." / "No indoor courts free on Sat 24 Oct." (+ too/either)"""
    if kind:
        return f"No {kind} courts free on {fmt_day(d)}{' either' if too else ''}."
    return f"{fmt_day(d)} is fully booked{' too' if too else ''}."


def next_day_lines(next_day: tuple[date, list[Slot]], kind: str | None, now: datetime) -> list[str]:
    d, slots = next_day
    starts = first_free(slots, now)
    return [f"First free on {fmt_day(d)}:", *fmt_starts(starts)] if starts else [none_left(d, kind, too=True)]


def render_match(club: Club, m: Match, next_day: tuple[date, list[Slot]] | None = None, now: datetime | None = None,
                 kind: str | None = None) -> str:
    t = fmt_time(m.when)
    lines = [header(club, m.when.date(), kind)]
    if m.exact:
        lines += [f"✅ <b>{t} free:</b>", *bullets(fmt_offer(o) for o in m.exact)]
    elif not m.fully_booked:
        lines += [f"❌ <b>{t} taken.</b> Nearest:", *fmt_starts(m.nearest)]
    else:
        lines.append(f"❌ <b>{none_left(m.when.date(), kind)}</b>")
        if next_day is not None:
            lines += next_day_lines(next_day, kind, now or m.when)
    return "\n".join(lines)


def render_range(club: Club, rm: RangeMatch, next_day: tuple[date, list[Slot]] | None = None,
                 now: datetime | None = None, kind: str | None = None) -> str:
    span = f"{fmt_time(rm.start)}–{fmt_time(rm.end)}"
    lines = [header(club, rm.start.date(), kind)]
    if rm.found:
        lines.append(f"✅ <b>Free {span}:</b>")
        for t, offers in rm.found[:RANGE_MAX_STARTS]:
            lines += [f"<b>{fmt_time(t)}</b>", *bullets(fmt_offer(o) for o in offers)]
        if len(rm.found) > RANGE_MAX_STARTS:
            more = rm.found[RANGE_MAX_STARTS:]
            lines.append(f"…and {len(more)} more start times, up to {fmt_time(more[-1][0])}")
    elif not rm.fully_booked:
        lines += [f"❌ <b>Nothing free {span}.</b> Nearest:", *fmt_starts(rm.nearest)]
    else:
        lines.append(f"❌ <b>{none_left(rm.start.date(), kind)}</b>")
        if next_day is not None:
            lines += next_day_lines(next_day, kind, now or rm.start)
    return "\n".join(lines)


def render_next(club: Club, found: tuple[datetime, list[Offer], list[tuple[datetime, int]]] | None, days: int,
                kind: str | None = None) -> str:
    if found is None:
        what = f"No {kind} courts" if kind else "Nothing"
        return "\n".join([header(club, kind=kind), f"😕 {what} free in the next {days} days."])
    start, offers, later = found
    lines = [header(club, start.date(), kind), f"✅ <b>{fmt_time(start)} free:</b>", *bullets(fmt_offer(o) for o in offers)]
    if later:
        lines += ["Then:", *fmt_starts(later)]
    return "\n".join(lines)


def render_not_open(club: Club, when: datetime, horizon: datetime, now: datetime, kind: str | None = None) -> str:
    """`when` is past the booking window; say roughly when it opens."""
    window = horizon.astimezone(timezone.utc) - now.astimezone(timezone.utc)  # absolute, DST-safe
    days = max(1, round(window.total_seconds() / 86400))
    opens = (when.astimezone(timezone.utc) - window).astimezone(when.tzinfo)
    half_hours = round((opens.hour * 60 + opens.minute) / 30)  # "around 20:30", not 20:29
    opens = opens.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=30 * half_hours)
    line = (
        f"🕒 <b>{fmt_time(when)} isn't bookable yet.</b> Courts open {days} days ahead, "
        f"so try from around {fmt_day(opens.date())} {fmt_time(opens)}."
    )
    return "\n".join([header(club, when.date(), kind), line])


def fmt_offer(o: Offer) -> str:
    court = escape(o.court.name) + (f" ({escape(o.court.kind)})" if o.court.kind else "")
    durations = sorted(o.prices)
    prices = [fmt_price(o.prices[d]) for d in durations]
    prices = [p for p in prices if p]
    if len(set(prices)) <= 1:
        price = prices[0] if prices else ""
    else:
        price = "/".join(prices)
    text = f"{court} {'/'.join(map(str, durations))} min"
    return f"{text} {price}" if price else text


def fmt_starts(starts: list[tuple[datetime, int]]) -> list[str]:
    return bullets(f"{fmt_time(t)} ({n} court{'s' if n != 1 else ''})" for t, n in starts)


def bullets(items: Iterable[str]) -> list[str]:
    """One line per item: compact replies were too wide for a phone in a busy group."""
    return [f"• {item}" for item in items]


def fmt_price(raw: str | None) -> str | None:
    """"80 GBP" -> "£80", "42.5 GBP" -> "£42.50"."""
    if not raw:
        return None
    m = re.match(r"^\s*([\d.]+)\s*([A-Z]{3})?\s*$", str(raw))
    if not m:
        return escape(str(raw))
    amount = float(m.group(1))
    num = f"{amount:.0f}" if amount == int(amount) else f"{amount:.2f}"
    cur = m.group(2) or ""
    sym = CURRENCY.get(cur)
    return f"{sym}{num}" if sym else f"{num} {cur}".strip()


def fmt_time(t: datetime) -> str:
    return t.strftime("%H:%M")


def fmt_day(d: date) -> str:
    return f"{d:%a} {d.day} {d:%b}"


def _natural(name: str) -> list:
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", name)]
