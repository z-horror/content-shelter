#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "== Neuroslop bot installer =="
echo "project: $ROOT"

if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker is not installed" >&2
    exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
    echo "ERROR: docker compose plugin is not available" >&2
    exit 1
fi

if ! docker info >/dev/null 2>&1; then
    echo "ERROR: no access to docker daemon (run as root or add user to docker group)" >&2
    exit 1
fi

if [ ! -f .env ]; then
    echo
    echo "Файл .env не найден — создаём."
    read -rp "BOT_TOKEN: " input_token
    read -rp "CHANNEL_ID (@username или -100...): " input_channel
    read -rp "ADMIN_GROUP_ID (-100...): " input_group
    read -rp "ADMIN_USER_IDS (через запятую): " input_admins
    read -rp "TZ [Europe/Moscow]: " input_tz
    input_tz="${input_tz:-Europe/Moscow}"

    cat > .env <<EOF
BOT_TOKEN=$input_token
CHANNEL_ID=$input_channel
ADMIN_GROUP_ID=$input_group
ADMIN_TOPIC_ID=
ADMIN_USER_IDS=$input_admins
DB_PATH=/data/neuroslop.sqlite3
TZ=$input_tz
EOF
    chmod 600 .env
    echo ".env создан"
else
    echo ".env найден, используем его"
fi

mkdir -p data
chmod 700 data || true

echo
echo "== build & start =="
docker compose up -d --build

TOKEN="$(grep -E '^BOT_TOKEN=' .env | cut -d= -f2- | tr -d '\r')"
echo
echo "== checking bot token =="
ok=0
for _ in $(seq 1 20); do
    if curl -sf "https://api.telegram.org/bot${TOKEN}/getMe" >/dev/null 2>&1; then
        ok=1
        break
    fi
    sleep 2
done

if [ "$ok" -eq 1 ]; then
    echo "telegram API: OK"
else
    echo "WARNING: telegram API did not answer, check token and network" >&2
fi

echo
docker compose ps
echo
echo "== last logs =="
sleep 3
docker compose logs --tail=30 || true
echo
echo "Готово. Открой бота в Telegram и нажми /start."
