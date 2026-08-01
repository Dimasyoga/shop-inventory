#!/bin/bash
# Timestamped backup of the live database.
#
# Uses SQLite's online backup API rather than `cp`: the app runs in WAL mode with
# a bot thread writing at any moment, so a plain file copy can capture a torn
# database. Safe to run while the container is serving.
#
# The Telegram bot token inside is encrypted, and the key deliberately stays out
# of this file. Restoring onto a machine without the key leaves the bot idle
# until the token is re-entered in Settings -- see the note printed at the end.
#
# Usage: ./backup.sh [output-dir]   (default: ./backups)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUT_DIR="${1:-$SCRIPT_DIR/backups}"
STAMP="$(date +%Y%m%d-%H%M%S)"
NAME="shop-$STAMP.db"

mkdir -p "$OUT_DIR"

BACKUP_PY='
import sqlite3, sys
src = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
dst.close(); src.close()
'

if docker compose ps --status running --services 2>/dev/null | grep -qx app; then
    echo "Backing up from the running container..."
    docker compose exec -T app python -c "$BACKUP_PY" /data/shop.db "/tmp/$NAME"
    docker compose cp "app:/tmp/$NAME" "$OUT_DIR/$NAME"
    docker compose exec -T app rm -f "/tmp/$NAME"
else
    DB="${SHOP_DB_PATH:-$SCRIPT_DIR/shop.db}"
    echo "Container not running; backing up local $DB..."
    [ -f "$DB" ] || { echo "No database at $DB" >&2; exit 1; }
    python3 -c "$BACKUP_PY" "$DB" "$OUT_DIR/$NAME"
fi

echo "Wrote $OUT_DIR/$NAME"
echo
echo "To restore:"
echo "  ./restore.sh $OUT_DIR/$NAME"
echo
echo "It checks the file is an intact shop database, saves the current one aside"
echo "first, and handles stopping and restarting the app."
echo
echo "The bot token in this file is encrypted. Restoring it somewhere without"
echo "the matching key (SHOP_ENCRYPTION_KEY, or /data/.encryption_key in the"
echo "shop-data volume) leaves the bot idle -- re-enter the token in Settings."
