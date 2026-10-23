import asyncio

from app import db
from app.scheduler import _recover
from app.utils import now_iso

from tests.helpers import FakeBot, seed_collection, seed_submission


async def _seed_queue(submission_id, status="processing"):
    await db.execute(
        "INSERT INTO queue (submission_id, publish_at, status, created_at) VALUES (?, ?, ?, ?)",
        (submission_id, now_iso(), status, now_iso()),
    )


async def _seed_post(submission_id, message_id):
    await db.execute(
        "INSERT INTO posts (channel_id, message_id, submission_id, media_type, published_at) "
        "VALUES (-1001111111111, ?, ?, 'video', ?)",
        (message_id, submission_id, now_iso()),
    )


def test_recovery_marks_done_and_reconciles_submission():
    async def scenario():
        await db.init()
        try:
            done_id = await seed_submission(1)
            unknown_id = await seed_submission(2)
            await _seed_queue(done_id)
            await _seed_queue(unknown_id)
            await _seed_post(done_id, 777)

            bot = FakeBot()
            await _recover(bot)

            statuses = {
                row["submission_id"]: row["status"]
                for row in await db.fetchall("SELECT * FROM queue ORDER BY id")
            }
            assert statuses[done_id] == "done"
            assert statuses[unknown_id] == "unknown"
            assert await db.fetchval("SELECT COUNT(*) FROM queue WHERE status = 'waiting'") == 0
            assert any("незавершённые публикации" in text for _, text in bot.messages)

            done = await db.fetchone("SELECT * FROM submissions WHERE id = ?", (done_id,))
            assert done["status"] == "approved"
            assert done["publishing_at"] is None
            assert await db.fetchval("SELECT approved_total FROM users WHERE id = 1") == 1

            pending = await db.fetchone("SELECT * FROM submissions WHERE id = ?", (unknown_id,))
            assert pending["status"] == "pending"
            assert pending["publishing_at"] is None
        finally:
            await db.close()

    asyncio.run(scenario())


def test_recovery_clears_claim_when_post_exists():
    async def scenario():
        await db.init()
        try:
            submission_id = await seed_submission(1)
            await db.execute(
                "UPDATE submissions SET publishing_at = ? WHERE id = ?", (now_iso(), submission_id)
            )
            await _seed_post(submission_id, 888)

            await _recover(FakeBot())

            submission = await db.fetchone("SELECT * FROM submissions WHERE id = ?", (submission_id,))
            assert submission["status"] == "approved"
            assert submission["publishing_at"] is None
            assert await db.fetchval("SELECT approved_total FROM users WHERE id = 1") == 1
        finally:
            await db.close()

    asyncio.run(scenario())


def test_recovery_keeps_claim_when_result_unknown():
    async def scenario():
        await db.init()
        try:
            submission_id = await seed_submission(1, status="scheduled")
            await db.execute(
                "UPDATE submissions SET publishing_at = ? WHERE id = ?", (now_iso(), submission_id)
            )
            await _seed_queue(submission_id)

            bot = FakeBot()
            await _recover(bot)

            submission = await db.fetchone("SELECT * FROM submissions WHERE id = ?", (submission_id,))
            assert submission["status"] == "scheduled"
            assert submission["publishing_at"] is not None
            queue_row = await db.fetchone("SELECT * FROM queue WHERE submission_id = ?", (submission_id,))
            assert queue_row["status"] == "unknown"
            assert await db.fetchval("SELECT COUNT(*) FROM posts") == 0
        finally:
            await db.close()

    asyncio.run(scenario())


def test_recovery_is_idempotent():
    async def scenario():
        await db.init()
        try:
            submission_id = await seed_submission(1)
            collection_id = await seed_collection(ordered=True)
            await db.execute(
                "INSERT INTO posts (channel_id, message_id, submission_id, collection_id, season, episode, "
                "media_type, published_at) VALUES (-1001111111111, 901, ?, ?, 1, 3, 'video', ?)",
                (submission_id, collection_id, now_iso()),
            )
            await _seed_queue(submission_id)

            await _recover(FakeBot())

            submission = await db.fetchone("SELECT * FROM submissions WHERE id = ?", (submission_id,))
            assert submission["status"] == "approved"
            assert submission["publishing_at"] is None
            assert await db.fetchval("SELECT approved_total FROM users WHERE id = 1") == 1
            assert await db.fetchval("SELECT last_episode FROM collections WHERE id = ?", (collection_id,)) == 3

            await _recover(FakeBot())

            assert await db.fetchval("SELECT approved_total FROM users WHERE id = 1") == 1
            assert await db.fetchval("SELECT last_episode FROM collections WHERE id = ?", (collection_id,)) == 3
            assert await db.fetchval("SELECT COUNT(*) FROM posts") == 1
        finally:
            await db.close()

    asyncio.run(scenario())


def test_recovery_does_not_recount_approved_submission():
    async def scenario():
        await db.init()
        try:
            submission_id = await seed_submission(1, status="approved")
            await db.execute("UPDATE users SET approved_total = 1 WHERE id = 1")
            await db.execute(
                "UPDATE submissions SET publishing_at = ? WHERE id = ?", (now_iso(), submission_id)
            )
            await _seed_post(submission_id, 902)

            await _recover(FakeBot())

            submission = await db.fetchone("SELECT * FROM submissions WHERE id = ?", (submission_id,))
            assert submission["status"] == "approved"
            assert submission["publishing_at"] is None
            assert await db.fetchval("SELECT approved_total FROM users WHERE id = 1") == 1
        finally:
            await db.close()

    asyncio.run(scenario())


def test_recovery_counts_multiple_posts_once():
    async def scenario():
        await db.init()
        try:
            submission_id = await seed_submission(1)
            await db.execute(
                "UPDATE submissions SET publishing_at = ? WHERE id = ?", (now_iso(), submission_id)
            )
            await _seed_post(submission_id, 903)
            await _seed_post(submission_id, 904)

            await _recover(FakeBot())

            submission = await db.fetchone("SELECT * FROM submissions WHERE id = ?", (submission_id,))
            assert submission["status"] == "approved"
            assert submission["publishing_at"] is None
            assert await db.fetchval("SELECT approved_total FROM users WHERE id = 1") == 1
            assert await db.fetchval("SELECT COUNT(*) FROM posts") == 2
        finally:
            await db.close()

    asyncio.run(scenario())
