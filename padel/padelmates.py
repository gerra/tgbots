"""Read-only client for Padel Mates' public slot-price endpoint.

Padium moved its bookings from Playtomic to Padel Mates, so this is where the
free courts are. No login, no booking: we only ever GET one club's slots.

Club metadata (club id, name, which courts are indoor/outdoor) lives in
club.json, written once by `probe.py --write`. The bot never searches for the
club at runtime.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger(__name__)

LONDON = ZoneInfo("Europe/London")
CLUB_FILE = Path(__file__).with_name("club.json")

# What padelmates.se's club page calls to draw its booking grid. Overridable
# (PADELMATES_SLOTS_URL) in case they move it.
SLOTS_URL = "https://fastapi-production-fargate.padelmates.io/player/player_booking/all_courts_slot_prices_v3"
# The endpoint checks Origin, and a bare client UA gets refused.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; padium-padel-bot/1.0; read-only availability lookup)",
    "Origin": "https://padelmates.se",
    "Accept": "application/json",
}
TIMEOUT = 10.0
CACHE_TTL = 60.0


class BookingSiteError(RuntimeError):
    """Padel Mates is unreachable or returned an error status."""


class ShapeError(BookingSiteError):
    """Padel Mates answered, but not in the shape we expect."""


@dataclass(frozen=True)
class Court:
    id: str
    name: str
    kind: str | None = None  # "indoor" / "outdoor" / None if unknown


@dataclass(frozen=True)
class Club:
    club_id: str
    name: str
    courts: dict[str, Court] = field(default_factory=dict)  # by court name
    timezone: str = "Europe/London"
    currency: str = "GBP"
    page: str | None = None  # public club page, if it has a nicer URL than the id

    @property
    def url(self) -> str:
        return self.page or f"https://padelmates.se/club/{self.club_id}"

    def court(self, court_id: str, name: str) -> Court:
        known = self.courts.get(name)
        return Court(court_id, name, known.kind if known else None)


@dataclass(frozen=True)
class Slot:
    start: datetime  # aware, Europe/London
    duration: int  # minutes
    price: str | None  # e.g. "80 GBP"
    court: Court


# --- club metadata ---------------------------------------------------------

def club_to_json(club: Club) -> dict[str, Any]:
    return {
        "club_id": club.club_id,
        "name": club.name,
        "timezone": club.timezone,
        "currency": club.currency,
        "url": club.url,
        "courts": [{"name": c.name, "kind": c.kind} for c in club.courts.values()],
    }


def load_club(path: Path = CLUB_FILE, club_id: str | None = None) -> Club:
    """Load club.json. PADIUM_CLUB_ID, if set, must match it."""
    club_id = club_id or os.environ.get("PADIUM_CLUB_ID") or None
    if not path.exists():
        raise SystemExit(f"{path.name} missing: run `python probe.py --club-id <id> --write` once to create it")
    data = json.loads(path.read_text())
    if club_id and club_id != data["club_id"]:
        raise SystemExit(
            f"PADIUM_CLUB_ID={club_id} but {path.name} is for {data['club_id']}: "
            "re-run `python probe.py --club-id <id> --write`"
        )
    courts = {c["name"]: Court(c["name"], c["name"], c.get("kind")) for c in data.get("courts", [])}
    return Club(
        data["club_id"], data["name"], courts,
        data.get("timezone", "Europe/London"), data.get("currency", "GBP"), data.get("url"),
    )


# --- availability ----------------------------------------------------------

def day_window(d: date, tz: ZoneInfo = LONDON) -> tuple[int, int]:
    """Epoch ms of local midnight on `d` and on the day after (23h/25h on clock-change days)."""
    start = datetime.combine(d, time(0), tzinfo=tz)
    end = datetime.combine(d + timedelta(days=1), time(0), tzinfo=tz)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def parse_slots(raw: Any, club: Club) -> list[Slot]:
    """Free slots from an all_courts_slot_prices_v3 response, in London time.

    Shape: {"allSlots": [{"courtName", "courtId", "duration": 60, "price": 80.0,
             "startTimestamp": <epoch ms>, "startTime": "05:00" (UTC), "reservedIntersection": false}],
            "originalCourts": [{"_id", "name", "indoor": true}],
            "advance_booking_horizon_ends_at": <epoch ms>, ...}
    allSlots lists only bookable slots, one per court, start and duration;
    reservedIntersection is skipped anyway in case that ever changes.
    """
    if not isinstance(raw, dict) or not isinstance(raw.get("allSlots"), list):
        raise ShapeError(f"expected {{allSlots: [...]}}, got {_sketch(raw)}")
    indoor = {c.get("_id"): c.get("indoor") for c in raw.get("originalCourts") or [] if isinstance(c, dict)}
    slots: list[Slot] = []
    try:
        for s in raw["allSlots"]:
            if s.get("reservedIntersection"):
                continue
            court = club.court(str(s.get("courtId") or s["courtName"]), s["courtName"])
            live = indoor.get(s.get("courtId"))
            if isinstance(live, bool):
                court = Court(court.id, court.name, "indoor" if live else "outdoor")
            start = datetime.fromtimestamp(int(s["startTimestamp"]) / 1000, timezone.utc).astimezone(LONDON)
            slots.append(Slot(start, int(s["duration"]), _price(s.get("price"), club.currency), court))
    except (KeyError, TypeError, ValueError) as e:
        raise ShapeError(f"bad slot: {e!r}") from e
    return slots


def parse_horizon(raw: Any) -> datetime | None:
    """Courts open for booking up to this moment (a rolling ~7 days ahead)."""
    ms = raw.get("advance_booking_horizon_ends_at") if isinstance(raw, dict) else None
    if not isinstance(ms, (int, float)):
        return None
    return datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone(LONDON)


def _price(value: Any, currency: str) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return f"{value} {currency}"
    return str(value)


def _sketch(v: Any) -> str:
    if isinstance(v, dict):
        return "{" + ", ".join(list(v)[:10]) + "}"
    return type(v).__name__


class PadelMatesClient:
    """Fetches one club's free slots per London day, cached for CACHE_TTL seconds."""

    def __init__(self, club: Club, http: httpx.AsyncClient | None = None) -> None:
        self.club = club
        self._http = http or httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS)
        self._cache: dict[date, tuple[float, list[Slot]]] = {}
        self._locks: dict[date, asyncio.Lock] = {}
        # Booking window end, from the latest response: no slots exist past it.
        self.horizon: datetime | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    async def day(self, d: date) -> list[Slot]:
        """All free slots starting on London date `d`, sorted by start time."""
        lock = self._locks.setdefault(d, asyncio.Lock())
        async with lock:  # concurrent /padel for the same day share one request
            hit = self._cache.get(d)
            if hit and _time.monotonic() - hit[0] < CACHE_TTL:
                return hit[1]
            raw = await self._fetch(d)
            self.horizon = parse_horizon(raw) or self.horizon
            slots = sorted(
                (s for s in parse_slots(raw, self.club) if s.start.date() == d),
                key=lambda s: (s.start, s.court.name, s.duration),
            )
            self._cache[d] = (_time.monotonic(), slots)
            return slots

    def bookable(self, when: datetime) -> bool:
        """False once `when` is past the booking window (call after day())."""
        return self.horizon is None or when < self.horizon

    async def _fetch(self, d: date) -> Any:
        start, end = day_window(d)
        params = {"club_id": self.club.club_id, "start_datetime": start, "end_datetime": end, "lang": "en"}
        try:
            r = await self._http.get(os.environ.get("PADELMATES_SLOTS_URL") or SLOTS_URL, params=params)
        except httpx.HTTPError as e:
            raise BookingSiteError(f"request failed: {e!r}") from e
        if r.status_code != 200:
            raise BookingSiteError(f"HTTP {r.status_code}: {r.text[:200]}")
        try:
            return r.json()
        except ValueError as e:
            raise ShapeError(f"not JSON: {r.text[:200]}") from e
