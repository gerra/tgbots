# padel-bot

Telegram bot (@padelbloksbot) that finds free padel courts at **Padium Canary
Wharf** (10 Bank Street, E14 4DE). It only ever searches that one club. It is
read-only: no login, no booking, no payment. It reads Playtomic's public
availability and links to the club page so you can book in the app.

Runs on hetzner_gb at `/opt/padel-bot` as the `padel-bot` systemd unit, using
long polling.

## Commands

| | |
|---|---|
| `/padel sat 18:00` | courts free at that time, or the nearest free times that day |
| `/padel tomorrow 7pm` · `/padel 24/10 19:30` · `/padel today 20:00` | same |
| `/padel next` | earliest free slot from now, up to 7 days ahead |
| `/padel` | usage |

- **Days:** `today`, `tomorrow`, `mon`..`sun` (the next occurrence, or today if it's that day and the time hasn't passed yet), and `dd/mm`.
- **Times:** `18:00`, `18`, `6pm`, `6:30pm`.
- All times are Europe/London. Only 60 and 90 min slots are counted.

```
🎾 Padium Canary Wharf, Sat 24 Oct
✅ 18:00 free: Court 3 (indoor) 60/90 min £80/£120 · Court 8 (outdoor) 60 min £60
Book in Playtomic
```

## How it works

| file | |
|---|---|
| `bot.py` | Telegram handlers (python-telegram-bot 21, async, long polling) |
| `playtomic.py` | API client: one availability call per day, 60 s cache, 10 s timeout |
| `parser.py` | `sat 18:00` → London datetime |
| `finder.py` | exact / nearest / fully-booked matching, plus the HTML reply |
| `probe.py` | hits Playtomic live, prints the response shapes, writes `club.json` and live fixtures |
| `club.json` | tenant id, slug, courts (name, indoor/outdoor), written once by `probe.py --write` |

Availability comes from `GET https://playtomic.com/api/clubs/availability?tenant_id=…&date=YYYY-MM-DD&sport_id=PADEL`.
That is the no-auth endpoint the playtomic.com club pages use. Notes on the other endpoints:

- `api.playtomic.io/v1/availability` sits behind a CloudFront rule that returns 403 for everything.
- `api.app.playtomic.io` has wanted a logged-in bearer token since 2026.

`probe.py` tries all three, so re-run it if the bot starts replying "Playtomic sent something I don't understand".

Slots come back as a UTC `start_date` + `start_time`. The bot converts them to London time with zoneinfo, so it handles BST and GMT.

## Set up (once)

1. **Create the bot.** In [@BotFather](https://t.me/BotFather), send `/newbot` (already done: @padelbloksbot) and copy the token.
2. **Register the command.** The bot calls `setMyCommands` on start, so `/padel` shows up in the menu automatically. To set it by hand, send `/setcommands` to BotFather, pick the bot, and send:
   ```
   padel - Free courts at Padium: /padel sat 18:00 · /padel next
   ```
3. **Leave privacy mode ON.** That's the BotFather default (`/setprivacy` → Enabled). The bot only needs commands. In a group with several bots, type `/padel@padelbloksbot sat 18:00`.
4. **Add it to a group.** Open the group, then Add members, search for `@padelbloksbot`, and add it. It doesn't need to be an admin.
5. **Find the club** (needs network access to playtomic.com):
   ```sh
   cd padel && python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
   .venv/bin/python probe.py --write     # prints shapes + tenant id, writes club.json + tests/fixtures/live_*.json
   git add club.json tests/fixtures && git commit -m "padel: Padium club.json"
   ```
6. **Configure the server.** Run `cp .env.example .env`, fill in `TELEGRAM_BOT_TOKEN` (and optionally `PADIUM_TENANT_ID`), then run `./push-env.sh`.
7. **Add GitHub secrets.** In this repo, go to Settings → Environments → `default` and add the same `DEPLOY_HOST`, `DEPLOY_USER` (root) and `DEPLOY_KEY` that fashier and www use.

## Deploy

Deploys are automatic. Any push to `main` that touches `padel/` triggers `.github/workflows/padel.yml`, which does this:

1. Runs ruff and pytest.
2. Rsyncs `padel/` to `/opt/padel-bot`. `.env` and `venv` are never touched.
3. Runs `pip install` into the venv.
4. Installs `padel-bot.service`, enables it and restarts it.
5. Fails, and attaches the journal to the run, if the unit doesn't come up.

```sh
./deploy.sh        # push .env, push main, restart
```

### Elsewhere (Docker / Fly.io / Railway / any VPS)

The bot has no ports, no disk and no database. All it needs is `TELEGRAM_BOT_TOKEN`.

```sh
docker build -t padel-bot . && docker run -d --restart=unless-stopped --env-file .env padel-bot
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

`tests/fixtures/sample_*.json` are hand-made in Playtomic's response shape. They cover a BST Saturday, the Sunday the clocks go back, and a GMT Monday. `live_*.json` are real responses saved by `probe.py --write`, and a test checks that they still parse.
