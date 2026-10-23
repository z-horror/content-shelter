#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB="$ROOT/data/neuroslop.sqlite3"

if [ ! -f "$DB" ]; then
    echo "ERROR: database not found: $DB" >&2
    exit 1
fi

mkdir -p "$ROOT/backups"
STAMP="$(date +%F-%H%M%S)"
OUT="$ROOT/backups/neuroslop-$STAMP.tar.gz"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

python3 - "$DB" "$TMP/neuroslop.sqlite3" <<'PY'
import sqlite3
import sys

source = sqlite3.connect(sys.argv[1])
target = sqlite3.connect(sys.argv[2])
with target:
    source.backup(target)

check = target.execute("PRAGMA integrity_check").fetchone()[0]
required = {"settings", "users", "collections", "submissions", "posts", "queue", "admin_sessions"}
tables = {row[0] for row in target.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
missing = required - tables

target.close()
source.close()

if check != "ok":
    raise SystemExit(f"backup verification failed: integrity_check={check}")
if missing:
    raise SystemExit(f"backup verification failed: missing tables {sorted(missing)}")
PY

tar -czf "$OUT" -C "$TMP" .
chmod 600 "$OUT"
find "$ROOT/backups" -name 'neuroslop-*.tar.gz' -mtime +14 -delete

echo "backup: $OUT"
ls -lh "$OUT"
