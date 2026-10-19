"""Telegram bot: /padel finds free courts at Padium Canary Wharf (read-only, via Padel Mates),
and game cards track who's playing and who has paid the booker back.

Run: python bot.py   (long polling; needs TELEGRAM_BOT_TOKEN in env or .env)
"""
from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path

from dotenv import load_dotenv
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    PersistenceInput,
    PicklePersistence,
)

import finder
import games
from padelmates import BookingSiteError, PadelMatesClient, ShapeError, load_club
from parser import LONDON, NextQuery, ParseError, parse

log = logging.getLogger("padel")

NEXT_DAYS = 7  # /padel next looks at today + this many days
STATE_FILE = Path(os.environ.get("PADEL_STATE_DIR") or Path(__file__).with_name("state")) / "bot.pickle"
MAX_OFFER_LISTS = 200  # 📌 button lists kept in memory; older buttons answer "expired"

USAGE = (
    "🎾 <b>Free courts at Padium Canary Wharf</b>\n"
    "<code>/padel sat 18:00</code>\n"
    "<code>/padel tomorrow 7pm</code>\n"
    "<code>/padel 24/10 19:30</code>\n"
    "<code>/padel next</code> – earliest free slot\n"
    "Days: today, tomorrow, mon–sun, dd/mm. Times: 18:00, 18, 6pm, 6:30pm. London time.\n\n"
    "<b>Booked a court?</b> Tap its 📌 button under a /padel answer, or\n"
    "<code>/booked sat 18:00 court 3 90 £120</code>\n"
    "Everyone taps 🙋 to join and 💸 once they've paid you. <code>/games</code> lists them."
)
BOOKED_USAGE = (
    "Usage: <code>/booked sat 18:00 court 3 90 £120</code>\n"
    "Day and time first, then court, minutes and total price (all optional)."
)
DOWN = "😕 Can't reach Padel Mates right now. Try again in a minute."
CHANGED = "😕 Padel Mates sent something I don't understand (their API may have changed). Logged it."
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


@dataclass
class Reply:
    text: str
    when: datetime | None = None  # start time of `offers`
    offers: list[finder.Offer] = field(default_factory=list)  # free courts you could book from here


def now_london() -> datetime:
    return datetime.now(LONDON)


# --- /padel ----------------------------------------------------------------

async def padel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return
    now = now_london()
    try:
        query = parse(context.args or [], now)
    except ParseError:
        await send(msg, USAGE, reply=True)
        return

    client: PadelMatesClient = context.bot_data["client"]
    await msg.chat.send_action(ChatAction.TYPING)
    try:
        if isinstance(query, NextQuery):
            answer = await answer_next(client, now)
        else:
            answer = await answer_slot(client, query.when, now)
    except ShapeError:
        log.exception("unexpected Padel Mates response for %s", context.args)
        answer = Reply(CHANGED)
    except BookingSiteError:
        log.exception("Padel Mates request failed for %s", context.args)
        answer = Reply(DOWN)
    text = answer.text + ("\n<i>Booked one? Tap its 📌 so the group can join and pay you.</i>" if answer.offers else "")
    await send(msg, text, reply=True, markup=offer_keyboard(context, client, answer))


async def answer_slot(client: PadelMatesClient, when: datetime, now: datetime) -> Reply:
    day = when.date()
    slots = await client.day(day)
    if not client.bookable(when):
        return Reply(finder.render_not_open(client.club, when, client.horizon, now))
    m = finder.match(slots, when, now)
    next_day = None
    if m.fully_booked:
        d = day + timedelta(days=1)
        if client.bookable(datetime.combine(d, time(0), tzinfo=LONDON)):
            next_day = (d, await client.day(d))
    return Reply(finder.render_match(client.club, m, next_day, now), when, m.exact)


async def answer_next(client: PadelMatesClient, now: datetime) -> Reply:
    for i in range(NEXT_DAYS + 1):
        d = now.date() + timedelta(days=i)
        if i and not client.bookable(datetime.combine(d, time(0), tzinfo=LONDON)):
            break  # past the booking window: nothing to find
        found = finder.earliest(await client.day(d), now)
        if found:
            return Reply(finder.render_next(client.club, found, NEXT_DAYS), found[0], found[1])
    return Reply(finder.render_next(client.club, None, NEXT_DAYS))


def offer_keyboard(context: ContextTypes.DEFAULT_TYPE, client: PadelMatesClient, answer: Reply) -> InlineKeyboardMarkup:
    """The booking link, then 📌 one button per free court and duration to tap once you've booked it.

    Padel Mates has no link to a given slot or even a day, so the link is the club page.
    """
    rows = [[InlineKeyboardButton("📲 Book in Padel Mates", url=client.club.url)]]
    if answer.offers and answer.when:
        choices = [
            (answer.when, o.court.name, o.court.kind, minutes, _amount(o.prices[minutes]))
            for o in answer.offers for minutes in sorted(o.prices)
        ]
        key = remember_offers(context, choices)
        for o in answer.offers:
            rows.append([
                InlineKeyboardButton(f"📌 {name} · {minutes}m", callback_data=f"bk:{key}:{i}")
                for i, (_, name, _, minutes, _) in enumerate(choices) if name == o.court.name
            ])
    return InlineKeyboardMarkup(rows)


def remember_offers(context: ContextTypes.DEFAULT_TYPE, choices: list[tuple]) -> str:
    offers: dict[str, list[tuple]] = context.bot_data.setdefault("offers", {})
    while len(offers) >= MAX_OFFER_LISTS:
        del offers[next(iter(offers))]
    key = secrets.token_hex(4)
    offers[key] = choices
    return key


