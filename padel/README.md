# padel-bot

Telegram bot (@padelbloksbot) that finds free padel courts at **Padium Canary
Wharf** (10 Bank Street, E14 4DE). It only ever searches that one club. It is
read-only towards the club: no login, no booking, no payment. It reads the
free slots that Padel Mates (Padium's booking system since it left Playtomic)
shows on the club's public page, and links there so you can book in the app.
Once someone has booked, a game card in the chat tracks who's playing and who
has paid the booker back.

Runs on hetzner_gb at `/opt/padel-bot` as the `padel-bot` systemd unit, using
long polling.

## Commands

| | |
|---|---|
| `/padel sat 18:00` | courts free at that time, or the nearest free times that day |
| `/padel tomorrow 7pm` · `/padel 24/10 19:30` · `/padel today 20:00` | same |
| `/padel tomorrow 4pm-6pm` | every free start time in a range, both ends included (also `16:00-18:00`, `4-6pm`, `4pm to 6pm`) |
| `/padel next` | earliest free slot from now, up to 7 days ahead |
| add `indoor` / `outdoor` | to any of the above: `/padel tomorrow 6pm indoor`, `/padel next outdoor` |
| add a length | `1h`, `1.5h`, `2h` (also `90m`, `1h30`), or a span like `1-2h`: `/padel next 1-2h`, `/padel sat 5-7pm indoor 2h` |
| `/booked sat 18:00 court 3 90 £120` | post a game card for a court you booked (court, minutes and price optional, any order) |
| `/games` | games tracked in this chat: how many are in, how many still owe |
| `/padel` | usage |

- **Days:** `today`, `tomorrow`, `mon`..`sun` (the next occurrence, or today if it's that day and the time hasn't passed yet), and `dd/mm`.
- **Times:** `18:00`, `18`, `6pm`, `6:30pm`.
- All times are Europe/London. Without a length, 60 and 90 min bookings are counted. A court that's free only for a 2-hour booking at some time shows up with `2h` or `1-2h`.

```
🎾 Padium Canary Wharf, Sat 24 Oct
✅ 18:30 free:
• Centre Court (indoor) 60/90 min £100/£150
• Court 3 (indoor) 60 min £80
• Outdoor Court 7 (outdoor) 60/90 min £80/£120
Booked one? Tap its 📌 so the group can join and pay you.
[📲 Book in Padel Mates]
[📌 Centre Court · 60m] [📌 Centre Court · 90m]
[📌 Court 3 · 60m]
[📌 Outdoor Court 7 · 60m] [📌 Outdoor Court 7 · 90m]
```

### Booking link

📲 opens Padium's page on Padel Mates (`padelmates.se/club/padium`). Padel Mates has no link to a particular slot, or even a day. Its web routes are only `/club/:id` plus membership and credit tabs, and the club grid always opens on today, even when the date comes from their own search page (checked in a browser, Oct 2026). So you still pick the day and court in the app.

### Who booked, who paid

Padel Mates won't tell a bot who booked or paid without the booker's login, so the group tells the bot:

1. After booking in the app, tap that court's 📌 under the `/padel` answer. For a booking made some other way, use `/booked sat 18:00 court 3 90 £120`.
2. The bot posts a game card:
   ```
   🎾 Sat 24 Oct 18:00 · Court 3 (indoor) · 90 min
   Padium Canary Wharf
   💷 £120 total · £30 each, pay Anna
   1. Anna 📌 booked
   2. Bob ✅ paid
   3. Cat ⏳ owes £30
   4. ➕ free spot
   [🙋 I'm in] [👋 Out] [💸 I paid]
   [🔔 Remind unpaid] [🗑 Cancel]
   ```
3. Players tap 🙋 to take a spot and 💸 once they've paid the booker. The booker counts as paid. A share is the court price divided by 4.
4. Only the booker can use 🔔 (a new message that pings everyone who still owes) and 🗑 (cancels the card only, not the Padel Mates booking).

Cards are saved in `state/bot.pickle` (PTB `PicklePersistence`, game cards only), so they survive restarts and deploys. Games are forgotten 14 days after they're played. `/games` keeps listing past games while someone still owes.

## How it works

| file | |
|---|---|
| `bot.py` | Telegram handlers (python-telegram-bot 21, async, long polling) and button callbacks |
| `games.py` | game cards: who's in, who paid, `/booked` parsing |
| `padelmates.py` | API client: one call per London day, 60 s cache, 10 s timeout |
| `parser.py` | `sat 18:00` → London datetime |
| `finder.py` | exact / nearest / fully-booked matching, plus the HTML reply |
| `probe.py` | hits Padel Mates live, prints the response shapes, writes `club.json` and live fixtures |
| `club.json` | club id, courts (name, indoor/outdoor), written once by `probe.py --write` |

Free slots come from the endpoint the padelmates.se club page draws its grid from:

```
GET https://fastapi-production-fargate.padelmates.io/player/player_booking/all_courts_slot_prices_v3
    ?club_id=<32-hex>&start_datetime=<epoch ms>&end_datetime=<epoch ms>&lang=en
    (headers: Origin: https://padelmates.se, a browser-like User-Agent)
```

The bot asks for one London day at a time, from local midnight to the next local midnight. That window is 23 or 25 hours on clock-change days. Checked live against Padium (club `47d2eb0db7194a9dbd29783c3a2a82ad`, Oct 2026):

- `allSlots` lists only bookable slots: `{courtName, courtId, duration, price: 80.0, startTimestamp (epoch ms), startTime ("05:00", UTC), reservedIntersection, ...}`. There's one entry per court, start time and duration (60/90/120). The bot converts `startTimestamp` to London time and counts 60 and 90 min.
- `originalCourts` gives each court's `indoor` flag. That's where "(indoor)" / "(outdoor)" comes from, with `club.json` as a fallback.
- `advance_booking_horizon_ends_at` is where the booking window ends, about 7 days out to the minute. Past it, the API returns no slots and `too_early_for_advance_booking: true`. The bot then replies "isn't bookable yet, try from around …" instead of claiming the day is fully booked.
- The response also carries `reservedBookings` (other players' names, ids and booking codes). The bot never uses or logs it, and `probe.py` strips it from saved fixtures.

Another project once saw `allSlots` leave out a free start time that the website grid showed. I haven't reproduced that, but if the bot says "taken" for a time you expected free, check the app.

Courts: Centre Court, Court 1, Babolat Court 2, Court 3, Checkout.com Court 4, Oysho Court 5 and Court 6 are indoor. Outdoor Court 7 and Outdoor Court 8 are outdoor. Prices: £80/£120 for 60/90 min, Centre Court £100/£150.

Padium left Playtomic, and nothing for it is listed there (checked Oct 2026). `probe.py` re-checks the endpoint. Re-run it if the bot starts replying "Padel Mates sent something I don't understand".

## Set up (once)

1. **Create the bot.** In [@BotFather](https://t.me/BotFather), send `/newbot` (already done: @padelbloksbot) and copy the token.
2. **Register the command.** The bot calls `setMyCommands` on start, so `/padel` shows up in the menu automatically. To set it by hand, send `/setcommands` to BotFather, pick the bot, and send:
   ```
   padel - Free courts at Padium: /padel sat 18:00 · /padel next
   booked - Track a game you booked: /booked sat 18:00 court 3 90 £120
   games - Games in this chat: who's in, who has paid
   ```
3. **Leave privacy mode ON.** That's the BotFather default (`/setprivacy` → Enabled). The bot only needs commands, and button taps reach it either way. In a group with several bots, type `/padel@padelbloksbot sat 18:00`.
4. **Add it to a group.** Open the group, then Add members, search for `@padelbloksbot`, and add it. It doesn't need to be an admin.
5. **Find the club** (needs network access to `padelmates.se` and `fastapi-production-fargate.padelmates.io`):
   ```sh
   cd padel && python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
   .venv/bin/python probe.py --club-id 47d2eb0db7194a9dbd29783c3a2a82ad --write   # Padium Canary Wharf
   git add club.json tests/fixtures && git commit -m "padel: club.json"
   ```
   This is already done and committed. The probe prints the response shapes and writes `club.json` plus scrubbed real fixtures. Re-run it if courts change.
6. **Configure the server.** Run `cp .env.example .env`, fill in `TELEGRAM_BOT_TOKEN` (and optionally `PADIUM_CLUB_ID`), then run `./push-env.sh`.
7. **Add GitHub secrets.** In this repo, go to Settings → Environments → `default` and add the same `DEPLOY_HOST`, `DEPLOY_USER` (root) and `DEPLOY_KEY` that fashier and www use.

## Deploy

Deploys are automatic. Any push to `main` that touches `padel/` triggers `.github/workflows/padel.yml`, which does this:

1. Runs ruff and pytest.
2. Rsyncs `padel/` to `/opt/padel-bot`. `.env`, `venv` and `state/` (the game cards) are never touched.
3. Runs `pip install` into the venv.
4. Installs `padel-bot.service`, enables it and restarts it.
5. Fails, and attaches the journal to the run, if the unit doesn't come up.

```sh
./deploy.sh        # push .env, push main, restart
```

### Elsewhere (Docker / Fly.io / Railway / any VPS)

The bot has no ports and no database. It needs `TELEGRAM_BOT_TOKEN`, plus a small volume for `state/` if you want game cards to survive redeploys.

```sh
docker build -t padel-bot . && docker run -d --restart=unless-stopped --env-file .env -v padel-state:/app/state padel-bot
```

- **Fly.io:** run `fly launch --no-deploy` (with no services or HTTP checks), then `fly secrets set TELEGRAM_BOT_TOKEN=…`, then `fly deploy`. Keep exactly one machine with `fly scale count 1`. Two pollers fight with `Conflict: terminated by other getUpdates`.
- **Railway:** create a new project from the GitHub repo with root directory `padel`. Railway picks up the Dockerfile. Add `TELEGRAM_BOT_TOKEN` under Variables.

## Look / poke

```sh
ssh hetzner_gb 'journalctl -u padel-bot -f'
ssh hetzner_gb 'systemctl status padel-bot --no-pager'
ssh hetzner_gb 'systemctl restart padel-bot'
```

## Develop

```sh
cd padel && .venv/bin/pytest -q && .venv/bin/ruff check .
TELEGRAM_BOT_TOKEN=… .venv/bin/python bot.py      # run locally (stop the server copy first: one poller per token)
```

`tests/fixtures/sample_*.json` are hand-made in Padel Mates' response shape. They cover a BST Saturday, a fully booked Sunday (the day the clocks go back), and a GMT Monday. `live_slots_*.json` are real Padium responses saved by `probe.py --write`, with other players' bookings stripped. A test checks that they still parse.
