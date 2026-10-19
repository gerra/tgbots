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
    text = (await bot.answer_slot(fixture_client(club), datetime(2026, 10, 24, 18, tzinfo=LONDON), MON_NOON)).text
    assert "✅ <b>18:00 free:</b>\n• Court 3 (indoor)" in text


async def test_answer_slot_fully_booked_fetches_next_day(club):
    calls = []
    text = (await bot.answer_slot(fixture_client(club, calls), datetime(2026, 10, 25, 18, tzinfo=LONDON), MON_NOON)).text
    assert calls == ["2026-10-25", "2026-10-26"]
    assert "First free on Mon 26 Oct:\n• 07:00 (2 courts)" in text


async def test_answer_next_walks_days_until_free(club):
    calls = []
    text = (await bot.answer_next(fixture_client(club, calls), MON_NOON)).text
    assert calls == ["2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23", "2026-10-24"]
    assert "Sat 24 Oct" in text and "✅ <b>07:00 free:</b>" in text


async def test_answer_next_nothing(club):
    calls = []
    text = (await bot.answer_next(fixture_client(club, calls), datetime(2026, 10, 27, 9, tzinfo=LONDON))).text
    assert len(calls) == bot.NEXT_DAYS + 1
    assert "Nothing free in the next 7 days" in text


async def test_answer_slot_past_booking_window(club):
    client = fixture_client(club, horizon_ms=HORIZON_MS)
    text = (await bot.answer_slot(client, datetime(2026, 10, 30, 18, tzinfo=LONDON), MON_NOON)).text
    assert "🕒 <b>18:00 isn't bookable yet.</b> Courts open 7 days ahead, so try from around Fri 23 Oct 18:00." in text


async def test_answer_slot_on_horizon_day_before_cutoff_is_normal(club):
    client = fixture_client(club, horizon_ms=HORIZON_MS)
    text = (await bot.answer_slot(client, datetime(2026, 10, 26, 7, tzinfo=LONDON), MON_NOON)).text
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


# --- buttons and game cards (Telegram objects faked) ------------------------

class FakeContext:
    def __init__(self, client, args=None):
        self.bot_data = {"client": client}
        self.chat_data = {}
        self.args = args or []


class FakeMessage:
    def __init__(self):
        self.sent = []
        self.chat = self
        self.message_id = 0

    async def send_message(self, text, **kw):
        self.sent.append((text, kw.get("reply_markup")))
        msg = FakeMessage()
        msg.message_id = len(self.sent)
        return msg

    async def reply_text(self, text, **kw):
        return await self.send_message(text, **kw)


class FakeQuery:
    def __init__(self, data, user_id, name, message):
        self.data, self.message = data, message
        self.from_user = type("U", (), {"id": user_id, "first_name": name})()
        self.answers, self.edits = [], []

    async def answer(self, text="", show_alert=False):
        self.answers.append(text)

    async def edit_message_text(self, text, **kw):
        self.edits.append(text)


def tap(data, user_id, name, message):
    q = FakeQuery(data, user_id, name, message)
    return type("Upd", (), {"callback_query": q})(), q


def buttons(markup):
    return [(b.text, b.callback_data or b.url) for row in markup.inline_keyboard for b in row]


async def test_exact_answer_has_booked_buttons_and_link(club):
    client = fixture_client(club)
    ctx = FakeContext(client)
    answer = await bot.answer_slot(client, datetime(2026, 10, 24, 18, tzinfo=LONDON), MON_NOON)
    labels = buttons(bot.offer_keyboard(ctx, client, answer))
    assert [t for t, _ in labels] == ["📲 Book in Padel Mates", "📌 Court 3 · 60m", "📌 Court 3 · 90m", "📌 Court 8 · 60m"]
    assert labels[0][1] == club.url


async def test_taken_answer_only_has_link(club):
    client = fixture_client(club)
    answer = await bot.answer_slot(client, datetime(2026, 10, 24, 18, 30, tzinfo=LONDON), MON_NOON)
    assert [t for t, _ in buttons(bot.offer_keyboard(FakeContext(client), client, answer))] == ["📲 Book in Padel Mates"]


async def test_booked_button_posts_card_then_players_join_and_pay(club):
    client = fixture_client(club)
    ctx = FakeContext(client)
    answer = await bot.answer_slot(client, datetime(2026, 10, 24, 18, tzinfo=LONDON), MON_NOON)
    data = dict(buttons(bot.offer_keyboard(ctx, client, answer)))["📌 Court 3 · 90m"]
    chat = FakeMessage()

    upd, q = tap(data, 1, "Anna", chat)
    await bot.on_booked_button(upd, ctx)
    (card, markup), = chat.sent
    assert "Sat 24 Oct 18:00 · Court 3 (indoor) · 90 min" in card and "£120 total · £30 each" in card
    (g,) = ctx.chat_data["games"].values()

    upd, q = tap(f"g:{g.id}:in", 2, "Bob", chat)
    await bot.on_card_button(upd, ctx)
    assert q.answers == ["You're in 🎾"] and "Bob</a> ⏳ owes £30" in q.edits[-1]

    upd, q = tap(f"g:{g.id}:remind", 2, "Bob", chat)
    await bot.on_card_button(upd, ctx)
    assert q.answers == ["Only Anna, who booked it, can do that."]

    upd, q = tap(f"g:{g.id}:remind", 1, "Anna", chat)
    await bot.on_card_button(upd, ctx)
    assert chat.sent[-1][0].startswith('🔔 <a href="tg://user?id=2">Bob</a>: please pay')

    upd, q = tap(f"g:{g.id}:paid", 2, "Bob", chat)
    await bot.on_card_button(upd, ctx)
    assert "Bob</a> ✅ paid" in q.edits[-1]

    upd, q = tap(f"g:{g.id}:cancel", 1, "Anna", chat)
    await bot.on_card_button(upd, ctx)
    assert q.edits[-1].startswith("❌ <s>")


async def test_expired_offer_button(club):
    upd, q = tap("bk:deadbeef:0", 1, "Anna", FakeMessage())
    await bot.on_booked_button(upd, FakeContext(fixture_client(club)))
    assert "expired" in q.answers[0]


async def test_booked_command(club):
    client = fixture_client(club)
    ctx = FakeContext(client, ["sat", "18:00", "outdoor", "8", "60", "£80"])
    chat = FakeMessage()
    upd = type("Upd", (), {"effective_message": chat,
                           "effective_user": type("U", (), {"id": 7, "first_name": "Zed"})()})()
    await bot.booked(upd, ctx)
    assert "Court 8 (outdoor) · 60 min" in chat.sent[0][0]
    ctx.args = ["whenever"]
    await bot.booked(upd, ctx)
    assert chat.sent[-1][0].startswith("Usage: <code>/booked")
