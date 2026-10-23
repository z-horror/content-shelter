import asyncio
import json
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest

from app import db
from app.services import link_download, link_flow, publishing
from app.scheduler import _process
from app.utils import now_iso

from tests.helpers import FakeBot, copy_method, seed_submission


def _result(tmp_path, **overrides):
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    path = work / "video.mp4"
    path.write_bytes(b"x" * 1000)
    data = {
        "path": path,
        "size": 1000,
        "duration": 60,
        "url": "https://www.youtube.com/watch?v=abcDEF12345",
        "meta": {
            "title": "Test video",
            "author": "Tester",
            "description": "Description",
            "tags": ["one", "two"],
        },
    }
    data.update(overrides)
    return link_download.DownloadResult(**data)


def _user():
    return SimpleNamespace(id=2, username="user", first_name="User")


def test_create_link_submission_uses_existing_flow(tmp_path):
    async def scenario(tmp_path):
        await db.init()
        try:
            result = _result(tmp_path)
            bot = FakeBot()
            submission_id = await link_flow.create_link_submission(bot, _user(), result)

            submission = await db.fetchone("SELECT * FROM submissions WHERE id = ?", (submission_id,))
            assert submission["status"] == "pending"
            assert submission["source_url"] == result.url
            assert submission["source_chat_id"] == -111
            assert submission["source_message_id"] == 2001
            assert submission["file_id"] == "video-file-id"
            assert json.loads(submission["source_meta"])["title"] == "Test video"
            assert bot.sent and bot.sent[0][0] == "video"
            assert "Test video" in bot.sent[0][1]
            assert await db.fetchval("SELECT submissions_total FROM users WHERE id = 2") == 1
        finally:
            await db.close()

    asyncio.run(scenario(tmp_path))


def test_create_link_submission_deletes_row_on_upload_failure(tmp_path):
    async def scenario():
        await db.init()
        try:
            result = _result(tmp_path)
            error = TelegramBadRequest(copy_method(), "Bad Request: chat not found")
            bot = FakeBot(send_error=error)
            with pytest.raises(link_flow.LinkUploadFailed):
                await link_flow.create_link_submission(bot, _user(), result)
            assert await db.fetchval("SELECT COUNT(*) FROM submissions") == 0
        finally:
            await db.close()

    asyncio.run(scenario())


def test_duplicate_link_blocks_download(tmp_path, monkeypatch):
    async def scenario():
        await db.init()
        try:
            submission_id = await seed_submission(1, user_id=2, status="approved")
            await db.execute(
                "UPDATE submissions SET source_url = ? WHERE id = ?",
                ("https://www.youtube.com/watch?v=abcDEF12345", submission_id),
            )
            await db.execute(
                "INSERT INTO posts (channel_id, message_id, submission_id, media_type, published_at) "
                "VALUES (-1001111111111, 555, ?, 'video', ?)",
                (submission_id, now_iso()),
            )

            called = False

            async def fake_download(url):
                nonlocal called
                called = True
                raise AssertionError("must not download")

            monkeypatch.setattr(link_download, "download", fake_download)

            async def not_cancelled():
                return False

            with pytest.raises(link_flow.LinkDuplicate):
                await link_flow.download_and_submit(FakeBot(), _user(), "https://youtu.be/abcDEF12345", not_cancelled)
            assert called is False
        finally:
            await db.close()

    asyncio.run(scenario())


def test_download_and_submit_success_cleans_file(tmp_path, monkeypatch):
    async def scenario():
        await db.init()
        try:
            result = _result(tmp_path)

            async def fake_download(url):
                return result

            monkeypatch.setattr(link_download, "download", fake_download)

            async def not_cancelled():
                return False

            submission_id = await link_flow.download_and_submit(
                FakeBot(), _user(), "https://youtu.be/abcDEF12345", not_cancelled
            )
            assert submission_id == 1
            assert not result.path.exists()
            assert await db.fetchval("SELECT COUNT(*) FROM submissions") == 1
        finally:
            await db.close()

    asyncio.run(scenario())


def test_download_and_submit_cancelled_cleans_file(tmp_path, monkeypatch):
    async def scenario():
        await db.init()
        try:
            result = _result(tmp_path)

            async def fake_download(url):
                return result

            monkeypatch.setattr(link_download, "download", fake_download)

            async def cancelled():
                return True

            with pytest.raises(link_flow.LinkCancelled):
                await link_flow.download_and_submit(
                    FakeBot(), _user(), "https://www.youtube.com/watch?v=abcDEF12345", cancelled
                )
            assert not result.path.exists()
            assert await db.fetchval("SELECT COUNT(*) FROM submissions") == 0
        finally:
            await db.close()

    asyncio.run(scenario())


def test_download_failure_notifies_admin_without_submission(tmp_path, monkeypatch):
    async def scenario():
        await db.init()
        try:
            async def fake_download(url):
                raise link_download.DownloadFailed("unavailable")

            monkeypatch.setattr(link_download, "download", fake_download)

            async def not_cancelled():
                return False

            bot = FakeBot()
            with pytest.raises(link_download.DownloadFailed):
                await link_flow.download_and_submit(
                    bot, _user(), "https://www.youtube.com/watch?v=abcDEF12345", not_cancelled
                )
            assert await db.fetchval("SELECT COUNT(*) FROM submissions") == 0
            assert any("Не удалось скачать" in text for _, text in bot.messages)
        finally:
            await db.close()

    asyncio.run(scenario())


def test_link_submission_can_be_scheduled_and_published(tmp_path, monkeypatch):
    async def scenario():
        await db.init()
        try:
            result = _result(tmp_path)

            async def fake_download(url):
                return result

            monkeypatch.setattr(link_download, "download", fake_download)

            async def not_cancelled():
                return False

            bot = FakeBot()
            submission_id = await link_flow.download_and_submit(
                bot, _user(), "https://www.youtube.com/watch?v=abcDEF12345", not_cancelled
            )
            past = "2020-01-01 00:00:00"
            await publishing.enqueue(bot, submission_id, past, caption_text=None)
            queue_row = await db.fetchone(
                "SELECT * FROM queue WHERE submission_id = ? AND status = 'waiting'", (submission_id,)
            )
            await _process(queue_row, bot)

            submission = await db.fetchone("SELECT * FROM submissions WHERE id = ?", (submission_id,))
            assert submission["status"] == "approved"
            post = await db.fetchone("SELECT * FROM posts WHERE submission_id = ?", (submission_id,))
            assert "<b>Test video</b>" in post["caption"]
        finally:
            await db.close()

    asyncio.run(scenario())
