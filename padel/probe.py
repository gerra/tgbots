"""Hit Playtomic live, print the real response shapes, find Padium Canary Wharf.

    python probe.py                       # search, print what came back
    python probe.py --write               # ...and write club.json + tests/fixtures/live_*.json
    python probe.py --slug <club-slug>    # skip the search (playtomic.com/clubs/<slug>)
    python probe.py --slug x --fixtures   # save live fixtures from any club, no club.json

Playtomic's API is unofficial and moves around. As of Oct 2026:
- api.playtomic.io is gone (502), api.app.playtomic.io wants a login (401);
- playtomic.com/search?q= lists clubs, and each playtomic.com/clubs/<slug>
  page embeds its tenant (id, courts, opening hours) in the RSC payload;
- playtomic.com/api/clubs/availability answers without a login.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from playtomic import (
    CLUB_FILE,
    LONDON,
    USER_AGENT,
    club_from_tenant,
    club_to_json,
    parse_availability,
)

HERE = Path(__file__).parent
FIXTURES = HERE / "tests" / "fixtures"
SEARCHES = ["padium", "padium london", "padium canary wharf", "canary wharf"]
GUESSED_SLUGS = ["padium-canary-wharf", "padium-london", "padium"]
WEB = "https://playtomic.com"

http = httpx.Client(timeout=15, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, follow_redirects=True)


def get(url: str, **kw: Any) -> httpx.Response | str:
    try:
        return http.get(url, **kw)
    except httpx.HTTPError as e:
        return f"{url} -> {e!r}"


def show(label: str, r: httpx.Response | str) -> Any | None:
    if isinstance(r, str):
        print(f"\n### {label}\n    {r}")
        return None
    print(f"\n### {label}\n    {r.request.method} {r.url}\n    -> HTTP {r.status_code} {r.headers.get('content-type', '')}")
    if r.status_code != 200:
        print("    " + r.text[:300].replace("\n", " "))
        return None
    try:
        data = r.json()
    except ValueError:
        print("    (not JSON) " + r.text[:200].replace("\n", " "))
        return None
    print("    shape: " + shape(data))
    return data


def shape(v: Any, depth: int = 0) -> str:
    """Compact type sketch of a JSON value, e.g. [{resource_id: str, slots: [{...}]}]."""
    if depth > 3:
        return "…"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{k}: {shape(x, depth + 1)}" for k, x in list(v.items())[:25]) + "}"
    if isinstance(v, list):
        return f"[{len(v)} × {shape(v[0], depth + 1)}]" if v else "[]"
    return type(v).__name__


def html(url: str, **params: str) -> str | None:
    r = get(url, params=params, headers={"Accept": "text/html"})
    if isinstance(r, str) or r.status_code != 200:
        print(f"    {url} {params or ''} -> {r if isinstance(r, str) else r.status_code}")
        return None
    return r.text


def club_page_tenant(slug: str) -> dict | None:
    """The tenant object playtomic.com/clubs/<slug> embeds in its RSC payload."""
    page = html(f"{WEB}/clubs/{slug}")
    if page is None:
        return None
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', page, re.DOTALL)
    payload = "".join(json.loads(f'"{c}"') for c in chunks)
    i = payload.find('{"tenant":{')
    if i < 0:
        print(f"    {slug}: no tenant in page payload")
        return None
    tenant, _ = json.JSONDecoder().raw_decode(payload, i)
    return tenant["tenant"]


def describe(t: dict) -> str:
    a = t.get("address") or {}
    return f"{t['tenant_id']}  {t['tenant_name']}  ({a.get('street')}, {a.get('postal_code')} {a.get('city')}, {a.get('country_code')})"


def is_padium_cw(t: dict) -> bool:
    addr = json.dumps(t.get("address") or {}).lower()
    return "padium" in t.get("tenant_name", "").lower() and (
        "canary" in t["tenant_name"].lower() or "bank st" in addr or "e14" in addr
    )


def search_tenant() -> dict | None:
    slugs: list[str] = []
    for q in SEARCHES:
        page = html(f"{WEB}/search", q=q) or ""
        found = sorted(set(re.findall(r'href="/clubs/([a-z0-9-]+)"', page)))
        print(f"\n### search {q!r}: {len(found)} clubs")
        slugs += [s for s in found if "padium" in s or "canary" in s or "wharf" in s]
    for slug in dict.fromkeys(slugs + GUESSED_SLUGS):
        t = club_page_tenant(slug)
        if t:
            print(f"    {slug}: {describe(t)}")
            if is_padium_cw(t):
                return t
    return None


def availability_candidates(tenant_id: str, d: date) -> list[tuple[str, str, dict]]:
    day = d.isoformat()
    v1 = {"sport_id": "PADEL", "tenant_id": tenant_id,
          "local_start_min": f"{day}T00:00:00", "local_start_max": f"{day}T23:59:59"}
    return [
        ("playtomic.com web", "https://playtomic.com/api/clubs/availability",
         {"tenant_id": tenant_id, "date": day, "sport_id": "PADEL"}),
        ("api.playtomic.io v1", "https://api.playtomic.io/v1/availability", v1),
        ("api.app.playtomic.io v1", "https://api.app.playtomic.io/v1/availability", v1),
    ]


def summarise(raw: list, club) -> None:
    slots = parse_availability(raw, club)
    first = raw[0] if raw else {}
    print(f"    resources: {len(raw)}; first: resource_id={first.get('resource_id')} start_date={first.get('start_date')}")
    print(f"    first slots raw: {json.dumps((first.get('slots') or [])[:3])}")
    if not slots:
        print("    no free slots")
        return
    raw_times = sorted(s["start_time"] for item in raw for s in item.get("slots") or [])
    print(f"    raw start_time range: {raw_times[0]} .. {raw_times[-1]}  (UTC: an hour before opening time during BST)")
    print(f"    as London: {min(s.start for s in slots):%a %d %H:%M} .. {max(s.start for s in slots):%a %d %H:%M}")
    print(f"    durations: {dict(Counter(s.duration for s in slots))}; prices: {dict(Counter(s.price for s in slots).most_common(4))}")
    unknown = {s.court.id for s in slots if s.court.name == 'Court ?'}
    if unknown:
        print(f"    !! resource ids not in tenant resources: {sorted(unknown)}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", help="playtomic.com/clubs/<slug>; skips the search")
    ap.add_argument("--write", action="store_true", help="write club.json and tests/fixtures/live_*.json")
    ap.add_argument("--fixtures", action="store_true", help="write only tests/fixtures/live_*.json")
    args = ap.parse_args()

    tenant = club_page_tenant(args.slug) if args.slug else search_tenant()
    if not tenant:
        print("\n!! Padium Canary Wharf not found on Playtomic. If it's there under another name, "
              "pass --slug from its playtomic.com/clubs/<slug> URL.")
        return 1
    club = club_from_tenant(tenant)
    print(f"\n=== CLUB: {describe(tenant)}\n    url={club.url}  timezone={club.timezone}")
    print(f"    opening hours: {json.dumps(tenant.get('opening_hours'))}")
    for c in club.courts.values():
        print(f"    court {c.id}  {c.name}  {c.kind}")

    today = datetime.now(LONDON).date()
    saturday = today + timedelta(days=(5 - today.weekday()) % 7 or 7)
    working: list[str] = []
    samples: dict[str, Any] = {}
    for d in (today, saturday):
        for label, url, params in availability_candidates(club.tenant_id, d):
            data = show(f"availability {d} via {label}", get(url, params=params))
            if isinstance(data, list):
                summarise(data, club)
                working.append(label)
                samples.setdefault(d.isoformat(), data)
    print(f"\n=== availability works via: {sorted(set(working)) or 'NOTHING'}")

    if args.write or args.fixtures:
        FIXTURES.mkdir(parents=True, exist_ok=True)
        (FIXTURES / "live_tenant.json").write_text(json.dumps(tenant, indent=2, ensure_ascii=False) + "\n")
        for old in FIXTURES.glob("live_availability_*.json"):
            old.unlink()
        for day, data in samples.items():
            (FIXTURES / f"live_availability_{day}.json").write_text(json.dumps(data, indent=2) + "\n")
        print(f"wrote {len(samples) + 1} fixtures in {FIXTURES}")
    if args.write:
        CLUB_FILE.write_text(json.dumps(club_to_json(club), indent=2, ensure_ascii=False) + "\n")
        print(f"wrote {CLUB_FILE.name}\nPADIUM_TENANT_ID={club.tenant_id}")
    return 0 if working else 2


if __name__ == "__main__":
    sys.exit(main())
