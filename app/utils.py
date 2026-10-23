import html
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app import config

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def slugify(name: str) -> str:
    text = (name or "").lower()
    text = "".join(_TRANSLIT.get(ch, ch) for ch in text)
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    text = re.sub(r"_{2,}", "_", text)
    return (text or "podborka")[:40].strip("_") or "podborka"


def esc(value) -> str:
    return html.escape(str(value or ""), quote=False)


def human_size(size) -> str:
    if not size:
        return "?"
    size = int(size)
    if size < 1024:
        return f"{size} Б"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} КБ"
    return f"{size / (1024 * 1024):.1f} МБ"


def human_duration(seconds) -> str:
    if seconds is None:
        return ""
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")


def local_now() -> datetime:
    return datetime.now(ZoneInfo(config.TIMEZONE))


def format_local(iso_value) -> str:
    if not iso_value:
        return "?"
    try:
        dt = datetime.fromisoformat(str(iso_value))
    except ValueError:
        return str(iso_value)
    local = dt.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(config.TIMEZONE))
    return local.strftime("%d.%m.%Y %H:%M")


def parse_user_datetime(text: str):
    """Возвращает UTC-строку или None.

    Поддерживает: +30m, +2h, +1d, HH:MM, DD.MM HH:MM, YYYY-MM-DD HH:MM.
    """
    if not text:
        return None
    raw = text.strip().lower().replace(",", ".")
    tz = ZoneInfo(config.TIMEZONE)
    now = datetime.now(tz)

    match = re.fullmatch(r"\+(\d+)\s*([mhdмчд])", raw)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        delta = {
            "m": timedelta(minutes=amount), "м": timedelta(minutes=amount),
            "h": timedelta(hours=amount), "ч": timedelta(hours=amount),
            "d": timedelta(days=amount), "д": timedelta(days=amount),
        }[unit]
        target = now + delta
        return target.astimezone(timezone.utc).replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")

    formats = ["%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M", "%d.%m %H:%M", "%H:%M"]
    for fmt in formats:
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        if fmt == "%H:%M":
            parsed = now.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
            if parsed <= now:
                parsed += timedelta(days=1)
        elif fmt == "%d.%m %H:%M":
            parsed = parsed.replace(year=now.year, tzinfo=tz)
            if parsed < now - timedelta(days=1):
                parsed = parsed.replace(year=now.year + 1)
        else:
            parsed = parsed.replace(tzinfo=tz)
        return parsed.astimezone(timezone.utc).replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")

    return None


def post_link(message_id: int) -> str:
    if config.CHANNEL_USERNAME:
        return f"https://t.me/{config.CHANNEL_USERNAME}/{message_id}"
    channel_id = config.CHANNEL_NUMERIC_ID
    if channel_id is None and config.CHANNEL_CHAT is not None:
        channel_id = config.CHANNEL_CHAT
    if channel_id is None:
        return ""
    text = str(channel_id)
    internal = text[4:] if text.startswith("-100") else text.lstrip("-")
    return f"https://t.me/c/{internal}/{message_id}"
