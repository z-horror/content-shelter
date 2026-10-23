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
target.close()
source.close()
PY

tar -czf "$OUT" -C "$TMP" .
find "$ROOT/backups" -name 'neuroslop-*.tar.gz' -mtime +14 -delete

echo "backup: $OUT"
ls -lh "$OUT"
