from datetime import datetime, timedelta

import pytest

import games
from parser import LONDON, ParseError

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=LONDON)
SAT = datetime(2026, 10, 24, 18, 0, tzinfo=LONDON)
COURTS = ["Centre Court", "Court 1", "Babolat Court 2", "Court 3", "Checkout.com Court 4",
          "Oysho Court 5", "Court 6", "Outdoor Court 7", "Outdoor Court 8"]


def game(price=120.0):
    return games.new_game(SAT, "Court 3", "indoor", 90, price, "GBP", 1, "Anna")


def test_booker_is_in_and_paid():
    g = game()
    assert [(p.name, p.paid) for p in g.players] == [("Anna", True)]
    assert g.share == 30


def test_join_leave_pay_flow():
    g = game()
    assert g.join(2, "Bob") == "You're in 🎾"
    assert g.join(2, "Bob") == "You're already in."
    g.join(3, "Cat")
    g.join(4, "Dan")
    assert g.join(5, "Eve") == "Game's full."
    assert [p.name for p in g.unpaid] == ["Bob", "Cat", "Dan"]
    assert g.toggle_paid(2) == "Marked as paid 💸"
    assert g.toggle_paid(2) == "Marked as not paid."
    assert g.toggle_paid(5) == "Tap 🙋 I'm in first."
    assert g.toggle_paid(1) == "You booked it, so you've paid."
    assert g.leave(1) == "You booked it: cancel the game instead."
    assert g.leave(4) == "You're out."
    assert g.leave(4) == "You weren't in."


def test_card():
    g = game()
    g.join(2, "Bob")
    g.join(3, "<Cat>")
    g.toggle_paid(2)
    club = type("C", (), {"name": "Padium Canary Wharf"})()
    assert games.render_card(g, club).splitlines() == [
        "🎾 <b>Sat 24 Oct 18:00 · Court 3 (indoor) · 90 min</b>",
        "Padium Canary Wharf",
        '💷 £120 total · £30 each, pay <a href="tg://user?id=1">Anna</a>',
        '1. <a href="tg://user?id=1">Anna</a> 📌 booked',
        '2. <a href="tg://user?id=2">Bob</a> ✅ paid',
        '3. <a href="tg://user?id=3">&lt;Cat&gt;</a> ⏳ owes £30',
        "4. ➕ free spot",
    ]
    g.join(4, "Dan")
    g.toggle_paid(3)
    g.toggle_paid(4)
    assert games.render_card(g, club).endswith("All paid 🙌")


def test_card_without_price_and_cancelled():
    g = game(price=None)
    g.join(2, "Bob")
    club = type("C", (), {"name": "Padium"})()
    text = games.render_card(g, club)
    assert "💷" not in text and "Bob</a> ⏳ hasn't paid" in text
    g.cancelled = True
    assert games.render_card(g, club).startswith("❌ <s>Sat 24 Oct 18:00")


def test_reminder_mentions_only_unpaid():
    g = game()
    g.join(2, "Bob")
    g.join(3, "Cat")
    g.toggle_paid(3)
    assert games.render_reminder(g) == (
        '🔔 <a href="tg://user?id=2">Bob</a>: please pay <a href="tg://user?id=1">Anna</a> £30 '
        "for Sat 24 Oct 18:00 · Court 3 (indoor) · 90 min."
    )


def test_prune_drops_old_and_cancelled():
    old, live, cancelled = game(), game(), game()
    old.when = NOW - timedelta(days=15)
    cancelled.cancelled = True
    store = {g.id: g for g in (old, live, cancelled)}
    games.prune(store, NOW)
    assert list(store) == [live.id]


@pytest.mark.parametrize("args,court,duration,price", [
    (["sat", "18:00"], "", None, None),
    (["sat", "18:00", "court", "3", "90", "£120"], "Court 3", 90, 120.0),
    (["sat", "6pm", "£80", "centre", "60m"], "Centre Court", 60, 80.0),
    (["sat", "18", "outdoor", "7"], "Outdoor Court 7", None, None),
    (["sat", "18", "4"], "Checkout.com Court 4", None, None),
    (["sat", "18", "oysho"], "Oysho Court 5", None, None),
    (["sat", "18", "court", "2", "120gbp"], "Babolat Court 2", None, 120.0),
    (["sat", "18", "the", "big", "one"], "The Big One", None, None),
])
def test_parse_booked(args, court, duration, price):
    b = games.parse_booked(args, NOW, COURTS)
    assert (b.when, b.court, b.duration, b.price) == (SAT, court, duration, price)


@pytest.mark.parametrize("args", [[], ["sat"], ["sat", "court"], ["someday", "18:00"]])
def test_parse_booked_bad(args):
    with pytest.raises(ParseError):
        games.parse_booked(args, NOW, COURTS)
