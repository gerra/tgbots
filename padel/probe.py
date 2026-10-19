"""Hit Playtomic live, print the real response shapes, find Padium Canary Wharf.

    python probe.py                      # look around, print what came back
    python probe.py --write              # ...and write club.json + tests/fixtures/live_*.json
    python probe.py --tenant-id <uuid>   # skip the search

Playtomic's API is unofficial and moves around, so this tries every host we
know of and reports which ones answer without a login.
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
NAME = "Padium"
COORD = "51.5045,-0.0195"  # 10 Bank Street, E14 4DE
GUESSED_SLUGS = ["padium-canary-wharf", "padium", "padium-london"]

TENANT_HOSTS = ["https://api.playtomic.io", "https://api.app.playtomic.io"]

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


def find_tenants(obj: Any) -> list[dict]:
    """Every dict carrying tenant_id + tenant_name anywhere in a JSON tree."""
    out = []
    if isinstance(obj, dict):
        if "tenant_id" in obj and "tenant_name" in obj:
            out.append(obj)
        for v in obj.values():
            out += find_tenants(v)
    elif isinstance(obj, list):
        for v in obj:
            out += find_tenants(v)
    return out


def is_padium_cw(t: dict) -> bool:
    addr = json.dumps(t.get("address") or {}).lower()
    return "padium" in t.get("tenant_name", "").lower() and (
        "canary" in t["tenant_name"].lower() or "bank st" in addr or "e14" in addr
    )


def search_tenant(tenant_id: str | None) -> tuple[dict | None, Any]:
    for host in TENANT_HOSTS:
        queries = (
            [{"tenant_id": tenant_id}] if tenant_id else
            [{"tenant_name": NAME, "sport_id": "PADEL", "size": 50},
             {"coordinate": COORD, "radius": 3000, "sport_id": "PADEL", "playtomic_status": "ACTIVE", "size": 100}]
        )
        for q in queries:
            data = show(f"tenants via {host}", get(f"{host}/v1/tenants", params=q))
            if not data:
                continue
            tenants = find_tenants(data)
            for t in tenants:
                print(f"    - {t['tenant_id']}  {t['tenant_name']}  {(t.get('address') or {}).get('street')}")
            hit = next((t for t in tenants if (t["tenant_id"] == tenant_id if tenant_id else is_padium_cw(t))), None)
            if hit:
                return hit, data
    # Fallback: the tenant object the playtomic.com club page embeds in __NEXT_DATA__.
    for slug in GUESSED_SLUGS:
        r = get(f"https://playtomic.com/clubs/{slug}", headers={"Accept": "text/html"})
        if isinstance(r, str):
            print(f"\n### club page {r}")
            continue
        print(f"\n### club page {r.url} -> HTTP {r.status_code}")
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.DOTALL)
        if r.status_code != 200 or not m:
            continue
        data = json.loads(m.group(1))
        tenants = [t for t in find_tenants(data) if not tenant_id or t["tenant_id"] == tenant_id]
        for t in tenants:
            print(f"    - {t['tenant_id']}  {t['tenant_name']}  resources: {len(t.get('resources') or [])}")
        if tenants:
            t = dict(tenants[0])
            t.setdefault("slug", slug)
            return t, data
    return None, None


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
    print(f"    raw start_time range: {raw_times[0]} .. {raw_times[-1]}  (compare with the first bookable time in the app: "
          "an hour earlier during BST means the times are UTC)")
    print(f"    as London: {min(s.start for s in slots):%a %d %H:%M} .. {max(s.start for s in slots):%a %d %H:%M}")
    print(f"    durations: {dict(Counter(s.duration for s in slots))}; prices: {dict(Counter(s.price for s in slots).most_common(4))}")
    unknown = {s.court.id for s in slots if s.court.name == 'Court ?'}
    if unknown:
        print(f"    !! resource ids not in tenant resources: {sorted(unknown)}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant-id")
    ap.add_argument("--write", action="store_true", help="write club.json and tests/fixtures/live_*.json")
    args = ap.parse_args()

    tenant, _ = search_tenant(args.tenant_id)
    if not tenant:
        print("\n!! Padium Canary Wharf not found. Find the id in the Playtomic web app URL and pass --tenant-id.")
        return 1
    club = club_from_tenant(tenant)
    print(f"\n=== FOUND: {club.name}  tenant_id={club.tenant_id}  url={club.url}")
    for c in club.courts.values():
        print(f"    court {c.id}  {c.name}  {c.kind}")
    print(f"    raw resource example: {json.dumps((tenant.get('resources') or [{}])[0])[:400]}")

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

    if args.write:
        CLUB_FILE.write_text(json.dumps(club_to_json(club), indent=2) + "\n")
        FIXTURES.mkdir(parents=True, exist_ok=True)
        (FIXTURES / "live_tenant.json").write_text(json.dumps(tenant, indent=2) + "\n")
        for day, data in samples.items():
            (FIXTURES / f"live_availability_{day}.json").write_text(json.dumps(data, indent=2) + "\n")
        print(f"wrote {CLUB_FILE.name} and {len(samples) + 1} fixtures in {FIXTURES}")
        print(f"PADIUM_TENANT_ID={club.tenant_id}")
    return 0 if working else 2


if __name__ == "__main__":
    sys.exit(main())
