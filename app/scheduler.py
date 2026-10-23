import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from app import config, db
from app.services import publishing
from app.utils import format_local, now_iso

log = logging.getLogger(__name__)


async def _notify_admins(bot: Bot, text: str):
    for admin_id in config.ADMIN_USER_IDS:
        try:
            await bot.send_message(admin_id, text, parse_mode=None)
        except (TelegramForbiddenError, TelegramBadRequest):
            pass


async def _process(row, bot: Bot):
    try:
        await publishing.publish_submission(
            bot,
            submission_id=row["submission_id"],
            caption_text=row["caption"],
            collection_id=row["collection_id"],
            season=row["season"],
            episode=row["episode"],
        )
        await db.execute("UPDATE queue SET status = 'done' WHERE id = ?", (row["id"],))
        log.info("scheduled submission %s published", row["submission_id"])
    except Exception as error:  # noqa: BLE001
        log.exception("scheduled publish failed for queue %s", row["id"])
        attempts = int(row["attempts"] or 0) + 1
        status = "failed" if attempts >= 3 else "waiting"
        await db.execute(
            "UPDATE queue SET status = ?, attempts = ?, error = ? WHERE id = ?",
            (status, attempts, str(error)[:500], row["id"]),
        )
        if status == "failed":
            await db.execute(
                "UPDATE submissions SET status = 'pending' WHERE id = ? AND status = 'scheduled'",
                (row["submission_id"],),
            )
            await _notify_admins(
                bot,
                f"⚠️ Не удалось опубликовать заявку #{row['submission_id']} "
                f"(запланировано на {format_local(row['publish_at'])}):\n{error}",
            )


async def scheduler_loop(bot: Bot):
    try:
        await db.execute(
            "UPDATE queue SET status = 'done' WHERE status = 'processing' AND submission_id IN "
            "(SELECT id FROM submissions WHERE status = 'approved')"
        )
        await db.execute("UPDATE queue SET status = 'waiting' WHERE status = 'processing'")
    except Exception:  # noqa: BLE001
        log.exception("queue recovery failed")
    await asyncio.sleep(5)
    while True:
        try:
            rows = await db.fetchall(
                "SELECT * FROM queue WHERE status = 'waiting' AND publish_at <= ? "
                "ORDER BY publish_at LIMIT 5",
                (now_iso(),),
            )
            for row in rows:
                cur = await db.execute(
                    "UPDATE queue SET status = 'processing' WHERE id = ? AND status = 'waiting'",
                    (row["id"],),
                )
                if cur.rowcount == 0:
                    continue
                await _process(row, bot)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("scheduler loop error")
        await asyncio.sleep(15)