def _amount(price: str | None) -> float | None:
    try:
        return float(price.split()[0]) if price else None
    except ValueError:
        return None


# --- game cards ------------------------------------------------------------

async def on_booked_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    _, key, index = q.data.split(":")
    choices = context.bot_data.get("offers", {}).get(key)
    if not choices or int(index) >= len(choices):
        await q.answer("These buttons expired. Use /booked sat 18:00 court 3 instead.", show_alert=True)
        return
    when, court, kind, minutes, price = choices[int(index)]
    club = context.bot_data["client"].club
    g = games.new_game(when, court, kind, minutes, price, club.currency, q.from_user.id, q.from_user.first_name)
    await post_card(context, q.message, g)
    await q.answer("Game card posted 🎾 Others tap 🙋 to join.")


async def booked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg, user = update.effective_message, update.effective_user
    if msg is None or user is None:
        return
    club = context.bot_data["client"].club
    try:
        b = games.parse_booked(context.args or [], now_london(), list(club.courts))
    except ParseError:
        await send(msg, BOOKED_USAGE, reply=True)
        return
    known = club.courts.get(b.court)
    g = games.new_game(b.when, b.court, known.kind if known else None, b.duration, b.price, club.currency,
                       user.id, user.first_name)
    await post_card(context, msg, g)


async def post_card(context: ContextTypes.DEFAULT_TYPE, near: Message, g: games.Game) -> None:
    store = chat_games(context)
    games.prune(store, now_london())
    sent = await send(near, games.render_card(g, context.bot_data["client"].club), markup=card_keyboard(g))
    g.message_id = sent.message_id
    store[g.id] = g


def card_keyboard(g: games.Game) -> InlineKeyboardMarkup | None:
    if g.cancelled:
        return None
    b = lambda label, action: InlineKeyboardButton(label, callback_data=f"g:{g.id}:{action}")  # noqa: E731
    return InlineKeyboardMarkup([
        [b("🙋 I'm in", "in"), b("👋 Out", "out"), b("💸 I paid", "paid")],
        [b("🔔 Remind unpaid", "remind"), b("🗑 Cancel", "cancel")],
    ])


async def on_card_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    _, gid, action = q.data.split(":")
    g = chat_games(context).get(gid)
    if g is None:
        await q.answer("This game isn't tracked any more.", show_alert=True)
        return
    user = q.from_user
    club = context.bot_data["client"].club
    before = games.render_card(g, club)
    if action == "in":
        note = g.join(user.id, user.first_name)
    elif action == "out":
        note = g.leave(user.id)
    elif action == "paid":
        note = g.toggle_paid(user.id)
    elif user.id != g.booker.id:
        note = f"Only {g.booker.name}, who booked it, can do that."
    elif action == "remind":
        if g.unpaid:
            await send(q.message, games.render_reminder(g))
            note = "Reminder sent."
        else:
            note = "Everyone in has paid."
    elif action == "cancel":
        g.cancelled = True
        note = "Game cancelled. Cancel the booking in Padel Mates too."
    else:
        note = ""
    await q.answer(note)
    after = games.render_card(g, club)
    if after != before:
        try:
            await q.edit_message_text(after, parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW,
                                      reply_markup=card_keyboard(g))
        except BadRequest as e:  # e.g. two taps racing to the same text
            log.warning("card edit failed: %s", e)


async def list_games(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return
    now = now_london()
    store = chat_games(context)
    games.prune(store, now)
    shown = [g for g in store.values() if g.when > now - timedelta(hours=3) or g.unpaid]
    await send(msg, games.render_list(shown), reply=True)


def chat_games(context: ContextTypes.DEFAULT_TYPE) -> dict[str, games.Game]:
    return context.chat_data.setdefault("games", {})


# --- plumbing --------------------------------------------------------------

async def usage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        await send(update.effective_message, USAGE, reply=True)


async def send(near: Message, text: str, reply: bool = False, markup: InlineKeyboardMarkup | None = None) -> Message:
    if reply:
        return await near.reply_text(text, parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW, reply_markup=markup)
    return await near.chat.send_message(text, parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW,
                                        reply_markup=markup)


async def post_init(app: Application) -> None:
    app.bot_data["client"] = PadelMatesClient(load_club())
    await app.bot.set_my_commands([
        BotCommand("padel", "Free courts at Padium: /padel sat 18:00 · /padel next"),
        BotCommand("booked", "Track a game you booked: /booked sat 18:00 court 3 90 £120"),
        BotCommand("games", "Games in this chat: who's in, who has paid"),
    ])
    log.info("ready: %s (%s)", app.bot_data["client"].club.name, app.bot_data["client"].club.club_id)


async def post_shutdown(app: Application) -> None:
    client = app.bot_data.get("client")
    if client:
        await client.aclose()


def main() -> None:
    load_dotenv()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # it logs every poll, token in the URL
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
    load_club()  # fail fast, before polling, if club.json is missing or mismatched
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Only chat_data (the game cards) is saved; bot_data holds the HTTP client.
    persistence = PicklePersistence(
        STATE_FILE, store_data=PersistenceInput(bot_data=False, chat_data=True, user_data=False, callback_data=False)
    )
    app = (
        Application.builder()
        .token(token)
        .persistence(persistence)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("padel", padel))
    app.add_handler(CommandHandler("booked", booked))
    app.add_handler(CommandHandler("games", list_games))
    app.add_handler(CommandHandler(["start", "help"], usage))
    app.add_handler(CallbackQueryHandler(on_booked_button, pattern=r"^bk:"))
    app.add_handler(CallbackQueryHandler(on_card_button, pattern=r"^g:"))
    app.run_polling(allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY], drop_pending_updates=True)


if __name__ == "__main__":
    main()
