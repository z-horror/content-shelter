import asyncio
import types

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from app import db
from app.services import collections, publishing
from app.utils import now_iso

from tests.helpers import FakeBot, copy_method, seed_collection, seed_submission, submission_row


def test_publish_happy_path():
    async def scenario():
        await db.init()
        try:
            submission_id = await seed_submission(1)
            collection_id = await seed_collection(ordered=True)
            bot = FakeBot()

            result = await publishing.publish_submission(bot, submission_id, caption_text="Подпись", collection_id=collection_id)

            assert bot.copy_calls == 1
            assert result["message_id"] == 1001
            post = await db.fetchone("SELECT * FROM posts WHERE submission_id = ?", (submission_id,))
            assert post is not None
            assert post["episode"] == 1
            assert post["collection_id"] == collection_id
            submission = await submission_row(submission_id)
            assert submission["status"] == "approved"
            assert submission["publishing_at"] is None
            assert await db.fetchval("SELECT approved_total FROM users WHERE id = 1") == 1
            assert await db.fetchval("SELECT last_episode FROM collections WHERE id = ?", (collection_id,)) == 1
        finally:
            await db.close()

    asyncio.run(scenario())


def test_publish_blocked_when_claimed():
    async def scenario():
        await db.init()
        try:
            submission_id = await seed_submission(1)
            await db.execute(
                "UPDATE submissions SET publishing_at = ? WHERE id = ?", (now_iso(), submission_id)
            )
            bot = FakeBot()
            with pytest.raises(publishing.PublishBusy):
                await publishing.publish_submission(bot, submission_id)
            assert bot.copy_calls == 0
        finally:
            await db.close()

    asyncio.run(scenario())


def test_concurrent_publish_only_once():
    async def scenario():
        await db.init()
        try:
            submission_id = await seed_submission(1)
            bot = FakeBot(delay=0.05)
            results = await asyncio.gather(
                publishing.publish_submission(bot, submission_id),
                publishing.publish_submission(bot, submission_id),
                return_exceptions=True,
            )
            successes = [item for item in results if isinstance(item, dict)]
            failures = [item for item in results if isinstance(item, publishing.PublishBusy)]
            assert len(successes) == 1
            assert len(failures) == 1
            assert bot.copy_calls == 1
            assert await db.fetchval("SELECT COUNT(*) FROM posts") == 1
        finally:
            await db.close()

    asyncio.run(scenario())


def test_second_publish_after_success_is_rejected():
    async def scenario():
        await db.init()
        try:
            submission_id = await seed_submission(1)
            bot = FakeBot()
            await publishing.publish_submission(bot, submission_id)
            with pytest.raises(RuntimeError):
                await publishing.publish_submission(bot, submission_id)
            assert bot.copy_calls == 1
        finally:
            await db.close()

    asyncio.run(scenario())


def test_definitive_telegram_error_releases_claim():
    async def scenario():
        await db.init()
        try:
            submission_id = await seed_submission(1)
            error = TelegramBadRequest(copy_method(), "Bad Request: chat not found")
            bot = FakeBot(copy_errors=[error])
            with pytest.raises(TelegramBadRequest):
                await publishing.publish_submission(bot, submission_id)
            submission = await submission_row(submission_id)
            assert submission["publishing_at"] is None
            assert submission["status"] == "pending"
            assert await db.fetchval("SELECT COUNT(*) FROM posts") == 0
        finally:
            await db.close()

    asyncio.run(scenario())


def test_ambiguous_error_keeps_claim_and_stops_retries():
    async def scenario():
        await db.init()
        try:
            submission_id = await seed_submission(1)
            bot = FakeBot(copy_errors=[asyncio.TimeoutError()])
            with pytest.raises(publishing.PublishAmbiguous):
                await publishing.publish_submission(bot, submission_id)
            submission = await submission_row(submission_id)
            assert submission["publishing_at"] is not None
            assert submission["status"] == "pending"
            assert await db.fetchval("SELECT COUNT(*) FROM posts") == 0
            assert any("результат публикации неизвестен" in text for _, text in bot.messages)

            with pytest.raises(publishing.PublishBusy):
                await publishing.publish_submission(bot, submission_id)
            assert bot.copy_calls == 1
        finally:
            await db.close()

    asyncio.run(scenario())


def test_retry_after_is_retried():
    async def scenario():
        await db.init()
        try:
            submission_id = await seed_submission(1)
            error = TelegramRetryAfter(copy_method(), "Too Many Requests", 1)
            bot = FakeBot(copy_errors=[error, None])
            result = await publishing.publish_submission(bot, submission_id)
            assert result["message_id"] == 1002
            assert bot.copy_calls == 2
        finally:
            await db.close()

    asyncio.run(scenario())


def test_duplicate_episode_cleans_channel_message():
    async def scenario():
        await db.init()
        try:
            collection_id = await seed_collection(ordered=True)
            bot = FakeBot()
            await publishing.add_existing_post(
                bot, 500, "video", "file-id", "unique", "cap",
                collection_id=collection_id, season=1, episode=1,
            )
            submission_id = await seed_submission(1)
            with pytest.raises(RuntimeError, match="серия уже занята"):
                await publishing.publish_submission(
                    bot, submission_id, collection_id=collection_id, season=1, episode=1
                )
            submission = await submission_row(submission_id)
            assert submission["publishing_at"] is None
            assert submission["status"] == "pending"
            assert len(bot.deleted) == 1
            assert await db.fetchval("SELECT COUNT(*) FROM posts") == 1
            assert await db.fetchval("SELECT approved_total FROM users WHERE id = 1") == 0
        finally:
            await db.close()

    asyncio.run(scenario())
