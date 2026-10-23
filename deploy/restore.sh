#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARCHIVE="${1:-}"

if [ -z "$ARCHIVE" ] || [ ! -f "$ARCHIVE" ]; then
    echo "usage: $0 <backup.tar.gz>" >&2
    exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

tar -xzf "$ARCHIVE" -C "$TMP"
RESTORE_DB="$TMP/neuroslop.sqlite3"
if [ ! -f "$RESTORE_DB" ]; then
    echo "ERROR: neuroslop.sqlite3 not found in archive" >&2
    exit 1
fi

python3 - "$RESTORE_DB" <<'PY'
import sqlite3
import sys

conn = sqlite3.connect(sys.argv[1])
check = conn.execute("PRAGMA integrity_check").fetchone()[0]
required = {"settings", "users", "collections", "submissions", "posts", "queue", "admin_sessions"}
tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
conn.close()

if check != "ok":
    raise SystemExit(f"archive verification failed: integrity_check={check}")
if required - tables:
    raise SystemExit(f"archive verification failed: missing tables {sorted(required - tables)}")
PY

cd "$ROOT"
echo "== stopping bot =="
docker compose stop bot || true

BACKUP_DIR="$ROOT/data/pre-restore-$(date +%F-%H%M%S)"
mkdir -p "$BACKUP_DIR"
moved=0
for suffix in "" "-wal" "-shm"; do
    src="data/neuroslop.sqlite3$suffix"
    if [ -e "$src" ]; then
        mv "$src" "$BACKUP_DIR/"
        moved=1
    fi
done
if [ "$moved" -eq 1 ]; then
    echo "previous database saved to $BACKUP_DIR"
fi

cp "$RESTORE_DB" data/neuroslop.sqlite3

echo "== starting bot =="
if ! docker compose up -d; then
    echo "ERROR: failed to start, rolling back" >&2
    rm -f data/neuroslop.sqlite3 data/neuroslop.sqlite3-wal data/neuroslop.sqlite3-shm
    for suffix in "" "-wal" "-shm"; do
        if [ -e "$BACKUP_DIR/neuroslop.sqlite3$suffix" ]; then
            mv "$BACKUP_DIR/neuroslop.sqlite3$suffix" data/
        fi
    done
    docker compose up -d || true
    exit 1
fi

echo "restored from $ARCHIVE"
docker compose logs --tail=20 || true
