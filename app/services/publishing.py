import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app import config, db
from app.services import collections as collections_service
from app.utils import esc, format_local, now_iso, post_link

log = logging.getLogger(__name__)

AMBIGUOUS_ERRORS = (asyncio.TimeoutError, TimeoutError, TelegramNetworkError, TelegramServerError)


class PublishBusy(RuntimeError):
    """Заявка уже обрабатывается другим обработчиком."""


class PublishAmbiguous(RuntimeError):
    """Неизвестно, создано ли сообщение в Telegram. Автоповтор запрещён."""


async def _notify_admins(bot: Bot, text: str):
    for admin_id in config.ADMIN_USER_IDS:
        try:
            await bot.send_message(admin_id, text, parse_mode=None)
        except Exception:  # noqa: BLE001
            log.warning("cannot notify admin %s", admin_id)


async def _claim(submission_id: int) -> bool:
    cur = await db.execute(
        "UPDATE submissions SET publishing_at = ? WHERE id = ? AND publishing_at IS NULL "
        "AND status IN ('pending', 'scheduled')",
        (now_iso(), submission_id),
    )
    return cur.rowcount == 1


async def _release(submission_id: int):
    await db.execute("UPDATE submissions SET publishing_at = NULL WHERE id = ?", (submission_id,))


async def _copy_with_retry(bot: Bot, **kwargs):
    """TelegramRetryAfter означает, что запрос точно не выполнен: повтор безопасен."""
    attempts = 0
    while True:
        try:
            return await bot.copy_message(**kwargs)
        except TelegramRetryAfter as error:
            attempts += 1
            if attempts > 2:
                raise
            delay = max(1, min(30, int(getattr(error, "retry_after", 1) or 1)))
            log.warning("telegram rate limit, retrying in %ss", delay)
            await asyncio.sleep(delay)


def post_keyboard(prev_link=None, next_link=None, random_enabled=False) -> InlineKeyboardMarkup:
    username = config.BOT_USERNAME or "bot"
    rows = [
        [
            InlineKeyboardButton(text="📤 Предложить видео", url=f"https://t.me/{username}?start=submit"),
            InlineKeyboardButton(text="📚 Подборки", url=f"https://t.me/{username}?start=collections"),
        ]
    ]
    if prev_link or next_link:
        row = []
        if prev_link:
            row.append(InlineKeyboardButton(text="⬅️ Предыдущая", url=prev_link))
        if next_link:
            row.append(InlineKeyboardButton(text="Следующая ➡️", url=next_link))
        rows.append(row)
    if random_enabled:
        rows.append([InlineKeyboardButton(text="🎲 Случайный пост", url=f"https://t.me/{username}?start=random")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def card_keyboard(submission_id: int, author_id=None) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"mod:pub:{submission_id}"),
            InlineKeyboardButton(text="📁 В подборку", callback_data=f"mod:col:{submission_id}"),
        ],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"mod:rej:{submission_id}")],
    ]
    if author_id is not None and not config.is_admin(author_id):
        rows.append([InlineKeyboardButton(text="🚫 Заблокировать", callback_data=f"mod:ban:{submission_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def random_keyboard() -> InlineKeyboardMarkup:
    username = config.BOT_USERNAME or "bot"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📚 Подборки", url=f"https://t.me/{username}?start=collections"),
                InlineKeyboardButton(text="🎲 Ещё", callback_data="u:random"),
            ]
        ]
    )


def build_caption(text, collection, season, episode) -> str:
    raw = (text or "").strip()
    if len(raw) > 900:
        raw = raw[:897] + "..."
    text = esc(raw)
    tag = ""
    if collection is not None:
        tag = f"#{collection['slug']}"
        if collection["is_ordered"] and episode:
            tag += f" · S{int(season or collection['season'])}E{int(episode)}"
    if tag and text and tag.split(" ")[0] in text:
        return text
    if text and tag:
        return f"{text}\n\n{tag}"
    return text or tag


