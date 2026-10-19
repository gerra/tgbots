"""Read-only client for Playtomic's public availability endpoint.

No login, no booking: we only ever GET availability for one club.

Club metadata (tenant id, slug, court names, indoor/outdoor) lives in
club.json, written once by `probe.py --write`. The bot never searches for the
club at runtime.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time as _time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger(__name__)

LONDON = ZoneInfo("Europe/London")
CLUB_FILE = Path(__file__).with_name("club.json")

# The no-auth endpoint playtomic.com's own club pages call. The older
# api.playtomic.io/v1/availability now 403s, and api.app.playtomic.io wants a
# logged-in bearer token, so neither fits a read-only bot. Overridable in case
# Playtomic moves it again (PLAYTOMIC_AVAILABILITY_URL); probe.py tries all three.
AVAILABILITY_URL = "https://playtomic.com/api/clubs/availability"
# Slots come back as a zone-less start_date + start_time in UTC.
SLOT_TZ = ZoneInfo("UTC")
TIMEOUT = 10.0
CACHE_TTL = 60.0
USER_AGENT = "padium-padel-bot/1.0 (read-only availability lookup)"


class PlaytomicError(RuntimeError):
    """Playtomic is unreachable or returned an error status."""


class ShapeError(PlaytomicError):
    """Playtomic answered, but not in the shape we expect."""


@dataclass(frozen=True)
class Court:
    id: str
    name: str
    kind: str | None = None  # "indoor" / "outdoor" / None if unknown


@dataclass(frozen=True)
class Club:
    tenant_id: str
    name: str
    slug: str | None
    courts: dict[str, Court]
    timezone: str = "Europe/London"

    @property
    def url(self) -> str:
        if self.slug:
            return f"https://playtomic.com/clubs/{self.slug}"
        return f"https://app.playtomic.io/tenant/{self.tenant_id}"

    def court(self, resource_id: str) -> Court:
        return self.courts.get(resource_id) or Court(resource_id, "Court ?")


@dataclass(frozen=True)
class Slot:
    start: datetime  # aware, Europe/London
    duration: int  # minutes
    price: str | None  # raw, e.g. "80 GBP"
    court: Court


# --- club metadata ---------------------------------------------------------

def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def club_from_tenant(raw: dict[str, Any]) -> Club:
    """Build a Club from a /v1/tenants item (or the tenant object a club page embeds)."""
    try:
        tenant_id = raw["tenant_id"]
        name = raw["tenant_name"]
    except (KeyError, TypeError) as e:
        raise ShapeError(f"tenant missing {e}") from e
    courts: dict[str, Court] = {}
    for r in raw.get("resources") or []:
        if r.get("sport_id", "PADEL") != "PADEL" or r.get("is_active") is False:
            continue
        rid = r.get("resource_id") or r.get("id")
        if not rid:
            continue
        props = r.get("properties") or {}
        kind = props.get("resource_type")
        courts[rid] = Court(rid, r.get("name") or "Court ?", kind.lower() if kind else None)
    tz = (raw.get("address") or {}).get("timezone") or "Europe/London"
    return Club(tenant_id, name, raw.get("slug") or _slugify(name), courts, tz)


def club_to_json(club: Club) -> dict[str, Any]:
    return {
        "tenant_id": club.tenant_id,
        "name": club.name,
        "slug": club.slug,
        "timezone": club.timezone,
        "url": club.url,
        "courts": [{"id": c.id, "name": c.name, "kind": c.kind} for c in club.courts.values()],
    }


def load_club(path: Path = CLUB_FILE, tenant_id: str | None = None) -> Club:
    """Load club.json. PADIUM_TENANT_ID, if set, must match it."""
    tenant_id = tenant_id or os.environ.get("PADIUM_TENANT_ID") or None
    if not path.exists():
        raise SystemExit(f"{path.name} missing: run `python probe.py --write` once to create it")
    data = json.loads(path.read_text())
    if tenant_id and tenant_id != data["tenant_id"]:
        raise SystemExit(
            f"PADIUM_TENANT_ID={tenant_id} but {path.name} is for {data['tenant_id']}: "
            "re-run `python probe.py --tenant-id <id> --write`"
        )
    courts = {c["id"]: Court(c["id"], c["name"], c.get("kind")) for c in data.get("courts", [])}
    return Club(data["tenant_id"], data["name"], data.get("slug"), courts, data.get("timezone", "Europe/London"))


# --- availability ----------------------------------------------------------

def parse_availability(raw: Any, club: Club) -> list[Slot]:
    """Flatten an availability response into London-time slots.

    Shape: [{"resource_id", "start_date": "YYYY-MM-DD",
             "slots": [{"start_time": "HH:MM:SS", "duration": 60, "price": "80 GBP"}]}]
    """
    if not isinstance(raw, list):
        raise ShapeError(f"expected a list, got {type(raw).__name__}")
    slots: list[Slot] = []
    try:
        for item in raw:
            court = club.court(item["resource_id"])
            start_date = item["start_date"][:10]
            for s in item.get("slots") or []:
                naive = datetime.fromisoformat(f"{start_date}T{s['start_time']}")
                start = naive.replace(tzinfo=SLOT_TZ).astimezone(LONDON)
                slots.append(Slot(start, int(s["duration"]), s.get("price"), court))
    except (KeyError, TypeError, ValueError) as e:
        raise ShapeError(f"bad availability item: {e!r}") from e
    return slots


class PlaytomicClient:
    """Fetches one club's slots per London day, cached for CACHE_TTL seconds."""

    def __init__(self, club: Club, http: httpx.AsyncClient | None = None) -> None:
        self.club = club
        self._http = http or httpx.AsyncClient(
            timeout=TIMEOUT, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
        )
        self._cache: dict[date, tuple[float, list[Slot]]] = {}
        self._locks: dict[date, asyncio.Lock] = {}

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
            # Slots are UTC, so a London day can spill into the neighbouring
            # UTC date; keep only what actually starts on `d` in London.
            slots = sorted(
                (s for s in parse_availability(raw, self.club) if s.start.date() == d),
                key=lambda s: (s.start, s.court.name, s.duration),
            )
            self._cache[d] = (_time.monotonic(), slots)
            return slots

    async def _fetch(self, d: date) -> Any:
        params = {"tenant_id": self.club.tenant_id, "sport_id": "PADEL", "date": d.isoformat()}
        try:
            url = os.environ.get("PLAYTOMIC_AVAILABILITY_URL") or AVAILABILITY_URL
            r = await self._http.get(url, params=params)
        except httpx.HTTPError as e:
            raise PlaytomicError(f"request failed: {e!r}") from e
        if r.status_code != 200:
            raise PlaytomicError(f"HTTP {r.status_code}: {r.text[:200]}")
        try:
            return r.json()
        except ValueError as e:
            raise ShapeError(f"not JSON: {r.text[:200]}") from e
