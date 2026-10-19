"""Hit Padel Mates live, print the real response shapes, set up Padium Canary Wharf.

    python probe.py --club-id <id>                # print what comes back for today + Saturday
    python probe.py --club-id <id> --write        # ...and write club.json + tests/fixtures/live_*.json
    python probe.py --club-id <id> --fixtures     # live fixtures only (any club), no club.json
    python probe.py --write                       # id from PADIUM_CLUB_ID (.env) or club.json

The club id is the 32-hex string in the padelmates.se/club/<id> URL that
the Padel Mates app or website shows for Padium Canary Wharf.

Padel Mates' API is unofficial: this is how we find out what it really sends.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from padelmates import (
    CLUB_FILE,
    HEADERS,
    LONDON,
    SLOTS_URL,
    Club,
    Court,
    club_to_json,
    day_window,
    parse_horizon,
    parse_slots,
)

HERE = Path(__file__).parent
FIXTURES = HERE / "tests" / "fixtures"
NAME = "Padium Canary Wharf"
CLUB_PAGES = ["https://padelmates.se/club/padium"]  # the link Padium's listings use

http = httpx.Client(timeout=15, headers=HEADERS, follow_redirects=True)


def shape(v: Any, depth: int = 0) -> str:
    """Compact type sketch of a JSON value, e.g. {allSlots: [25 × {courtName: str, ...}]}."""
    if depth > 3:
        return "…"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{k}: {shape(x, depth + 1)}" for k, x in list(v.items())[:30]) + "}"
    if isinstance(v, list):
        return f"[{len(v)} × {shape(v[0], depth + 1)}]" if v else "[]"
    return type(v).__name__


def discover_ids() -> list[str]:
    """32-hex ids that padelmates.se/club/padium (or wherever it redirects) mentions."""
    ids: list[str] = []
    for url in CLUB_PAGES:
        try:
            r = http.get(url, headers={"Accept": "text/html"})
        except httpx.HTTPError as e:
            print(f"### {url} -> {e!r}")
            continue
        found = re.findall(r"[0-9a-f]{32}", str(r.url) + r.text)
        print(f"### {url} -> HTTP {r.status_code}, final URL {r.url}, ids: {sorted(set(found))[:10]}")
        ids += found
    return list(dict.fromkeys(ids))


def fetch(club_id: str, d) -> Any | None:
    start, end = day_window(d)
    params = {"club_id": club_id, "start_datetime": start, "end_datetime": end, "lang": "en"}
    try:
        r = http.get(SLOTS_URL, params=params)
    except httpx.HTTPError as e:
        print(f"\n### {d} -> {e!r}")
        return None
    print(f"\n### slots {d}\n    GET {r.url}\n    -> HTTP {r.status_code} {r.headers.get('content-type', '')}")
    if r.status_code != 200:
        print("    " + r.text[:300].replace("\n", " "))
        return None
    try:
        data = r.json()
    except ValueError:
        print("    (not JSON) " + r.text[:300].replace("\n", " "))
        return None
    print("    shape: " + shape(data))
    return data


def summarise(raw: Any, club: Club) -> None:
    items = raw.get("allSlots") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        print("    !! no allSlots list: the bot's parser needs updating")
        return
    print(f"    allSlots: {len(items)}; reserved: {sum(1 for s in items if s.get('reservedIntersection'))}; "
          f"horizon: {parse_horizon(raw)}; too early: {raw.get('too_early_for_advance_booking')}")
    for s in items[:2]:
        print(f"    raw: {json.dumps(s)[:600]}")
    print(f"    slot keys: {sorted({k for s in items for k in s})}")
    slots = parse_slots(raw, club)
    if not slots:
        print("    no free slots")
        return
    first = next(s for s in items if not s.get("reservedIntersection"))
    print(f"    first free: startTime={first.get('startTime')!r} -> London {slots[0].start:%a %d %H:%M}")
    print(f"    free, London time: {min(s.start for s in slots):%H:%M} .. {max(s.start for s in slots):%H:%M}")
    print(f"    courts: {sorted({(s.court.name, s.court.kind) for s in slots})}")
    print(f"    durations: {dict(Counter(s.duration for s in slots))}; prices: {dict(Counter(s.price for s in slots).most_common(5))}")


# Only what the bot reads. The rest of the response includes other players'
# bookings (names, user ids, booking codes), which must not land in a public repo.
KEEP = ("allSlots", "originalCourts", "too_early_for_advance_booking", "advance_booking_horizon_ends_at")
COURT_KEEP = ("_id", "name", "indoor", "slot_duration", "future_booking_days")


def scrub(raw: Any) -> Any:
    if not isinstance(raw, dict):
        return raw
    out = {k: raw[k] for k in KEEP if k in raw}
    out["originalCourts"] = [{k: c.get(k) for k in COURT_KEEP} for c in raw.get("originalCourts") or []]
    return out


def natural(name: str) -> list:
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", name)]


def guess_kind(name: str, samples: list[dict]) -> str | None:
    """indoor/outdoor from the court name or any slot field that mentions it."""
    text = (name + " " + json.dumps([s for s in samples if s.get("courtName") == name][:1])).lower()
    if "outdoor" in text or "outside" in text:
        return "outdoor"
    if "indoor" in text or "inside" in text:
        return "indoor"
    return None


def known_club_id() -> str | None:
    """--club-id's fallback: PADIUM_CLUB_ID (env or .env), then the committed club.json."""
    load_dotenv(HERE / ".env")
    if os.environ.get("PADIUM_CLUB_ID"):
        return os.environ["PADIUM_CLUB_ID"]
    if CLUB_FILE.exists():
        return json.loads(CLUB_FILE.read_text()).get("club_id")
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--club-id", help="32-hex id from padelmates.se/club/<id>")
    ap.add_argument("--name", default=NAME)
    ap.add_argument("--write", action="store_true", help="write club.json and tests/fixtures/live_*.json")
    ap.add_argument("--fixtures", action="store_true", help="write only tests/fixtures/live_*.json")
    args = ap.parse_args()

    club_id = args.club_id or known_club_id()
    candidates = [club_id] if club_id else [*discover_ids(), "padium"]
    today = datetime.now(LONDON).date()
    saturday = today + timedelta(days=(5 - today.weekday()) % 7 or 7)
    for club_id in candidates:
        club = Club(club_id, args.name)
        samples = {d.isoformat(): data for d in (today, saturday) if (data := fetch(club_id, d)) is not None}
        if not samples:
            continue
        for data in samples.values():
            summarise(data, club)
        print(f"\n=== club_id={club_id} answers")
        if args.write or args.fixtures:
            FIXTURES.mkdir(parents=True, exist_ok=True)
            for old in FIXTURES.glob("live_*.json"):
                old.unlink()
            for day, data in samples.items():
                (FIXTURES / f"live_slots_{day}.json").write_text(json.dumps(scrub(data), indent=1, ensure_ascii=False) + "\n")
            print(f"wrote {len(samples)} fixtures in {FIXTURES}")
        if args.write:
            items = [s for data in samples.values() for s in data.get("allSlots", [])]
            listed = {c["name"]: c.get("indoor") for data in samples.values() for c in data.get("originalCourts", [])}
            names = sorted(set(listed) | {s["courtName"] for s in items}, key=natural)
            courts = {
                n: Court(n, n, ("indoor" if listed[n] else "outdoor") if isinstance(listed.get(n), bool) else guess_kind(n, items))
                for n in names
            }
            CLUB_FILE.write_text(json.dumps(club_to_json(Club(club_id, args.name, courts)), indent=2) + "\n")
            print(f"wrote {CLUB_FILE.name}: {[(c.name, c.kind) for c in courts.values()]}")
            if any(c.kind is None for c in courts.values()):
                print('   set each court\'s "kind" (indoor/outdoor) in club.json by hand where it is null')
            print(f"PADIUM_CLUB_ID={club_id}")
        return 0
    print("\n!! no working club id. Open Padium Canary Wharf on padelmates.se (or the app's share link) "
          "and pass the 32-hex id from the URL with --club-id.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