async def neighbour_posts(post_row):
    """Ближайшие существующие серии (пропускает удалённые и дырки)."""
    if not post_row or not post_row["collection_id"] or not post_row["episode"]:
        return None, None
    previous = await db.fetchone(
        "SELECT * FROM posts WHERE collection_id = ? AND season = ? AND episode < ? "
        "AND is_deleted = 0 ORDER BY episode DESC LIMIT 1",
        (post_row["collection_id"], post_row["season"], post_row["episode"]),
    )
    following = await db.fetchone(
        "SELECT * FROM posts WHERE collection_id = ? AND season = ? AND episode > ? "
        "AND is_deleted = 0 ORDER BY episode ASC LIMIT 1",
        (post_row["collection_id"], post_row["season"], post_row["episode"]),
    )
    return previous, following


async def refresh_keyboard(bot: Bot, post_row):
    if not post_row or not post_row["collection_id"] or not post_row["episode"]:
        return
    previous, following = await neighbour_posts(post_row)
    prev_link = post_link(previous["message_id"]) if previous else None
    next_link = post_link(following["message_id"]) if following else None
    random_enabled = await db.get_bool_setting("random_button_under_posts")
    try:
        await bot.edit_message_reply_markup(
            chat_id=post_row["channel_id"],
            message_id=post_row["message_id"],
            reply_markup=post_keyboard(prev_link, next_link, random_enabled),
        )
    except TelegramBadRequest as error:
        log.warning("cannot refresh keyboard for post %s: %s", post_row["message_id"], error)


async def edit_card(bot: Bot, submission, status_line: str, remove_buttons=True, keyboard=None):
    if not submission["admin_chat_id"] or not submission["admin_message_id"]:
        return
    base = submission["card_caption"] or f"📥 <b>Заявка #{submission['id']}</b>"
    caption = f"{base}\n\n{status_line}"
    if keyboard is not None:
        markup = keyboard
    elif remove_buttons:
        markup = InlineKeyboardMarkup(inline_keyboard=[])
    else:
        markup = None
    try:
        await bot.edit_message_caption(
            chat_id=submission["admin_chat_id"],
            message_id=submission["admin_message_id"],
            caption=caption[:1024],
            parse_mode="HTML",
            reply_markup=markup,
        )
    except TelegramBadRequest:
        try:
            await bot.edit_message_text(
                chat_id=submission["admin_chat_id"],
                message_id=submission["admin_message_id"],
                text=caption[:4096],
                parse_mode="HTML",
                reply_markup=markup,
            )
        except TelegramBadRequest as error:
            log.warning("cannot edit card %s: %s", submission["id"], error)


async def publish_submission(
    bot: Bot,
    submission_id: int,
    caption_text=None,
    collection_id=None,
    season=None,
    episode=None,
    reviewer_id=None,
    notify=True,
) -> dict:
    submission = await db.fetchone("SELECT * FROM submissions WHERE id = ?", (submission_id,))
    if submission is None:
        raise RuntimeError("заявка не найдена")
    if not submission["source_chat_id"] or not submission["source_message_id"]:
        raise RuntimeError("у заявки нет исходного файла, пришли его заново")
    if submission["status"] not in ("pending", "scheduled"):
        raise RuntimeError("заявка уже обработана")
    if submission["publishing_at"]:
        raise PublishBusy(f"заявка уже публикуется с {submission['publishing_at']}")
    if not await _claim(submission_id):
        raise PublishBusy("не удалось зарезервировать заявку")

    try:
        return await _publish_claimed(bot, submission, submission_id, caption_text,
                                      collection_id, season, episode, reviewer_id, notify)
    except (PublishBusy, PublishAmbiguous):
        raise
    except Exception:
        await _release(submission_id)
        raise


