import json
from datetime import date, datetime

import httpx
import pytest
from conftest import FIXTURES, load_fixture

import finder
from parser import LONDON
from playtomic import PlaytomicClient, PlaytomicError, ShapeError, parse_availability

MON_NOON = datetime(2026, 10, 19, 12, 0, tzinfo=LONDON)


def london(d, h, m=0):
    return datetime(2026, 10, d, h, m, tzinfo=LONDON)


def day_slots(club, day: str):
    d = date.fromisoformat(day)
    return [s for s in parse_availability(load_fixture(f"sample_availability_{day}.json"), club) if s.start.date() == d]


def test_tenant_parsing(club):
    assert club.name == "Padium Canary Wharf"
    assert club.url == "https://playtomic.com/clubs/padium-canary-wharf"
    assert len(club.courts) == 8  # inactive court dropped
    kinds = {c.name: c.kind for c in club.courts.values()}
    assert kinds["Court 3"] == "indoor" and kinds["Court 8"] == "outdoor"


def test_utc_slots_become_london_time(club):
    slots = day_slots(club, "2026-10-24")
    assert min(s.start for s in slots) == london(24, 7)  # 06:00Z in BST
    assert all(s.start.date() == date(2026, 10, 24) for s in slots)  # 23:00Z is Sunday in London
    mon = day_slots(club, "2026-10-26")
    assert min(s.start for s in mon) == datetime(2026, 10, 26, 7, tzinfo=LONDON)  # GMT: no shift


def test_exact_match(club):
    m = finder.match(day_slots(club, "2026-10-24"), london(24, 18), MON_NOON)
    assert [o.court.name for o in m.exact] == ["Court 3", "Court 8"]
    assert m.exact[0].prices == {60: "80 GBP", 90: "120 GBP"}  # 120 min dropped
    text = finder.render_match(club, m)
    assert text.splitlines() == [
        "🎾 <b>Padium Canary Wharf</b>, Sat 24 Oct",
        "✅ <b>18:00 free:</b> Court 3 (indoor) 60/90 min £80/£120 · Court 8 (outdoor) 60 min £60",
        '<a href="https://playtomic.com/clubs/padium-canary-wharf">Book in Playtomic</a>',
    ]


def test_nearest_sorted_by_distance(club):
    m = finder.match(day_slots(club, "2026-10-24"), london(24, 18, 30), MON_NOON)
    assert not m.exact and not m.fully_booked
    assert [(t.strftime("%H:%M"), n) for t, n in m.nearest] == [
        ("18:00", 2), ("19:30", 1), ("17:00", 2), ("20:00", 3), ("21:30", 1), ("07:00", 1),
    ]
    assert "❌ <b>18:30 taken.</b> Nearest: 18:00 (2 courts) · 19:30 (1 court) · 17:00 (2 courts)" in finder.render_match(club, m)


def test_nearest_caps_three_each_side(club):
    m = finder.match(day_slots(club, "2026-10-24"), london(24, 6), MON_NOON)
    assert [t.strftime("%H:%M") for t, _ in m.nearest] == ["07:00", "17:00", "18:00"]


def test_past_slots_ignored(club):
    sat_evening = london(24, 19)
    m = finder.match(day_slots(club, "2026-10-24"), london(24, 18), sat_evening)
    assert not m.exact
    assert [t.strftime("%H:%M") for t, _ in m.nearest] == ["19:30", "20:00", "21:30"]


def test_fully_booked_shows_next_day(club):
    m = finder.match(day_slots(club, "2026-10-25"), london(25, 18), MON_NOON)
    assert m.fully_booked
    text = finder.render_match(club, m, (date(2026, 10, 26), day_slots(club, "2026-10-26")), MON_NOON)
    assert "❌ <b>Sun 25 Oct is fully booked.</b>" in text
    assert "First free on Mon 26 Oct: 07:00 (2 courts) · 08:00 (2 courts) · 09:30 (1 court)" in text


