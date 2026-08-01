#!/bin/bash
# Restore the database from a backup taken by ./backup.sh.
#
# The counterpart to backup.sh, which used to print these steps and leave the
# operator to retype them under pressure. Restoring is the one moment the shop's
# whole history is at stake, so this does the parts that are easy to get wrong:
#
#   * refuses a backup that is not a readable, intact SQLite database with this
#     app's tables in it -- overwriting good data with a truncated file is worse
#     than not restoring at all;
#   * shows what is in the backup and asks before touching anything;
#   * copies the CURRENT database aside first, so a restore of the wrong file is
#     itself undoable;
#   * removes the -wal/-shm sidecars, whose leftover frames from the old database
#     would otherwise be replayed straight back on top of the restored file.
#
# Usage: ./restore.sh <backup-file> [--yes]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKUP="${1:-}"
ASSUME_YES=""
[ "${2:-}" = "--yes" ] && ASSUME_YES=1

if [ -z "$BACKUP" ]; then
    echo "Usage: $0 <backup-file> [--yes]" >&2
    echo >&2
    echo "Available backups:" >&2
    ls -1t "$SCRIPT_DIR/backups"/*.db 2>/dev/null | head -10 >&2 || echo "  (none in ./backups)" >&2
    exit 2
fi
[ -f "$BACKUP" ] || { echo "No such file: $BACKUP" >&2; exit 1; }
BACKUP="$(cd "$(dirname "$BACKUP")" && pwd)/$(basename "$BACKUP")"

# Reads the candidate and refuses anything that is not a sound database for THIS
# app: integrity_check catches truncation and corruption, and the table check
# catches a perfectly valid database that happens to belong to something else.
#
# Deliberately written without a single quote or a backslash anywhere: the whole
# program is carried inside a single-quoted shell string, where both would have to
# be escaped, and an escaping slip here fails closed -- every backup gets rejected,
# including the good one you are trying to restore at the worst possible moment.
INSPECT_PY='
import sqlite3, sys
path = sys.argv[1]
try:
    conn = sqlite3.connect("file:" + path + "?mode=ro", uri=True)
    result = conn.execute("PRAGMA integrity_check").fetchone()[0]
except sqlite3.DatabaseError as e:
    sys.exit("not a readable SQLite database (" + str(e) + ")")
if result != "ok":
    sys.exit("failed its integrity check: " + str(result))
names = {r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type = ?", ("table",))}
missing = {"products", "orders", "order_items", "settings"} - names
if missing:
    sys.exit("does not look like a shop database (no "
             + ", ".join(sorted(missing)) + " table)")
def count(table):
    return conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
batches = count("restock_batches") if "restock_batches" in names else 0
newest = conn.execute("SELECT MAX(created_at) FROM orders").fetchone()[0] or "never"
print("  products: {}   orders: {}   restock batches: {}".format(
    count("products"), count("orders"), batches))
print("  most recent order: " + str(newest))
'

COMPOSE_RUNNING=""
if docker compose ps --status running --services 2>/dev/null | grep -qx app; then
    COMPOSE_RUNNING=1
fi

echo "Checking $BACKUP..."
if command -v python3 >/dev/null 2>&1; then
    python3 -c "$INSPECT_PY" "$BACKUP" || {
        echo "Refusing to restore from this file." >&2; exit 1; }
elif [ -n "$COMPOSE_RUNNING" ] || docker compose config >/dev/null 2>&1; then
    # No python on the host, but the image has one. Mounted read-only: this step
    # only ever reads the candidate.
    docker compose run --rm -T -v "$BACKUP:/tmp/candidate.db:ro" \
        --entrypoint python app -c "$INSPECT_PY" /tmp/candidate.db || {
        echo "Refusing to restore from this file." >&2; exit 1; }
else
    echo "Need python3 (or a working docker compose) to check the backup first." >&2
    exit 1
fi

if [ -z "$ASSUME_YES" ]; then
    echo
    echo "This REPLACES the live database with the contents above."
    printf "Type 'restore' to continue: "
    read -r reply
    [ "$reply" = "restore" ] || { echo "Cancelled."; exit 1; }
fi

# The current database goes to a timestamped safety copy before anything is
# overwritten, so picking the wrong backup file costs nothing but a second run.
STAMP="$(date +%Y%m%d-%H%M%S)"
SAFETY_DIR="$SCRIPT_DIR/backups"
SAFETY="$SAFETY_DIR/pre-restore-$STAMP.db"
mkdir -p "$SAFETY_DIR"

echo
echo "Saving the current database to $SAFETY ..."
if ! "$SCRIPT_DIR/backup.sh" "$SAFETY_DIR" >/dev/null 2>&1; then
    echo "Could not back up the current database. Nothing has been changed." >&2
    exit 1
fi
# backup.sh names its own output; rename the one it just wrote so the safety copy
# is obvious in the directory listing next to the ordinary nightly backups.
LATEST="$(ls -1t "$SAFETY_DIR"/shop-*.db 2>/dev/null | head -1)"
if [ -n "$LATEST" ]; then
    mv "$LATEST" "$SAFETY"
    echo "  saved."
else
    echo "Expected a backup in $SAFETY_DIR and found none. Stopping." >&2
    exit 1
fi

if [ -n "$COMPOSE_RUNNING" ]; then
    echo "Stopping the app..."
    docker compose down
    echo "Restoring into the volume..."
    # One shell so the sidecar removal and the copy cannot be separated by a
    # failure that leaves stale WAL frames next to a fresh database.
    docker compose run --rm -T --entrypoint sh app -c \
        'rm -f /data/shop.db-wal /data/shop.db-shm && cat > /data/shop.db' < "$BACKUP"
    echo "Starting the app..."
    docker compose up -d
    echo "Waiting for it to report healthy..."
    for _ in $(seq 1 30); do
        if curl -fsS "http://localhost:${HOST_PORT:-5000}/healthz" >/dev/null 2>&1; then
            echo "Restored. The app is up."
            exit 0
        fi
        sleep 2
    done
    echo "Restored, but the app has not reported healthy yet -- check: docker compose logs app" >&2
    exit 1
fi

DB="${SHOP_DB_PATH:-$SCRIPT_DIR/shop.db}"
echo "Container not running; restoring local $DB ..."
rm -f "$DB-wal" "$DB-shm"
cp "$BACKUP" "$DB"
echo "Restored. Start the app when ready."
echo
echo "If the bot stays idle afterwards, the token was encrypted with a key this"
echo "machine does not have -- re-enter it in Settings."
