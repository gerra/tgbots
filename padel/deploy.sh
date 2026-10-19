#!/usr/bin/env bash
# Push .env, push code, restart. CI (.github/workflows/padel.yml) does the
# actual test + rsync + venv + systemd work on push to main; this drives it from here.
set -euo pipefail

cd "$(dirname "$0")"

echo "Syncing .env to remote server..."
./push-env.sh

echo "Pushing to origin main..."
git push origin main

# Restart even when the push is a no-op (CI only redeploys on new commits),
# so the bot always picks up the freshly synced .env.
echo "Restarting bot..."
ssh hetzner_gb 'systemctl restart padel-bot'

echo "Deploy complete!"
echo
echo "  watch CI:   gh run watch"
echo "  live logs:  ssh hetzner_gb 'journalctl -u padel-bot -f'"
