"""Match free slots against a request, and render the Telegram HTML reply."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from html import escape

from playtomic import Club, Court, Slot

DURATIONS = (60, 90)
NEAREST_EACH_SIDE = 3
NEXT_DAY_STARTS = 3
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


# --- matching --------------------------------------------------------------

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

def header(club: Club, d: date | None = None) -> str:
    title = f"🎾 <b>{escape(club.name)}</b>"
    return f"{title}, {fmt_day(d)}" if d else title


def footer(club: Club) -> str:
    return f'<a href="{escape(club.url, quote=True)}">Book in Playtomic</a>'


def render_match(club: Club, m: Match, next_day: tuple[date, list[Slot]] | None = None, now: datetime | None = None) -> str:
    t = fmt_time(m.when)
    lines = [header(club, m.when.date())]
    if m.exact:
        lines.append(f"✅ <b>{t} free:</b> " + " · ".join(fmt_offer(o) for o in m.exact))
    elif not m.fully_booked:
        lines.append(f"❌ <b>{t} taken.</b> Nearest: " + fmt_starts(m.nearest))
    else:
        lines.append(f"❌ <b>{fmt_day(m.when.date())} is fully booked.</b>")
        if next_day is not None:
            d, slots = next_day
            starts = first_free(slots, now or m.when)
            if starts:
                lines.append(f"First free on {fmt_day(d)}: " + fmt_starts(starts))
            else:
                lines.append(f"{fmt_day(d)} is fully booked too.")
    lines.append(footer(club))
    return "\n".join(lines)


def render_next(club: Club, found: tuple[datetime, list[Offer], list[tuple[datetime, int]]] | None, days: int) -> str:
    if found is None:
        return "\n".join([header(club), f"😕 Nothing free in the next {days} days.", footer(club)])
    start, offers, later = found
    lines = [
        header(club, start.date()),
        f"✅ <b>{fmt_time(start)} free:</b> " + " · ".join(fmt_offer(o) for o in offers),
    ]
    if later:
        lines.append("Then: " + fmt_starts(later))
    lines.append(footer(club))
    return "\n".join(lines)


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


def fmt_starts(starts: list[tuple[datetime, int]]) -> str:
    return " · ".join(f"{fmt_time(t)} ({n} court{'s' if n != 1 else ''})" for t, n in starts)


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
