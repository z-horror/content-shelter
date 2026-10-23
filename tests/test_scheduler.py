import asyncio

from app import db
from app.scheduler import _recover
from app.utils import now_iso

from tests.helpers import FakeBot, seed_submission


async def _seed_queue(submission_id, status="processing"):
    await db.execute(
        "INSERT INTO queue (submission_id, publish_at, status, created_at) VALUES (?, ?, ?, ?)",
        (submission_id, now_iso(), status, now_iso()),
    )


def test_recovery_marks_done_and_unknown_without_requeue():
    async def scenario():
        await db.init()
        try:
            done_id = await seed_submission(1)
            unknown_id = await seed_submission(2)
            await _seed_queue(done_id)
            await _seed_queue(unknown_id)
            await db.execute(
                "INSERT INTO posts (channel_id, message_id, submission_id, media_type, published_at) "
                "VALUES (-1001111111111, 777, ?, 'video', ?)",
                (done_id, now_iso()),
            )

            bot = FakeBot()
            await _recover(bot)

            statuses = {
                row["submission_id"]: row["status"]
                for row in await db.fetchall("SELECT * FROM queue ORDER BY id")
            }
            assert statuses[done_id] == "done"
            assert statuses[unknown_id] == "unknown"
            assert await db.fetchval(
                "SELECT COUNT(*) FROM queue WHERE status = 'waiting'"
            ) == 0
            assert any("незавершённые публикации" in text for _, text in bot.messages)
        finally:
            await db.close()

    asyncio.run(scenario())
