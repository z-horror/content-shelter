import json

from app import db
from app.utils import now_iso


async def set_session(user_id: int, **payload):
    await db.execute(
        "INSERT INTO admin_sessions (user_id, payload, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at",
        (user_id, json.dumps(payload, ensure_ascii=False), now_iso()),
    )


async def get_session(user_id: int):
    raw = await db.fetchval("SELECT payload FROM admin_sessions WHERE user_id = ?", (user_id,))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


async def update_session(user_id: int, **fields):
    payload = await get_session(user_id) or {}
    payload.update(fields)
    await set_session(user_id, **payload)
    return payload


async def clear_session(user_id: int):
    await db.execute("DELETE FROM admin_sessions WHERE user_id = ?", (user_id,))
