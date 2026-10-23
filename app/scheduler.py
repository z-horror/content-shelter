import asyncio
import logging

from aiogram import Bot
from app import config, db
from app.services import collections as collections_service
from app.services import publishing
from app.utils import format_local, now_iso

log = logging.getLogger(__name__)


async def _notify_admins(bot: Bot, text: str):
    for admin_id in config.ADMIN_USER_IDS:
        try:
            await bot.send_message(admin_id, text, parse_mode=None)
        except Exception:  # noqa: BLE001
            log.warning("cannot notify admin %s", admin_id)


async def _reconcile_published(submission_id: int, post) -> bool:
    """Пост уже в базе: приводим заявку к approved и снимаем резерв.

    Счётчик и номер серии увеличиваем только если заявка ещё не approved:
    при старом порядке записи (пост → статус → счётчик) это доказуемо
    означает, что счётчик не был увеличен.
    """
    submission = await db.fetchone("SELECT * FROM submissions WHERE id = ?", (submission_id,))
    if submission is None:
        return False

    async with db.transaction():
        if submission["status"] != "approved":
            await db.execute(
                "UPDATE submissions SET status = 'approved', publishing_at = NULL, "
                "reviewed_at = COALESCE(reviewed_at, ?) WHERE id = ?",
                (now_iso(), submission_id),
            )
            await db.execute(
                "UPDATE users SET approved_total = approved_total + 1 WHERE id = ?",
                (submission["user_id"],),
            )
            if post["collection_id"] and post["episode"]:
                await collections_service.bump_episode(post["collection_id"], post["episode"])
            log.warning(
                "reconciled submission %s as approved: post %s exists, counters adjusted",
                submission_id, post["message_id"],
            )
        else:
            await db.execute(
                "UPDATE submissions SET publishing_at = NULL WHERE id = ?", (submission_id,)
            )
    return True


async def _recover(bot: Bot):

    rows = await db.fetchall("SELECT * FROM queue WHERE status = 'processing'")
    unknown = []
    for row in rows:
        post = await db.fetchone(
            "SELECT * FROM posts WHERE submission_id = ? ORDER BY id LIMIT 1",
            (row["submission_id"],),
        )
        if post is not None:
            await _reconcile_published(row["submission_id"], post)
            await db.execute(
                "UPDATE queue SET status = 'done', error = NULL WHERE id = ?", (row["id"],)
            )
            log.info("queue %s recovered as done (post exists)", row["id"])
        else:
            await db.execute(
                "UPDATE queue SET status = 'unknown', error = ? WHERE id = ?",
                ("прервано во время публикации; результат неизвестен", row["id"]),
            )
            unknown.append(row)
            log.error(
                "queue %s recovered as unknown: submission=%s scheduled=%s",
                row["id"], row["submission_id"], row["publish_at"],
            )

    claims = await db.fetchall(
        "SELECT id, publishing_at FROM submissions WHERE publishing_at IS NOT NULL"
    )
    for claim in claims:
        post = await db.fetchone(
            "SELECT * FROM posts WHERE submission_id = ? ORDER BY id LIMIT 1", (claim["id"],)
        )
        if post is not None:
            await _reconcile_published(claim["id"], post)
            log.info("publish claim for submission %s cleared: post exists", claim["id"])
        else:
            log.warning(
                "stale publish claim: submission=%s since=%s",
                claim["id"], claim["publishing_at"],
            )

    if unknown:
        ids = ", ".join(f"#{row['submission_id']}" for row in unknown)
        await _notify_admins(
            bot,
            f"⚠️ После перезапуска найдены незавершённые публикации: {ids}.\n"
            "Результат неизвестен, автоповтор остановлен. Проверь канал и очередь вручную.",
        )


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
    except (publishing.PublishBusy, publishing.PublishAmbiguous) as error:
        log.error(
            "scheduled publish stopped for queue %s: %s", row["id"], type(error).__name__
        )
        await db.execute(
            "UPDATE queue SET status = 'unknown', error = ? WHERE id = ?",
            (str(error)[:500], row["id"]),
        )
        await _notify_admins(
            bot,
            f"⚠️ Заявка #{row['submission_id']}: публикация не завершена "
            f"({type(error).__name__}). Автоповтор остановлен, проверь канал.",
        )
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
        await _recover(bot)
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
