import asyncio

import pytest

from app import config, db


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.sqlite3"))
    monkeypatch.setattr(config, "BOT_TOKEN", "test-token")
    monkeypatch.setattr(config, "BOT_USERNAME", "test_bot")
    monkeypatch.setattr(config, "CHANNEL_CHAT", -1001111111111)
    monkeypatch.setattr(config, "CHANNEL_NUMERIC_ID", -1001111111111)
    monkeypatch.setattr(config, "CHANNEL_USERNAME", "testchan")
    monkeypatch.setattr(config, "ADMIN_CHAT", -111)
    monkeypatch.setattr(config, "ADMIN_CHAT_TYPE", "group")
    monkeypatch.setattr(config, "ADMIN_USER_IDS", [1])
    monkeypatch.setattr(db, "_tx_depth", 0)
    yield
    try:
        asyncio.run(db.close())
    except Exception:  # noqa: BLE001
        pass
    db._tx_depth = 0
