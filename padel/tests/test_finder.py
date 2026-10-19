import json
from datetime import date, datetime, timedelta

import httpx
import pytest
from conftest import FIXTURES, ROOT, load_fixture

import finder
from padelmates import (
    BookingSiteError,
    PadelMatesClient,
    ShapeError,
    day_window,
    load_club,
    parse_horizon,
    parse_slots,
)
from parser import LONDON

MON_NOON = datetime(2026, 10, 19, 12, 0, tzinfo=LONDON)


def london(d, h, m=0):
    return datetime(2026, 10, d, h, m, tzinfo=LONDON)


def day_slots(club, day: str):
    d = date.fromisoformat(day)
    return [s for s in parse_slots(load_fixture(f"sample_slots_{day}.json"), club) if s.start.date() == d]


def test_club_file(club):
    assert club.name == "Padium Canary Wharf"
    assert club.url == "https://padelmates.se/club/0f1e2d3c4b5a69788796a5b4c3d2e1f0"
    assert club.courts["Court 3"].kind == "indoor" and club.courts["Court 8"].kind == "outdoor"


def test_load_club_checks_env_id(monkeypatch):
    monkeypatch.setenv("PADIUM_CLUB_ID", "someone-else")
    with pytest.raises(SystemExit):
        load_club(FIXTURES / "sample_club.json")


def test_timestamps_become_london_time(club):
    slots = day_slots(club, "2026-10-24")
    assert min(s.start for s in slots) == london(24, 7)
    assert all(s.start.date() == date(2026, 10, 24) for s in slots)  # 00:00 Sunday dropped
    mon = day_slots(club, "2026-10-26")
    assert min(s.start for s in mon) == datetime(2026, 10, 26, 7, tzinfo=LONDON)


def test_reserved_slots_are_not_free(club):
    assert not [s for s in day_slots(club, "2026-10-24") if s.court.name == "Court 5"]
    assert day_slots(club, "2026-10-25") == []


def test_day_window_spans_local_midnights():
    start, end = day_window(date(2026, 10, 24))
    assert (end - start) / 3_600_000 == 24
    assert datetime.fromtimestamp(start / 1000, LONDON) == london(24, 0)
    start, end = day_window(date(2026, 10, 25))  # clocks go back: 25 hour day
    assert (end - start) / 3_600_000 == 25


def test_exact_match(club):
    m = finder.match(day_slots(club, "2026-10-24"), london(24, 18), MON_NOON)
    assert [o.court.name for o in m.exact] == ["Court 3", "Court 8"]
    assert m.exact[0].prices == {60: "80 GBP", 90: "120 GBP"}  # 120 min dropped
    text = finder.render_match(club, m)
    assert text.splitlines() == [
        "🎾 <b>Padium Canary Wharf</b>, Sat 24 Oct",
        "✅ <b>18:00 free:</b>",
        "• Court 3 (indoor) 60/90 min £80/£120",
        "• Court 8 (outdoor) 60 min £60",
    ]


def test_nearest_sorted_by_distance(club):
    m = finder.match(day_slots(club, "2026-10-24"), london(24, 18, 30), MON_NOON)
    assert not m.exact and not m.fully_booked
    assert [(t.strftime("%H:%M"), n) for t, n in m.nearest] == [
        ("18:00", 2), ("19:30", 1), ("17:00", 2), ("20:00", 3), ("21:30", 1), ("07:00", 1),
    ]
    assert "❌ <b>18:30 taken.</b> Nearest:\n• 18:00 (2 courts)\n• 19:30 (1 court)\n• 17:00 (2 courts)\n" in finder.render_match(club, m)


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
    assert "First free on Mon 26 Oct:\n• 07:00 (2 courts)\n• 08:00 (2 courts)\n• 09:30 (1 court)" in text


def test_fully_booked_next_day_too(club):
    m = finder.match([], london(25, 18), MON_NOON)
    text = finder.render_match(club, m, (date(2026, 10, 26), []), MON_NOON)
    assert "Mon 26 Oct is fully booked too." in text


def test_unknown_court_still_counts(club):
    groups = finder.group(day_slots(club, "2026-10-26"), MON_NOON)
    offers = groups[datetime(2026, 10, 26, 8, tzinfo=LONDON)]
    assert [(o.court.name, o.court.kind) for o in offers] == [("Centre Court", None), ("Court 7", "indoor")]
    assert finder.fmt_offer(offers[0]) == "Centre Court 60 min £40"


def test_earliest_and_render_next(club):
    found = finder.earliest(day_slots(club, "2026-10-24"), MON_NOON)
    text = finder.render_next(club, found, 7)
    assert "🎾 <b>Padium Canary Wharf</b>, Sat 24 Oct" in text
    assert "✅ <b>07:00 free:</b>\n• Court 3 (indoor) 60 min £60\n" in text
    assert "Then:\n• 17:00 (2 courts)\n• 18:00 (2 courts)\n• 19:30 (1 court)" in text
    assert "Nothing free in the next 7 days" in finder.render_next(club, None, 7)


