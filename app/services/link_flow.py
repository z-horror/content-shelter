import json
import logging
from typing import Awaitable, Callable

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile

from app import config, db
from app.services import link_download
from app.services.publishing import card_keyboard
from app.utils import esc, human_duration, human_size, now_iso

log = logging.getLogger(__name__)


class LinkDuplicate(Exception):
    def __init__(self, row):
        super().__init__("duplicate link")
        self.row = row


class LinkCancelled(Exception):
    pass


class LinkUploadFailed(link_download.LinkError):
    code = "upload_failed"


async def find_duplicate(normalized_url: str):
    return await db.fetchone(
        "SELECT s.*, "
        "(SELECT p.message_id FROM posts p WHERE p.submission_id = s.id ORDER BY p.id DESC LIMIT 1) "
        "AS post_message_id, "
        "(SELECT q.publish_at FROM queue q WHERE q.submission_id = s.id AND q.status = 'waiting' "
        "ORDER BY q.id DESC LIMIT 1) AS publish_at "
        "FROM submissions s "
        "WHERE s.source_url = ? AND s.status IN ('pending', 'scheduled', 'approved') "
        "ORDER BY s.id DESC LIMIT 1",
        (normalized_url,),
    )


async def _notify_admins(bot: Bot, user_id: int, url: str, reason: str):
    text = (
        "⚠️ Не удалось скачать видео по ссылке\n"
        f"{url}\n"
        f"Пользователь: {user_id}\n"
        f"Причина: {reason}"
    )
    for admin_id in config.ADMIN_USER_IDS:
        try:
            await bot.send_message(admin_id, text, parse_mode=None)
        except Exception:  # noqa: BLE001
            log.warning("cannot notify admin %s about link failure", admin_id)


def _card_text(submission_id: int, user, result: link_download.DownloadResult) -> str:
    author = esc(user.first_name)
    if user.username:
        author += f" · @{user.username}"
    author += f" · <code>{user.id}</code>"

    details = f"📦 видео · {human_size(result.size)}"
    if result.duration:
        details += f" · {human_duration(result.duration)}"

    lines = [f"📥 <b>Заявка #{submission_id}</b>", f"👤 {author}", details]
    meta = result.meta or {}
    if meta.get("title"):
        lines.append(f"🎬 {esc(meta['title'][:200])}")
    if meta.get("author"):
        lines.append(f"📺 {esc(meta['author'][:120])}")
    if meta.get("description"):
        lines.append(f"💬 {esc(meta['description'][:200])}")
    lines.append(f'🔗 <a href="{esc(result.url)}">источник</a>')
    return "\n".join(lines)


async def create_link_submission(bot: Bot, user, result: link_download.DownloadResult) -> int:
    await db.execute(
        "INSERT INTO users (id, username, first_name, created_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET username = excluded.username, first_name = excluded.first_name",
        (user.id, user.username, user.first_name, now_iso()),
    )

    meta_json = json.dumps(result.meta or {}, ensure_ascii=False)
    cur = await db.execute(
        "INSERT INTO submissions (user_id, source_chat_id, source_message_id, media_type, "
        "source_url, source_meta, status, created_at) "
        "VALUES (?, ?, 0, 'video', ?, ?, 'pending', ?)",
        (user.id, config.ADMIN_CHAT, result.url, meta_json, now_iso()),
    )
    submission_id = cur.lastrowid
    card = _card_text(submission_id, user, result)
    keyboard = card_keyboard(submission_id, user.id)

    try:
        try:
            sent = await bot.send_video(
                chat_id=config.ADMIN_CHAT,
                video=FSInputFile(result.path),
                caption=card,
                parse_mode="HTML",
                reply_markup=keyboard,
                supports_streaming=True,
                duration=result.duration,
                message_thread_id=config.ADMIN_TOPIC_ID,
                request_timeout=180,
            )
            file_id = sent.video.file_id
            file_unique_id = sent.video.file_unique_id
        except TelegramBadRequest:
            sent = await bot.send_document(
                chat_id=config.ADMIN_CHAT,
                document=FSInputFile(result.path),
                caption=card,
                parse_mode="HTML",
                reply_markup=keyboard,
                message_thread_id=config.ADMIN_TOPIC_ID,
                request_timeout=180,
            )
            file_id = sent.document.file_id
            file_unique_id = sent.document.file_unique_id
    except Exception as error:  # noqa: BLE001
        log.exception("cannot upload downloaded video to admin chat")
        await db.execute("DELETE FROM submissions WHERE id = ?", (submission_id,))
        raise LinkUploadFailed("upload failed") from error

    await db.execute(
        "UPDATE submissions SET source_message_id = ?, file_id = ?, file_unique_id = ?, "
        "admin_chat_id = ?, admin_message_id = ?, card_caption = ? WHERE id = ?",
        (sent.message_id, file_id, file_unique_id, config.ADMIN_CHAT, sent.message_id, card, submission_id),
    )
    await db.execute("UPDATE users SET submissions_total = submissions_total + 1 WHERE id = ?", (user.id,))
    return submission_id


async def download_and_submit(
    bot: Bot,
    user,
    raw_url: str,
    is_cancelled: Callable[[], Awaitable[bool]],
) -> int:
    normalized = link_download.normalize_url(raw_url)

    duplicate = await find_duplicate(normalized)
    if duplicate is not None:
        raise LinkDuplicate(duplicate)

    try:
        result = await link_download.download(normalized)
    except link_download.LinkError as error:
        await _notify_admins(bot, user.id, raw_url, error.code)
        raise
    except Exception as error:  # noqa: BLE001
        log.exception("unexpected link download error")
        await _notify_admins(bot, user.id, raw_url, "unexpected")
        raise link_download.DownloadFailed("unexpected") from error

    try:
        if await is_cancelled():
            raise LinkCancelled()
        try:
            return await create_link_submission(bot, user, result)
        except LinkCancelled:
            raise
        except link_download.LinkError as error:
            await _notify_admins(bot, user.id, raw_url, error.code)
            raise
        except Exception as error:  # noqa: BLE001
            log.exception("unexpected link submission error")
            await _notify_admins(bot, user.id, raw_url, "submission_failed")
            raise LinkUploadFailed("submission failed") from error
    finally:
        link_download.cleanup(result)