def test_fully_booked_next_day_too(club):
    m = finder.match([], london(25, 18), MON_NOON)
    text = finder.render_match(club, m, (date(2026, 10, 26), []), MON_NOON)
    assert "Mon 26 Oct is fully booked too." in text


def test_unknown_court_still_counts(club):
    groups = finder.group(day_slots(club, "2026-10-26"), MON_NOON)
    names = [o.court.name for o in groups[datetime(2026, 10, 26, 8, tzinfo=LONDON)]]
    assert names == ["Court 7", "Court ?"]


def test_earliest_and_render_next(club):
    found = finder.earliest(day_slots(club, "2026-10-24"), MON_NOON)
    text = finder.render_next(club, found, 7)
    assert "🎾 <b>Padium Canary Wharf</b>, Sat 24 Oct" in text
    assert "✅ <b>07:00 free:</b> Court 3 (indoor) 60 min £60" in text
    assert "Then: 17:00 (2 courts) · 18:00 (2 courts) · 19:30 (1 court)" in text
    assert "Nothing free in the next 7 days" in finder.render_next(club, None, 7)


def test_court_names_sorted_naturally_and_escaped(club):
    from playtomic import Court, Slot
    t = london(24, 18)
    slots = [Slot(t, 60, "80 GBP", Court("a", "Court 10")), Slot(t, 60, "80 GBP", Court("b", "Court 2 <VIP>"))]
    m = finder.match(slots, t, MON_NOON)
    assert finder.fmt_offer(m.exact[0]) == "Court 2 &lt;VIP&gt; 60 min £80"
    assert m.exact[1].court.name == "Court 10"


@pytest.mark.parametrize("raw,out", [("80 GBP", "£80"), ("42.5 GBP", "£42.50"), ("36 EUR", "€36"),
                                     ("10 CHF", "10 CHF"), (None, None), ("80.00 GBP", "£80")])
def test_price(raw, out):
    assert finder.fmt_price(raw) == out


# --- client: caching, errors, shape changes ---------------------------------

def make_client(club, handler):
    return PlaytomicClient(club, httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_client_fetches_once_per_day_and_caches(club):
    calls = []

    def handler(request):
        calls.append(dict(request.url.params))
        day = request.url.params["date"]
        return httpx.Response(200, json=load_fixture(f"sample_availability_{day}.json"))

    client = make_client(club, handler)
    a = await client.day(date(2026, 10, 24))
    b = await client.day(date(2026, 10, 24))
    assert a is b and len(calls) == 1
    assert calls[0] == {"tenant_id": club.tenant_id, "sport_id": "PADEL", "date": "2026-10-24"}
    assert all(s.start.date() == date(2026, 10, 24) for s in a)


async def test_client_http_error(club):
    client = make_client(club, lambda r: httpx.Response(503, text="down"))
    with pytest.raises(PlaytomicError):
        await client.day(date(2026, 10, 24))


async def test_client_network_error(club):
    def boom(request):
        raise httpx.ConnectTimeout("timeout", request=request)

    with pytest.raises(PlaytomicError):
        await make_client(club, boom).day(date(2026, 10, 24))


@pytest.mark.parametrize("payload", [{"error": "x"}, [{"slots": []}], [{"resource_id": "a", "start_date": "2026-10-24", "slots": [{"duration": 60}]}]])
async def test_client_shape_change(club, payload):
    with pytest.raises(ShapeError):
        await make_client(club, lambda r: httpx.Response(200, json=payload)).day(date(2026, 10, 24))


def test_live_fixtures_parse(club):
    """Real responses saved by `probe.py --write` must still parse."""
    live = sorted(FIXTURES.glob("live_availability_*.json"))
    if not live:
        pytest.skip("no live fixtures yet: run probe.py --write")
    from playtomic import club_from_tenant
    real_club = club_from_tenant(json.loads((FIXTURES / "live_tenant.json").read_text()))
    for f in live:
        slots = parse_availability(json.loads(f.read_text()), real_club)
        assert all(s.court.name != "Court ?" for s in slots), f"{f.name}: unknown resource ids"