def test_court_names_sorted_naturally_and_escaped(club):
    from padelmates import Court, Slot
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
    return PadelMatesClient(club, httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_client_fetches_once_per_day_and_caches(club):
    calls = []

    def handler(request):
        calls.append(dict(request.url.params))
        return httpx.Response(200, json=load_fixture("sample_slots_2026-10-24.json"))

    client = make_client(club, handler)
    a = await client.day(date(2026, 10, 24))
    b = await client.day(date(2026, 10, 24))
    assert a is b and len(calls) == 1
    start, end = day_window(date(2026, 10, 24))
    assert calls[0] == {"club_id": club.club_id, "start_datetime": str(start), "end_datetime": str(end), "lang": "en"}


async def test_client_http_error(club):
    client = make_client(club, lambda r: httpx.Response(503, text="down"))
    with pytest.raises(BookingSiteError):
        await client.day(date(2026, 10, 24))


async def test_client_network_error(club):
    def boom(request):
        raise httpx.ConnectTimeout("timeout", request=request)

    with pytest.raises(BookingSiteError):
        await make_client(club, boom).day(date(2026, 10, 24))


@pytest.mark.parametrize("payload", [[], {"error": "x"}, {"allSlots": [{"duration": 60}]},
                                     {"allSlots": [{"courtName": "C", "duration": 60, "startTimestamp": "soon"}]}])
async def test_client_shape_change(club, payload):
    with pytest.raises(ShapeError):
        await make_client(club, lambda r: httpx.Response(200, json=payload)).day(date(2026, 10, 24))


def test_live_fixtures_parse():
    """Real Padium responses saved by `probe.py --write` must still parse."""
    live = sorted(FIXTURES.glob("live_slots_*.json"))
    if not live:
        pytest.skip("no live fixtures yet: run probe.py --write")
    club = load_club(ROOT / "club.json", club_id="")
    for f in live:
        raw = json.loads(f.read_text())
        slots = parse_slots(raw, club)
        assert slots, f.name
        kinds = {s.court.name: s.court.kind for s in slots}
        assert kinds.get("Outdoor Court 7", "outdoor") == "outdoor" and kinds.get("Court 1", "indoor") == "indoor"
        assert {s.duration for s in slots} <= {60, 90, 120}
        assert parse_horizon(raw) is not None
        assert raw.keys() <= {"allSlots", "originalCourts", "too_early_for_advance_booking",
                              "advance_booking_horizon_ends_at"}, "probe.scrub must drop other players' bookings"


def test_live_kinds_win_over_club_file(club):
    raw = {"allSlots": [{"courtName": "Court 3", "courtId": "c3", "duration": 60, "price": 80.0,
                         "startTimestamp": 1792861200000, "reservedIntersection": False}],
           "originalCourts": [{"_id": "c3", "name": "Court 3", "indoor": False}]}
    assert parse_slots(raw, club)[0].court.kind == "outdoor"


def test_horizon():
    assert parse_horizon({"advance_booking_horizon_ends_at": 1793021536240}) == datetime(
        2026, 10, 26, 13, 32, 16, 240000, tzinfo=LONDON)
    assert parse_horizon({"allSlots": []}) is None


# --- indoor/outdoor and ranges ----------------------------------------------

def test_only_kind(club):
    slots = day_slots(club, "2026-10-24")
    assert {s.court.name for s in finder.only(slots, "outdoor")} == {"Court 8"}
    assert "Court 8" not in {s.court.name for s in finder.only(slots, "indoor")}
    assert finder.only(slots, None) == slots


def test_range_lists_every_start_inclusive(club):
    rm = finder.match_range(day_slots(club, "2026-10-24"), london(24, 17), london(24, 19, 30), MON_NOON)
    assert [(t.strftime("%H:%M"), [o.court.name for o in offers]) for t, offers in rm.found] == [
        ("17:00", ["Court 1", "Court 8"]), ("18:00", ["Court 3", "Court 8"]), ("19:30", ["Court 1"]),
    ]
    text = finder.render_range(club, rm, kind=None)
    assert text.splitlines()[:4] == [
        "🎾 <b>Padium Canary Wharf</b>, Sat 24 Oct", "✅ <b>Free 17:00–19:30:</b>", "<b>17:00</b>",
        "• Court 1 (indoor) 90 min £120",
    ]


def test_range_with_nothing_shows_nearest_outside(club):
    rm = finder.match_range(day_slots(club, "2026-10-24"), london(24, 8), london(24, 16), MON_NOON)
    assert not rm.found
    assert [t.strftime("%H:%M") for t, _ in rm.nearest] == ["07:00", "17:00", "18:00", "19:30"]
    assert "❌ <b>Nothing free 08:00–16:00.</b> Nearest:\n• 07:00 (1 court)\n• 17:00 (2 courts)" in finder.render_range(club, rm)


def test_range_caps_long_lists(club):
    from padelmates import Court, Slot
    slots = [Slot(london(24, 8) + timedelta(minutes=30 * i), 60, "80 GBP", Court("c", "Court 1", "indoor")) for i in range(12)]
    rm = finder.match_range(slots, london(24, 8), london(24, 14), MON_NOON)
    text = finder.render_range(club, rm)
    assert text.count("<b>1") + text.count("<b>0") == finder.RANGE_MAX_STARTS
    assert text.endswith("…and 4 more start times, up to 13:30")


def test_kind_wording(club):
    m = finder.match([], london(25, 18), MON_NOON)
    text = finder.render_match(club, m, (date(2026, 10, 26), []), MON_NOON, kind="indoor")
    assert text.splitlines() == [
        "🎾 <b>Padium Canary Wharf</b>, Sun 25 Oct · indoor",
        "❌ <b>No indoor courts free on Sun 25 Oct.</b>",
        "No indoor courts free on Mon 26 Oct either.",
    ]
    assert "😕 No outdoor courts free in the next 7 days." in finder.render_next(club, None, 7, kind="outdoor")
