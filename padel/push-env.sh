#!/usr/bin/env bash
# Push local padel/.env to /opt/padel-bot/.env on hetzner_gb.
# Excluded from git + deploy rsync, so this script is the only way it gets there.
set -euo pipefail

LOCAL_ENV="$(cd "$(dirname "$0")" && pwd)/.env"
REMOTE_HOST="hetzner_gb"
REMOTE_PATH="/opt/padel-bot/.env"

if [ ! -f "$LOCAL_ENV" ]; then
  echo "no .env at $LOCAL_ENV (copy .env.example)" >&2
  exit 1
fi

ssh "$REMOTE_HOST" "mkdir -p $(dirname "$REMOTE_PATH")"
scp "$LOCAL_ENV" "$REMOTE_HOST:$REMOTE_PATH"
ssh "$REMOTE_HOST" "chmod 600 $REMOTE_PATH"
echo "pushed -> $REMOTE_HOST:$REMOTE_PATH"