async def _publish_claimed(bot: Bot, submission, submission_id: int, caption_text,
                           collection_id, season, episode, reviewer_id, notify) -> dict:
    collection = None
    if collection_id:
        collection = await db.fetchone("SELECT * FROM collections WHERE id = ?", (collection_id,))
        if collection is None:
            raise RuntimeError("подборка не найдена")
        if collection["is_ordered"]:
            season = int(season or collection["season"])
            if episode is None:
                episode = await collections_service.next_episode(collection_id, season)
        else:
            season = None
            episode = None

    caption = build_caption(caption_text, collection, season, episode)
    prev_link = next_link = None
    if collection_id and episode:
        previous = await db.fetchone(
            "SELECT message_id FROM posts WHERE collection_id = ? AND season = ? AND episode < ? "
            "AND is_deleted = 0 ORDER BY episode DESC LIMIT 1",
            (collection_id, season, episode),
        )
        following = await db.fetchone(
            "SELECT message_id FROM posts WHERE collection_id = ? AND season = ? AND episode > ? "
            "AND is_deleted = 0 ORDER BY episode ASC LIMIT 1",
            (collection_id, season, episode),
        )
        prev_link = post_link(previous["message_id"]) if previous else None
        next_link = post_link(following["message_id"]) if following else None
    random_enabled = await db.get_bool_setting("random_button_under_posts")

    channel_id = config.CHANNEL_NUMERIC_ID if config.CHANNEL_NUMERIC_ID is not None else config.CHANNEL_CHAT
    try:
        message = await _copy_with_retry(
            bot,
            chat_id=config.CHANNEL_CHAT,
            from_chat_id=submission["source_chat_id"],
            message_id=submission["source_message_id"],
            caption=caption or None,
            parse_mode="HTML",
            reply_markup=post_keyboard(prev_link, next_link, random_enabled),
        )
    except AMBIGUOUS_ERRORS as error:
        log.critical(
            "publication result unknown: submission=%s error=%s", submission_id, type(error).__name__
        )
        await _notify_admins(
            bot,
            f"⚠️ Заявка #{submission_id}: результат публикации неизвестен "
            f"({type(error).__name__}). Автоповтор остановлен, проверь канал.\n"
            f"Снять блокировку: UPDATE submissions SET publishing_at=NULL WHERE id={submission_id};",
        )
        raise PublishAmbiguous("результат отправки в Telegram неизвестен") from error

    try:
        async with db.transaction():
            cur = await db.execute(
                "INSERT INTO posts (channel_id, message_id, submission_id, collection_id, season, episode, "
                "media_type, file_id, file_unique_id, caption, author_id, published_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    channel_id,
                    message.message_id,
                    submission_id,
                    collection_id,
                    season,
                    episode,
                    submission["media_type"],
                    submission["file_id"],
                    submission["file_unique_id"],
                    caption,
                    submission["user_id"],
                    now_iso(),
                ),
            )
            post_id = cur.lastrowid
            await db.execute(
                "UPDATE submissions SET status = 'approved', publishing_at = NULL, "
                "reviewed_at = ?, reviewer_id = ? WHERE id = ?",
                (now_iso(), reviewer_id, submission_id),
            )
            await db.execute(
                "UPDATE users SET approved_total = approved_total + 1 WHERE id = ?",
                (submission["user_id"],),
            )
            if collection_id and episode:
                await collections_service.bump_episode(collection_id, episode)
    except Exception as error:
        deleted = False
        try:
            await bot.delete_message(channel_id, message.message_id)
            deleted = True
        except Exception:  # noqa: BLE001
            log.exception("cannot remove orphan channel message %s", message.message_id)
        if deleted:
            await _release(submission_id)
            raise RuntimeError(
                "не удалось сохранить пост: возможно, серия уже занята. Попробуй другой номер."
            ) from error
        log.critical(
            "publication saved in Telegram but not in DB: submission=%s message=%s",
            submission_id, message.message_id,
        )
        await _notify_admins(
            bot,
            f"⚠️ Заявка #{submission_id}: пост создан в канале (message_id={message.message_id}), "
            f"но запись в БД не сохранена. Автоповтор остановлен.",
        )
        raise PublishAmbiguous("пост создан, но запись в БД не сохранена") from error

    current = await db.fetchone("SELECT * FROM posts WHERE id = ?", (post_id,))
    await refresh_keyboard(bot, current)
    if collection_id and episode:
        previous, following = await neighbour_posts(current)
        await refresh_keyboard(bot, previous)
        await refresh_keyboard(bot, following)

    link = post_link(message.message_id)
    submission = await db.fetchone("SELECT * FROM submissions WHERE id = ?", (submission_id,))
    await edit_card(bot, submission, f"✅ Опубликовано: {link}")

    if notify:
        text = "✅ Твоё видео опубликовано!"
        if collection is not None:
            text += f"\nПодборка: {esc(collection['name'])}"
        if link:
            text += f"\n{link}"
        try:
            await bot.send_message(submission["user_id"], text, parse_mode="HTML")
        except TelegramForbiddenError:
            pass
        except TelegramBadRequest as error:
            log.warning("cannot notify author %s: %s", submission["user_id"], error)

    return {"post_id": post_id, "message_id": message.message_id, "link": link}


