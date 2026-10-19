from datetime import datetime

import httpx
from conftest import FIXTURES, load_fixture

import bot
from parser import LONDON
from playtomic import PlaytomicClient

MON_NOON = datetime(2026, 10, 19, 12, 0, tzinfo=LONDON)


def fixture_client(club, calls=None):
    def handler(request):
        day = request.url.params["date"]
        if calls is not None:
            calls.append(day)
        f = FIXTURES / f"sample_availability_{day}.json"
        return httpx.Response(200, json=load_fixture(f.name) if f.exists() else [])

    return PlaytomicClient(club, httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_answer_slot_exact(club):
    text = await bot.answer_slot(fixture_client(club), datetime(2026, 10, 24, 18, tzinfo=LONDON), MON_NOON)
    assert "✅ <b>18:00 free:</b> Court 3 (indoor)" in text


async def test_answer_slot_fully_booked_fetches_next_day(club):
    calls = []
    text = await bot.answer_slot(fixture_client(club, calls), datetime(2026, 10, 25, 18, tzinfo=LONDON), MON_NOON)
    assert calls == ["2026-10-25", "2026-10-26"]
    assert "First free on Mon 26 Oct: 07:00 (2 courts)" in text


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
