import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app import config, db
from app.utils import esc, format_local, now_iso, post_link

log = logging.getLogger(__name__)


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


def card_keyboard(submission_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"mod:pub:{submission_id}"),
                InlineKeyboardButton(text="📁 В подборку", callback_data=f"mod:col:{submission_id}"),
            ],
            [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"mod:rej:{submission_id}")],
        ]
    )


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


async def _next_episode(collection_id, season):
    current = await db.fetchval(
        "SELECT MAX(episode) FROM posts WHERE collection_id = ? AND season = ?",
        (collection_id, season),
        0,
    )
    return int(current or 0) + 1


async def _neighbour_link(collection_id, season, episode):
    if not collection_id or not episode or episode < 1:
        return None
    row = await db.fetchone(
        "SELECT message_id FROM posts WHERE collection_id = ? AND season = ? "
        "AND episode = ? AND is_deleted = 0",
        (collection_id, season, episode),
    )
    return post_link(row["message_id"]) if row else None


async def refresh_keyboard(bot: Bot, post_row):
    if not post_row or not post_row["collection_id"] or not post_row["episode"]:
        return
    prev_link = await _neighbour_link(post_row["collection_id"], post_row["season"], post_row["episode"] - 1)
    next_link = await _neighbour_link(post_row["collection_id"], post_row["season"], post_row["episode"] + 1)
    random_enabled = await db.get_bool_setting("random_button_under_posts")
    try:
        await bot.edit_message_reply_markup(
            chat_id=post_row["channel_id"],
            message_id=post_row["message_id"],
            reply_markup=post_keyboard(prev_link, next_link, random_enabled),
        )
    except TelegramBadRequest as error:
        log.warning("cannot refresh keyboard for post %s: %s", post_row["message_id"], error)


async def edit_card(bot: Bot, submission, status_line: str, remove_buttons=True):
    if not submission["admin_chat_id"] or not submission["admin_message_id"]:
        return
    base = submission["card_caption"] or f"📥 <b>Заявка #{submission['id']}</b>"
    caption = f"{base}\n\n{status_line}"
    markup = InlineKeyboardMarkup(inline_keyboard=[]) if remove_buttons else None
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

    collection = None
    if collection_id:
        collection = await db.fetchone("SELECT * FROM collections WHERE id = ?", (collection_id,))
        if collection is None:
            raise RuntimeError("подборка не найдена")
        if collection["is_ordered"]:
            season = int(season or collection["season"])
            if episode is None:
                episode = await _next_episode(collection_id, season)
        else:
            season = None
            episode = None

    caption = build_caption(caption_text, collection, season, episode)
    prev_link = await _neighbour_link(collection_id, season, episode - 1) if (collection_id and episode) else None
    random_enabled = await db.get_bool_setting("random_button_under_posts")

    message = await bot.copy_message(
        chat_id=config.CHANNEL_CHAT,
        from_chat_id=submission["source_chat_id"],
        message_id=submission["source_message_id"],
        caption=caption or None,
        parse_mode="HTML",
        reply_markup=post_keyboard(prev_link, None, random_enabled),
    )

    channel_id = config.CHANNEL_NUMERIC_ID if config.CHANNEL_NUMERIC_ID is not None else config.CHANNEL_CHAT
    try:
        cur = await db.execute(
            "INSERT INTO posts (channel_id, message_id, submission_id, collection_id, season, episode, "
            "media_type, file_id, caption, author_id, published_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                channel_id,
                message.message_id,
                submission_id,
                collection_id,
                season,
                episode,
                submission["media_type"],
                submission["file_id"],
                caption,
                submission["user_id"],
                now_iso(),
            ),
        )
    except Exception:
        try:
            await bot.delete_message(channel_id, message.message_id)
        except TelegramBadRequest:
            pass
        raise RuntimeError("не удалось сохранить пост: возможно, серия уже занята. Попробуй другой номер.")
    post_id = cur.lastrowid

    await db.execute(
        "UPDATE submissions SET status = 'approved', reviewed_at = ?, reviewer_id = ? WHERE id = ?",
        (now_iso(), reviewer_id, submission_id),
    )
    await db.execute("UPDATE users SET approved_total = approved_total + 1 WHERE id = ?", (submission["user_id"],))

    current = await db.fetchone("SELECT * FROM posts WHERE id = ?", (post_id,))
    await refresh_keyboard(bot, current)
    if collection_id and episode and episode > 1:
        previous = await db.fetchone(
            "SELECT * FROM posts WHERE collection_id = ? AND season = ? AND episode = ? AND is_deleted = 0",
            (collection_id, season, episode - 1),
        )
        await refresh_keyboard(bot, previous)

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
