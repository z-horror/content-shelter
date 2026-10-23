import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")
except ImportError:  # pragma: no cover
    pass


def _parse_chat(raw):
    if not raw:
        return None
    raw = str(raw).strip()
    if not raw:
        return None
    if raw.lstrip("-").isdigit():
        return int(raw)
    return raw


def _parse_ids(raw):
    result = []
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            result.append(int(part))
    return result


def _parse_int(raw):
    raw = (raw or "").strip()
    return int(raw) if raw.lstrip("-").isdigit() else None


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()
ADMIN_GROUP_ID = os.getenv("ADMIN_GROUP_ID", "").strip()
ADMIN_TOPIC_ID = _parse_int(os.getenv("ADMIN_TOPIC_ID", ""))
ADMIN_USER_IDS = _parse_ids(os.getenv("ADMIN_USER_IDS", ""))
DB_PATH = os.getenv("DB_PATH", str(BASE_DIR / "data" / "neuroslop.sqlite3"))
TIMEZONE = os.getenv("TZ", "Europe/Moscow").strip() or "Europe/Moscow"

CHANNEL_CHAT = _parse_chat(CHANNEL_ID)
ADMIN_CHAT = _parse_chat(ADMIN_GROUP_ID)

# заполняется во время старта
BOT_USERNAME = ""
CHANNEL_NUMERIC_ID = None
CHANNEL_USERNAME = None
ADMIN_CHAT_TYPE = None


def is_admin(user_id):
    return user_id in ADMIN_USER_IDS
