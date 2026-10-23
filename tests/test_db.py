import asyncio
import sqlite3
from pathlib import Path

import pytest

from app import config, db


def test_transaction_rollback():
    async def scenario():
        await db.init()
        try:
            with pytest.raises(RuntimeError):
                async with db.transaction():
                    await db.execute(
                        "INSERT INTO users (id, username, first_name, created_at) "
                        "VALUES (1, 'u', 'U', '2026-01-01 00:00:00')"
                    )
                    raise RuntimeError("boom")
            assert await db.fetchval("SELECT COUNT(*) FROM users") == 0
        finally:
            await db.close()

    asyncio.run(scenario())


def test_transaction_commit():
    async def scenario():
        await db.init()
        try:
            async with db.transaction():
                await db.execute(
                    "INSERT INTO users (id, username, first_name, created_at) "
                    "VALUES (1, 'u', 'U', '2026-01-01 00:00:00')"
                )
            assert await db.fetchval("SELECT COUNT(*) FROM users") == 1
        finally:
            await db.close()

    asyncio.run(scenario())


def _create_v1_database(path: Path):
    con = sqlite3.connect(path)
    sql = (Path(config.BASE_DIR) / "migrations" / "001_init.sql").read_text(encoding="utf-8")
    con.executescript(sql)
    con.execute("PRAGMA user_version = 1")
    con.execute(
        "INSERT INTO users (id, username, first_name, created_at) "
        "VALUES (7, 'old', 'Old', '2026-01-01 00:00:00')"
    )
    con.execute(
        "INSERT INTO submissions (id, user_id, source_chat_id, source_message_id, media_type, created_at) "
        "VALUES (3, 7, 7, 99, 'video', '2026-01-01 00:00:00')"
    )
    con.commit()
    con.close()


def test_migration_from_v1_preserves_rows():
    asyncio.run(db.close())

    async def scenario():
        _create_v1_database(Path(config.DB_PATH))
        await db.init()
        try:
            version = await db.fetchval("PRAGMA user_version")
            assert version == 3
            cols = {row[1] for row in await db.fetchall("PRAGMA table_info(submissions)")}
            assert {"file_unique_id", "comment_text", "publishing_at"} <= cols
            user = await db.fetchone("SELECT * FROM users WHERE id = 7")
            assert user is not None and user["username"] == "old"
            submission = await db.fetchone("SELECT * FROM submissions WHERE id = 3")
            assert submission is not None
            assert submission["publishing_at"] is None
        finally:
            await db.close()

    asyncio.run(scenario())


def test_migration_is_idempotent():
    async def scenario():
        await db.init()
        await db.close()
        await db.init()
        try:
            assert await db.fetchval("PRAGMA user_version") == 3
        finally:
            await db.close()

    asyncio.run(scenario())


def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_broken.sql").write_text(
        "CREATE TABLE half_applied (id INTEGER);\n"
        "CREATE TABLE half_applied (id INTEGER);\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)

    async def scenario():
        with pytest.raises(sqlite3.OperationalError):
            await db.init()
        await db.close()
        con = sqlite3.connect(config.DB_PATH)
        try:
            version = con.execute("PRAGMA user_version").fetchone()[0]
            tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            con.close()
        assert version == 0
        assert "half_applied" not in tables

    asyncio.run(scenario())
