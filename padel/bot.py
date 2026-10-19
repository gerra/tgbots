"""Telegram bot: /padel finds free courts at Padium Canary Wharf (read-only, via Padel Mates).

Run: python bot.py   (long polling; needs TELEGRAM_BOT_TOKEN in env or .env)
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, time, timedelta

from dotenv import load_dotenv
from telegram import BotCommand, LinkPreviewOptions, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

import finder
from padelmates import BookingSiteError, PadelMatesClient, ShapeError, load_club
from parser import LONDON, NextQuery, ParseError, parse

log = logging.getLogger("padel")

NEXT_DAYS = 7  # /padel next looks at today + this many days

USAGE = (
    "🎾 <b>Free courts at Padium Canary Wharf</b>\n"
    "<code>/padel sat 18:00</code>\n"
    "<code>/padel tomorrow 7pm</code>\n"
    "<code>/padel 24/10 19:30</code>\n"
    "<code>/padel next</code> – earliest free slot\n"
    "Days: today, tomorrow, mon–sun, dd/mm. Times: 18:00, 18, 6pm, 6:30pm. London time."
)
DOWN = "😕 Can't reach Padel Mates right now. Try again in a minute."
CHANGED = "😕 Padel Mates sent something I don't understand (their API may have changed). Logged it."


def now_london() -> datetime:
    return datetime.now(LONDON)


async def padel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return
    now = now_london()
    try:
        query = parse(context.args or [], now)
    except ParseError:
        await reply(msg, USAGE)
        return

    client: PadelMatesClient = context.bot_data["client"]
    await msg.chat.send_action(ChatAction.TYPING)
    try:
        if isinstance(query, NextQuery):
            text = await answer_next(client, now)
        else:
            text = await answer_slot(client, query.when, now)
    except ShapeError:
        log.exception("unexpected Padel Mates response for %s", context.args)
        text = CHANGED
    except BookingSiteError:
        log.exception("Padel Mates request failed for %s", context.args)
        text = DOWN
    await reply(msg, text)


async def answer_slot(client: PadelMatesClient, when: datetime, now: datetime) -> str:
    day = when.date()
    slots = await client.day(day)
    if not client.bookable(when):
        return finder.render_not_open(client.club, when, client.horizon, now)
    m = finder.match(slots, when, now)
    next_day = None
    if m.fully_booked:
        d = day + timedelta(days=1)
        if client.bookable(datetime.combine(d, time(0), tzinfo=LONDON)):
            next_day = (d, await client.day(d))
    return finder.render_match(client.club, m, next_day, now)


async def answer_next(client: PadelMatesClient, now: datetime) -> str:
    for i in range(NEXT_DAYS + 1):
        d = now.date() + timedelta(days=i)
        if i and not client.bookable(datetime.combine(d, time(0), tzinfo=LONDON)):
            break  # past the booking window: nothing to find
        found = finder.earliest(await client.day(d), now)
        if found:
            return finder.render_next(client.club, found, NEXT_DAYS)
    return finder.render_next(client.club, None, NEXT_DAYS)


async def usage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        await reply(update.effective_message, USAGE)


async def reply(msg, text: str) -> None:
    await msg.reply_text(
        text, parse_mode=ParseMode.HTML, link_preview_options=LinkPreviewOptions(is_disabled=True)
    )


async def post_init(app: Application) -> None:
    app.bot_data["client"] = PadelMatesClient(load_club())
    await app.bot.set_my_commands(
        [BotCommand("padel", "Free courts at Padium: /padel sat 18:00 · /padel next")]
    )
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
    app = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("padel", padel))
    app.add_handler(CommandHandler(["start", "help"], usage))
    app.run_polling(allowed_updates=[Update.MESSAGE], drop_pending_updates=True)


if __name__ == "__main__":
    main()
