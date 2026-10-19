from datetime import datetime

import httpx
from conftest import FIXTURES, load_fixture

import bot
import finder
from padelmates import PadelMatesClient
from parser import LONDON

MON_NOON = datetime(2026, 10, 19, 12, 0, tzinfo=LONDON)


# Mon 26 Oct 13:00 GMT: the booking window as seen at MON_NOON (7 days ahead).
HORIZON_MS = int(datetime(2026, 10, 26, 12, 0, tzinfo=LONDON).timestamp() * 1000)


def fixture_client(club, calls=None, horizon_ms=None):
    def handler(request):
        start = int(request.url.params["start_datetime"])
        day = datetime.fromtimestamp(start / 1000, LONDON).date().isoformat()
        if calls is not None:
            calls.append(day)
        f = FIXTURES / f"sample_slots_{day}.json"
        body = load_fixture(f.name) if f.exists() else {"allSlots": []}
        if horizon_ms:
            body["advance_booking_horizon_ends_at"] = horizon_ms
        return httpx.Response(200, json=body)

    return PadelMatesClient(club, httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_answer_slot_exact(club):
    text = await bot.answer_slot(fixture_client(club), datetime(2026, 10, 24, 18, tzinfo=LONDON), MON_NOON)
    assert "✅ <b>18:00 free:</b>\n• Court 3 (indoor)" in text


async def test_answer_slot_fully_booked_fetches_next_day(club):
    calls = []
    text = await bot.answer_slot(fixture_client(club, calls), datetime(2026, 10, 25, 18, tzinfo=LONDON), MON_NOON)
    assert calls == ["2026-10-25", "2026-10-26"]
    assert "First free on Mon 26 Oct:\n• 07:00 (2 courts)" in text


async def test_answer_next_walks_days_until_free(club):
    calls = []
    text = await bot.answer_next(fixture_client(club, calls), MON_NOON)
    assert calls == ["2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23", "2026-10-24"]
    assert "Sat 24 Oct" in text and "✅ <b>07:00 free:</b>" in text


async def test_answer_next_nothing(club):
    calls = []
    text = await bot.answer_next(fixture_client(club, calls), datetime(2026, 10, 27, 9, tzinfo=LONDON))
    assert len(calls) == bot.NEXT_DAYS + 1
    assert "Nothing free in the next 7 days" in text


async def test_answer_slot_past_booking_window(club):
    client = fixture_client(club, horizon_ms=HORIZON_MS)
    text = await bot.answer_slot(client, datetime(2026, 10, 30, 18, tzinfo=LONDON), MON_NOON)
    assert "🕒 <b>18:00 isn't bookable yet.</b> Courts open 7 days ahead, so try from around Fri 23 Oct 18:00." in text


async def test_answer_slot_on_horizon_day_before_cutoff_is_normal(club):
    client = fixture_client(club, horizon_ms=HORIZON_MS)
    text = await bot.answer_slot(client, datetime(2026, 10, 26, 7, tzinfo=LONDON), MON_NOON)
    assert "✅ <b>07:00 free:</b>" in text


async def test_answer_next_stops_at_booking_window(club):
    calls = []
    client = fixture_client(club, calls, horizon_ms=HORIZON_MS)
    await bot.answer_next(client, datetime(2026, 10, 27, 9, tzinfo=LONDON))
    assert calls == ["2026-10-27"]  # day 2 is already past the window


def test_not_open_rounds_to_half_hour(club):
    now = datetime(2026, 10, 19, 14, 34, tzinfo=LONDON)
    horizon = datetime(2026, 10, 26, 13, 33, tzinfo=LONDON)  # GMT by then: 7 days in absolute time
    text = finder.render_not_open(club, datetime(2026, 10, 30, 19, 30, tzinfo=LONDON), horizon, now)
    assert "so try from around Fri 23 Oct 20:30." in text