async def enqueue(bot: Bot, submission_id: int, publish_at: str, caption_text=None,
                  collection_id=None, season=None, episode=None, reviewer_id=None):
    submission = await db.fetchone("SELECT * FROM submissions WHERE id = ?", (submission_id,))
    if submission is None:
        raise RuntimeError("заявка не найдена")

    async with db.transaction():
        await db.execute(
            "INSERT INTO queue (submission_id, collection_id, season, episode, caption, publish_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (submission_id, collection_id, season, episode, caption_text, publish_at, now_iso()),
        )
        await db.execute(
            "UPDATE submissions SET status = 'scheduled', reviewed_at = ?, reviewer_id = ? WHERE id = ?",
            (now_iso(), reviewer_id, submission_id),
        )
    await edit_card(bot, submission, f"🕓 Запланировано на {format_local(publish_at)}")

    try:
        await bot.send_message(
            submission["user_id"],
            f"🕓 Заявка #{submission_id} принята, опубликуем {format_local(publish_at)}.",
        )
    except (TelegramForbiddenError, TelegramBadRequest):
        pass


async def add_existing_post(
    bot: Bot,
    channel_message_id: int,
    media_type: str,
    file_id,
    file_unique_id,
    caption,
    collection_id=None,
    season=None,
    episode=None,
) -> int:
    channel_id = config.CHANNEL_NUMERIC_ID if config.CHANNEL_NUMERIC_ID is not None else config.CHANNEL_CHAT
    async with db.transaction():
        cur = await db.execute(
            "INSERT INTO posts (channel_id, message_id, submission_id, collection_id, season, episode, "
            "media_type, file_id, file_unique_id, caption, author_id, published_at) "
            "VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
            (channel_id, channel_message_id, collection_id, season, episode,
             media_type, file_id, file_unique_id, caption, now_iso()),
        )
        post_id = cur.lastrowid
        if collection_id and episode:
            await collections_service.bump_episode(collection_id, episode)

    current = await db.fetchone("SELECT * FROM posts WHERE id = ?", (post_id,))
    previous, following = await neighbour_posts(current)
    await refresh_keyboard(bot, previous)
    await refresh_keyboard(bot, following)
    return post_id


async def rebuild_all_keyboards(bot: Bot):
    """Пересобрать клавиатуры у всех постов (после смены username бота)."""
    rows = await db.fetchall("SELECT * FROM posts ORDER BY id")
    random_enabled = await db.get_bool_setting("random_button_under_posts")
    updated = 0
    failed = 0
    for post in rows:
        prev_link = next_link = None
        if post["collection_id"] and post["episode"]:
            previous, following = await neighbour_posts(post)
            prev_link = post_link(previous["message_id"]) if previous else None
            next_link = post_link(following["message_id"]) if following else None
        try:
            await bot.edit_message_reply_markup(
                chat_id=post["channel_id"],
                message_id=post["message_id"],
                reply_markup=post_keyboard(prev_link, next_link, random_enabled),
            )
            updated += 1
        except TelegramBadRequest:
            failed += 1
        await asyncio.sleep(0.05)
    return updated, failed
