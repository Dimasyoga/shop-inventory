#!/bin/bash
# Pull the latest code and restart the app. This is the whole update procedure.
#
# The database and session key live in the shop-data volume, not in this
# directory, so nothing here touches them.

set -euo pipefail

cd "$(cd "$(dirname "$0")" && pwd)"

if [ ! -f .env ]; then
    echo "No .env found. Copy .env.example to .env and set the admin password first." >&2
    exit 1
fi

echo "==> Backing up the database first"
./backup.sh

echo "==> Pulling latest code"
git pull --ff-only

echo "==> Rebuilding and restarting"
docker compose up -d --build

echo "==> Waiting for health check"
for i in $(seq 1 30); do
    if curl -fsS "http://localhost:${HOST_PORT:-5000}/healthz" >/dev/null 2>&1; then
        echo "Healthy. Deploy complete."
        docker compose ps
        exit 0
    fi
    sleep 2
done

echo "App did not become healthy in 60s. Recent logs:" >&2
docker compose logs --tail 50 app >&2
exit 1
