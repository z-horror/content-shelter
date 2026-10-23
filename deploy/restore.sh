#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARCHIVE="${1:-}"

if [ -z "$ARCHIVE" ] || [ ! -f "$ARCHIVE" ]; then
    echo "usage: $0 <backup.tar.gz>" >&2
    exit 1
fi

cd "$ROOT"
echo "== stopping bot =="
docker compose stop bot || true

echo "== restoring =="
mkdir -p data
tar -xzf "$ARCHIVE" -C data

echo "== starting bot =="
docker compose up -d

echo "restored from $ARCHIVE"
