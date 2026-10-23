import glob
import logging
from pathlib import Path

import aiosqlite

from app import config

log = logging.getLogger(__name__)

_conn: aiosqlite.Connection | None = None


def conn() -> aiosqlite.Connection:
    if _conn is None:
        raise RuntimeError("database is not initialised")
    return _conn


async def init():
    global _conn
    path = Path(config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    _conn = await aiosqlite.connect(path)
    _conn.row_factory = aiosqlite.Row
    await _conn.execute("PRAGMA journal_mode=WAL")
    await _conn.execute("PRAGMA foreign_keys=ON")
    await _conn.commit()
    await _migrate()
    log.info("database ready: %s", path)


async def close():
    global _conn
    if _conn is not None:
        await _conn.close()
        _conn = None


async def _migrate():
    cur = await conn().execute("PRAGMA user_version")
    row = await cur.fetchone()
    version = row[0] if row else 0
    await cur.close()

    files = sorted(glob.glob(str(Path(config.BASE_DIR) / "migrations" / "*.sql")))
    for filename in files:
        stem = Path(filename).stem
        try:
            number = int(stem.split("_", 1)[0])
        except ValueError:
            log.warning("skip migration without numeric prefix: %s", filename)
            continue
        if number <= version:
            continue
        sql = Path(filename).read_text(encoding="utf-8")
        log.info("applying migration %s", Path(filename).name)
        await conn().executescript(sql)
        await conn().execute(f"PRAGMA user_version = {number}")
        await conn().commit()
        version = number


async def fetchone(sql, params=()):
    cur = await conn().execute(sql, params)
    row = await cur.fetchone()
    await cur.close()
    return row


async def fetchall(sql, params=()):
    cur = await conn().execute(sql, params)
    rows = await cur.fetchall()
    await cur.close()
    return rows


async def fetchval(sql, params=(), default=None):
    row = await fetchone(sql, params)
    return row[0] if row is not None else default


async def execute(sql, params=()):
    cur = await conn().execute(sql, params)
    await conn().commit()
    return cur


async def get_setting(key, default=None):
    value = await fetchval("SELECT value FROM settings WHERE key = ?", (key,))
    return default if value is None else value


async def set_setting(key, value):
    await execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


async def get_int_setting(key, default=0):
    raw = await get_setting(key)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


async def get_bool_setting(key, default=False):
    raw = await get_setting(key)
    if raw is None:
        return default
    return str(raw).strip() in ("1", "true", "yes", "on")
