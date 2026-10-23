#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ -d .git ]; then
    echo "== git pull =="
    git pull --ff-only
fi

echo "== rebuild & restart =="
docker compose up -d --build

echo
docker compose ps
echo
docker compose logs --tail=30 || true
