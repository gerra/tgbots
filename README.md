# tgbots

Small Telegram bots, one per directory, each deployed to hetzner_gb by its own
GitHub Actions workflow.

| bot | what | deploy |
|---|---|---|
| [`padel/`](padel/) | @padelbloksbot: `/padel sat 18:00` finds free courts at Padium Canary Wharf | `.github/workflows/padel.yml` → `/opt/padel-bot` (systemd `padel-bot`) |
