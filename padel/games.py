"""Games the group booked: who's playing, who has paid the booker back.

Padel Mates won't tell a read-only bot who booked or paid (that needs the
booker's login), so players say it themselves with the card's buttons.
Games live in the chat's chat_data, which PicklePersistence keeps on disk.
"""
from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html import escape

from finder import fmt_day, fmt_time
from padelmates import Club
from parser import ParseError, parse_day, parse_time

SPOTS = 4  # padel is doubles
KEEP_FOR = timedelta(days=14)  # forget games this long after they're played
CURRENCY = {"GBP": "£", "EUR": "€", "USD": "$"}


@dataclass
class Player:
    id: int
    name: str
    paid: bool = False


@dataclass
class Game:
    id: str
    when: datetime  # aware, Europe/London
    court: str
    kind: str | None  # indoor / outdoor
    duration: int | None  # minutes
    price: float | None  # whole court, as charged by the club
    currency: str
    players: list[Player] = field(default_factory=list)  # players[0] is the booker
    message_id: int | None = None
    cancelled: bool = False

    @property
    def booker(self) -> Player:
        return self.players[0]

    @property
    def share(self) -> float | None:
        return self.price / SPOTS if self.price else None

    @property
    def unpaid(self) -> list[Player]:
        return [p for p in self.players if not p.paid]

    def player(self, user_id: int) -> Player | None:
        return next((p for p in self.players if p.id == user_id), None)

    # Each action returns a short message for the button's toast.

    def join(self, user_id: int, name: str) -> str:
        if self.player(user_id):
            return "You're already in."
        if len(self.players) >= SPOTS:
            return "Game's full."
        self.players.append(Player(user_id, name))
        return "You're in 🎾"

    def leave(self, user_id: int) -> str:
        p = self.player(user_id)
        if p is None:
            return "You weren't in."
        if p is self.booker:
            return "You booked it: cancel the game instead."
        self.players.remove(p)
        return "You're out."

    def toggle_paid(self, user_id: int) -> str:
        p = self.player(user_id)
        if p is None:
            return "Tap 🙋 I'm in first."
        if p is self.booker:
            return "You booked it, so you've paid."
        p.paid = not p.paid
        return "Marked as paid 💸" if p.paid else "Marked as not paid."


def new_game(when: datetime, court: str, kind: str | None, duration: int | None, price: float | None,
             currency: str, booker_id: int, booker_name: str) -> Game:
    return Game(secrets.token_hex(3), when, court, kind, duration, price, currency,
                [Player(booker_id, booker_name, paid=True)])


def prune(games: dict[str, Game], now: datetime) -> None:
    for gid in [gid for gid, g in games.items() if g.when + KEEP_FOR < now or g.cancelled]:
        del games[gid]


# --- /booked arguments -----------------------------------------------------

@dataclass
class Booked:
    when: datetime
    court: str
    duration: int | None
    price: float | None


_PRICE = re.compile(r"^(?:£(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)(?:gbp|£))$")
_DURATION = re.compile(r"^(60|90|120)(?:m|min|mins)?$")


def parse_booked(args: list[str], now: datetime, court_names: list[str]) -> Booked:
    """`/booked sat 18:00 court 3 90 £120`: day, time, then court, duration and price in any order."""
    tokens = [t.lower() for t in args]
    if len(tokens) < 2:
        raise ParseError("need a day and a time")
    t = parse_time(tokens[1])
    if t is None:
        raise ParseError(f"bad time: {tokens[1]}")
    d = parse_day(tokens[0], t, now)
    when = datetime.combine(d, t, tzinfo=now.tzinfo)
    duration = price = None
    words: list[str] = []
    for tok in tokens[2:]:
        if m := _PRICE.match(tok):
            price = float(m.group(1) or m.group(2))
        elif (m := _DURATION.match(tok)) and duration is None:
            duration = int(m.group(1))
        else:
            words.append(tok)
    return Booked(when, match_court(words, court_names), duration, price)


def match_court(words: list[str], court_names: list[str]) -> str:
    """"3" -> "Court 3", "centre" -> "Centre Court", "outdoor 7" -> "Outdoor Court 7"."""
    words = [w for w in words if w not in ("court", "on", "at")]
    if not words:
        return ""
    def hits(ws: list[str]) -> list[str]:
        return [n for n in court_names
                if all(w in n.lower().replace(".", " ").split() or (not w.isdigit() and w in n.lower()) for w in ws)]

    # All the words first; failing that, just the court number ("outdoor 8" still finds "Court 8").
    for ws in (words, [w for w in words if w.isdigit()]):
        found = hits(ws) if ws else []
        if len(found) == 1:
            return found[0]
    return " ".join(words).title()


# --- rendering -------------------------------------------------------------

def money(amount: float | None, currency: str) -> str:
    if amount is None:
        return ""
    num = f"{amount:.0f}" if amount == int(amount) else f"{amount:.2f}"
    sym = CURRENCY.get(currency)
    return f"{sym}{num}" if sym else f"{num} {currency}"


def mention(p: Player) -> str:
    return f'<a href="tg://user?id={p.id}">{escape(p.name)}</a>'


def describe(g: Game) -> str:
    """"Sat 24 Oct 18:00 · Court 3 (indoor) · 90 min"."""
    parts = [f"{fmt_day(g.when.date())} {fmt_time(g.when)}"]
    if g.court:
        parts.append(escape(g.court) + (f" ({escape(g.kind)})" if g.kind else ""))
    if g.duration:
        parts.append(f"{g.duration} min")
    return " · ".join(parts)


def render_card(g: Game, club: Club) -> str:
    if g.cancelled:
        return f"❌ <s>{describe(g)}</s>\nCancelled by {mention(g.booker)}."
    lines = [f"🎾 <b>{describe(g)}</b>", escape(club.name)]
    if g.price:
        lines.append(f"💷 {money(g.price, g.currency)} total · {money(g.share, g.currency)} each, pay {mention(g.booker)}")
    owes = f"owes {money(g.share, g.currency)}" if g.share else "hasn't paid"
    for i in range(SPOTS):
        if i >= len(g.players):
            lines.append(f"{i + 1}. ➕ free spot")
            continue
        p = g.players[i]
        status = "📌 booked" if i == 0 else ("✅ paid" if p.paid else f"⏳ {owes}")
        lines.append(f"{i + 1}. {mention(p)} {status}")
    if len(g.players) == SPOTS and not g.unpaid:
        lines.append("All paid 🙌")
    return "\n".join(lines)


def render_reminder(g: Game) -> str:
    who = ", ".join(mention(p) for p in g.unpaid)
    amount = f" {money(g.share, g.currency)}" if g.share else ""
    return f"🔔 {who}: please pay {mention(g.booker)}{amount} for {describe(g)}."


def render_list(games: list[Game]) -> str:
    if not games:
        return "No games tracked here. Book one from a /padel reply (📌 buttons) or with /booked."
    lines = ["🎾 <b>Games</b>"]
    for g in sorted(games, key=lambda g: g.when):
        unpaid = len(g.unpaid)
        status = "all paid" if not unpaid else f"{unpaid} to pay"
        lines.append(f"• {describe(g)}: {len(g.players)}/{SPOTS} in, {status}")
    return "\n".join(lines)
