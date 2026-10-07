#!/usr/bin/env bash
# One-shot deploy/update for the VPS. Safe to re-run.
#
#   ./deploy.sh          build, self-test, start
#   ./deploy.sh update   git pull, rebuild, restart
set -euo pipefail

cd "$(dirname "$0")"

say() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }

if [ "${1:-}" = "update" ]; then
    say "Pulling latest code"
    git pull --ff-only
fi

if [ ! -f .env ]; then
    say "No .env yet — creating one from the template"
    cp .env.example .env
    echo
    echo "Now edit .env and fill in at minimum:"
    echo "  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_IDS, SEC_USER_AGENT"
    echo
    echo "  nano .env"
    echo
    echo "Then run ./deploy.sh again."
    exit 1
fi

# Fail early and readably rather than after a five-minute build.
missing=""
for key in TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_IDS SEC_USER_AGENT; do
    value="$(grep -E "^${key}=" .env | head -1 | cut -d= -f2- || true)"
    [ -z "${value// /}" ] && missing="$missing $key"
done
if [ -n "$missing" ]; then
    echo "ERROR: these are still blank in .env:$missing" >&2
    echo "See README.md 'Setup' for where each one comes from." >&2
    exit 1
fi

mkdir -p data

say "Building"
docker compose build

say "Running self-test"
if ! docker compose run --rm ticker-alerts python -m app.selftest; then
    echo
    echo "Self-test failed. Look up the FAIL line in RUNBOOK.md." >&2
    echo "Not starting the service." >&2
    exit 1
fi

say "Starting"
docker compose up -d

sleep 5
docker compose ps

say "Done"
echo "Message your bot /status to confirm, then /add NVDA AMD to begin."
echo "Logs:  docker compose logs -f"
