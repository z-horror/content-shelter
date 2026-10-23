import html
import json
import logging
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)

from app import config, db
from app.services import link_download, link_flow
from app.services import submissions as submissions_service
from app.services.publishing import card_keyboard, random_keyboard
from app.utils import (esc, format_local, format_local_day, format_local_short,
                       human_duration, human_size, now_iso, post_link)

log = logging.getLogger(__name__)

router = Router(name="user")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.data.startswith("u:"))


class Submit(StatesGroup):
    waiting_media = State()
    waiting_link = State()


def _btn(text, data=None, url=None):
    if url:
        return InlineKeyboardButton(text=text, url=url)
    return InlineKeyboardButton(text=text, callback_data=data)


async def ensure_user(user):
    await db.execute(
        "INSERT INTO users (id, username, first_name, created_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET username = excluded.username, first_name = excluded.first_name",
        (user.id, user.username, user.first_name, now_iso()),
    )


async def main_menu(message: Message, user_id: int):
    rows = [
        [_btn("📤 Предложить видео", "u:submit")],
        [_btn("📚 Подборки", "u:cols"), _btn("🎲 Случайный пост", "u:random")],
        [_btn("📋 Мои заявки", "u:my"), _btn("ℹ️ Правила", "u:rules")],
    ]
    if config.is_admin(user_id):
        rows.append([_btn("⚙️ Админка", "adm:menu")])
    await message.answer(
        "Привет! Это бот канала с нейрослопом.\n\n"
        "Присылай смешные видео и фото — лучшие попадут в канал.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def check_subscription(bot: Bot, user_id: int) -> bool:
    if not await db.get_bool_setting("require_subscription", True):
        return True
    if config.CHANNEL_CHAT is None:
        return True
    try:
        member = await bot.get_chat_member(chat_id=config.CHANNEL_CHAT, user_id=user_id)
    except TelegramBadRequest as error:
        log.warning("cannot check subscription for %s: %s", user_id, error)
        return True
    if member.status in ("creator", "administrator", "member"):
        return True
    if member.status == "restricted":
        return bool(getattr(member, "is_member", False))
    return False


async def _submissions_today(user_id: int) -> int:
    local = datetime.now(ZoneInfo(config.TIMEZONE))
    start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = start_local.astimezone(timezone.utc).replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")
    return int(await db.fetchval(
        "SELECT COUNT(*) FROM submissions WHERE user_id = ? AND created_at >= ?",
        (user_id, start_utc),
        0,
    ))


def extract_media(message: Message):
    if message.video:
        return {"type": "video", "file_id": message.video.file_id,
                "unique_id": message.video.file_unique_id,
                "size": message.video.file_size, "duration": message.video.duration}
    if message.animation:
        return {"type": "video", "file_id": message.animation.file_id,
                "unique_id": message.animation.file_unique_id,
                "size": message.animation.file_size, "duration": message.animation.duration}
    if message.photo:
        photo = message.photo[-1]
        return {"type": "photo", "file_id": photo.file_id, "unique_id": photo.file_unique_id,
                "size": photo.file_size, "duration": None}
    if message.document:
        mime = message.document.mime_type or ""
        if mime.startswith("video/"):
            return {"type": "video", "file_id": message.document.file_id,
                    "unique_id": message.document.file_unique_id,
                    "size": message.document.file_size, "duration": None}
        if mime.startswith("image/"):
            return {"type": "photo", "file_id": message.document.file_id,
                    "unique_id": message.document.file_unique_id,
                    "size": message.document.file_size, "duration": None}
    return None


async def begin_submit(message: Message, state: FSMContext, bot: Bot, user):
    ban = await db.fetchone("SELECT is_banned, ban_reason FROM users WHERE id = ?", (user.id,))
    if ban and ban["is_banned"]:
        text = "🚫 Ты не можешь отправлять заявки."
        if ban["ban_reason"]:
            text += f"\nПричина: {ban['ban_reason']}"
        await message.answer(text, parse_mode=None)
        return

    if not await check_subscription(bot, user.id):
        rows = []
        if config.CHANNEL_USERNAME:
            rows.append([_btn("📢 Подписаться", url=f"https://t.me/{config.CHANNEL_USERNAME}")])
        rows.append([_btn("🔄 Проверить", "u:submit")])
        await message.answer(
            "Чтобы предлагать видео, нужно быть подписанным на канал.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
        return

    limit = await db.get_int_setting("submit_daily_limit", 3)
    if limit > 0 and await _submissions_today(user.id) >= limit:
        await message.answer(f"Лимит заявок на сегодня исчерпан ({limit} в сутки). Приходи завтра.")
        return

    link_enabled = await db.get_bool_setting("link_upload_enabled", True)
    if not link_enabled:
        await _prompt_file(message, state)
        return

    await state.clear()
    await message.answer(
        "Как отправить видео?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [_btn("📎 Отправить файл", "u:file"), _btn("🔗 Отправить ссылку", "u:link")],
            [_btn("✖️ Отмена", "u:cancel")],
        ]),
    )


async def _prompt_file(message: Message, state: FSMContext):
    await state.set_state(Submit.waiting_media)
    await message.answer(
        "Пришли одно видео или фото.\n"
        "Одна заявка = один файл. Можно добавить подпись — она не публикуется, это просто комментарий.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("✖️ Отмена", "u:cancel")]]),
    )


async def _prompt_link(message: Message, state: FSMContext):
    await state.set_state(Submit.waiting_link)
    await message.answer(
        "Пришли ссылку на YouTube или Instagram (Reels/видео).\n"
        "Скачаю и передам в модерацию. Ограничения: до 50 МБ и 10 минут.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("✖️ Отмена", "u:cancel")]]),
    )


@router.message(Submit.waiting_media, Command("cancel"))
async def submit_cancel_command(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.")
    await main_menu(message, message.chat.id)


@router.message(Submit.waiting_media)
async def submit_media(message: Message, state: FSMContext, bot: Bot):
    ban = await db.fetchone("SELECT is_banned, ban_reason FROM users WHERE id = ?", (message.from_user.id,))
    if ban and ban["is_banned"]:
        text = "🚫 Ты не можешь отправлять заявки."
        if ban["ban_reason"]:
            text += f"\nПричина: {ban['ban_reason']}"
        await state.clear()
        await message.answer(text, parse_mode=None)
        return

    origin_chat, _ = submissions_service.forwarded_from_channel(message)
    if origin_chat is not None and origin_chat in (config.CHANNEL_NUMERIC_ID, config.CHANNEL_CHAT):
        await message.answer("Этот пост уже в канале — добавлять не нужно.")
        return

    if message.media_group_id:
        await message.answer("Пришли, пожалуйста, один файл за раз (без альбомов).")
        return

    media = extract_media(message)
    if media is None:
        await message.answer("Это не видео и не фото. Пришли один видеофайл или картинку.")
        return

    if media["type"] == "video":
        max_mb = await db.get_int_setting("video_max_mb", 50)
        max_seconds = await db.get_int_setting("video_max_seconds", 300)
        if media["size"] and max_mb > 0 and media["size"] > max_mb * 1024 * 1024:
            await message.answer(f"Видео слишком большое: максимум {max_mb} МБ.")
            return
        if media["duration"] and max_seconds > 0 and media["duration"] > max_seconds:
            await message.answer(f"Видео слишком длинное: максимум {max_seconds} секунд.")
            return
    else:
        max_mb = await db.get_int_setting("photo_max_mb", 10)
        if media["size"] and max_mb > 0 and media["size"] > max_mb * 1024 * 1024:
            await message.answer(f"Фото слишком большое: максимум {max_mb} МБ.")
            return

    if config.ADMIN_CHAT is None:
        await message.answer("Приём заявок временно не настроен. Попробуй позже.")
        log.error("ADMIN_GROUP_ID is not configured")
        return

    user = message.from_user
    comment = (message.caption or "").strip()[:300] or None
    cur = await db.execute(
        "INSERT INTO submissions (user_id, source_chat_id, source_message_id, media_type, file_id, "
        "file_unique_id, comment_text, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (user.id, message.chat.id, message.message_id, media["type"], media["file_id"],
         media.get("unique_id"), comment, now_iso()),
    )
    submission_id = cur.lastrowid

    media_title = "видео" if media["type"] == "video" else "фото"
    details = f"📦 {media_title} · {human_size(media['size'])}"
    if media["duration"]:
        details += f" · {human_duration(media['duration'])}"
    author = esc(user.first_name)
    if user.username:
        author += f" · @{user.username}"
    author += f" · <code>{user.id}</code>"
    card = f"📥 <b>Заявка #{submission_id}</b>\n👤 {author}\n{details}"
    if comment:
        card += f"\n💬 {esc(comment)}"
    found = await submissions_service.duplicates(media.get("unique_id"), exclude_submission_id=submission_id)
    for line in submissions_service.duplicate_lines(found):
        card += f"\n{line}"

    keyboard = card_keyboard(submission_id, user.id)
    log.info("submission #%s from %s (%s)", submission_id, user.id, media["type"])

    try:
        sent = await bot.copy_message(
            chat_id=config.ADMIN_CHAT,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
            caption=card,
            parse_mode="HTML",
            reply_markup=keyboard,
            message_thread_id=config.ADMIN_TOPIC_ID,
        )
    except TelegramBadRequest as error:
        log.exception("cannot copy submission to admin chat")
        await db.execute("DELETE FROM submissions WHERE id = ?", (submission_id,))
        await message.answer("Не получилось передать заявку. Попробуй ещё раз.")
        log.error("copy to admin chat failed: %s", error)
        return

    await db.execute(
        "UPDATE submissions SET admin_chat_id = ?, admin_message_id = ?, card_caption = ? WHERE id = ?",
        (config.ADMIN_CHAT, sent.message_id, card, submission_id),
    )
    await db.execute("UPDATE users SET submissions_total = submissions_total + 1 WHERE id = ?", (user.id,))
    await state.clear()
    await message.answer(f"Заявка #{submission_id} принята. Сообщу, когда её посмотрят.")
    await main_menu(message, message.chat.id)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, command: CommandObject):
    await state.clear()
    await ensure_user(message.from_user)
    payload = (command.args or "").strip().lower()
    if payload == "submit":
        await begin_submit(message, state, message.bot, message.from_user)
    elif payload == "collections":
        await show_collections(message)
    elif payload == "random":
        await send_random(message, message.bot)
    elif payload == "my":
        await show_my(message)
    else:
        await main_menu(message, message.chat.id)


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext):
    await state.clear()
    await ensure_user(message.from_user)
    await main_menu(message, message.chat.id)


@router.callback_query(F.data == "u:menu")
async def cb_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    await main_menu(callback.message, callback.from_user.id)


@router.callback_query(F.data == "u:cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer("Отменено")
    await main_menu(callback.message, callback.from_user.id)


@router.callback_query(F.data == "u:file")
async def cb_file(callback: CallbackQuery, state: FSMContext):
    await ensure_user(callback.from_user)
    await callback.answer()
    await _prompt_file(callback.message, state)


@router.callback_query(F.data == "u:link")
async def cb_link(callback: CallbackQuery, state: FSMContext):
    await ensure_user(callback.from_user)
    if not await db.get_bool_setting("link_upload_enabled", True):
        await callback.answer("Загрузка по ссылкам отключена", show_alert=True)
        return
    await callback.answer()
    await _prompt_link(callback.message, state)


@router.callback_query(F.data.in_({"u:submit", "u:check"}))
async def cb_submit(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await ensure_user(callback.from_user)
    await callback.answer()
    await begin_submit(callback.message, state, bot, callback.from_user)


@router.callback_query(F.data == "u:rules")
async def cb_rules(callback: CallbackQuery):
    await callback.answer()
    rules = await db.get_setting("rules_text", "Правила пока не заданы.")
    await callback.message.answer(rules, parse_mode=None)


@router.callback_query(F.data == "u:cols")
async def cb_collections(callback: CallbackQuery):
    await callback.answer()
    await show_collections(callback.message)


async def show_collections(message: Message):
    rows = await db.fetchall(
        "SELECT c.*, (SELECT COUNT(*) FROM posts p WHERE p.collection_id = c.id AND p.is_deleted = 0) AS cnt "
        "FROM collections c ORDER BY c.is_ordered DESC, c.position, c.id"
    )
    items = [row for row in rows if row["cnt"] > 0]
    if not items:
        await message.answer("Пока нет ни одной подборки.")
        return
    keyboard = []
    for row in items:
        name = row["name"]
        if len(name) > 40:
            name = name[:37] + "..."
        icon = "📺" if row["is_ordered"] else "📁"
        keyboard.append([_btn(f"{icon} {name} ({row['cnt']})", f"u:c:{row['id']}:0")])
    keyboard.append([_btn("⬅️ В меню", "u:menu")])
    await message.answer(
        "📚 Подборки:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
    )


PAGE_SIZE = 10


def _plural(number: int, one: str, few: str, many: str) -> str:
    if number % 10 == 1 and number % 100 != 11:
        return one
    if number % 10 in (2, 3, 4) and number % 100 not in (12, 13, 14):
        return few
    return many


def _plain_title(caption):
    if not caption:
        return None
    text = html.unescape(re.sub(r"<[^>]+>", "", caption))
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#") or re.match(r"^https?://", line):
            return None
        return line[:40]
    return None


def _author_label(row):
    if row["username"]:
        return f"@{row['username']}"
    if row["first_name"]:
        return row["first_name"]
    return "из канала"


def _series_label(season, episode, default_season=1):
    if not episode:
        return ""
    return f"S{int(season or default_season)}E{int(episode)}"


async def _collection_page(collection_id: int, page: int):
    total = int(await db.fetchval(
        "SELECT COUNT(*) FROM posts WHERE collection_id = ? AND is_deleted = 0",
        (collection_id,),
        0,
    ))
    offset = max(page, 0) * PAGE_SIZE
    rows = await db.fetchall(
        "SELECT p.*, u.username, u.first_name FROM posts p "
        "LEFT JOIN users u ON u.id = p.author_id "
        "WHERE p.collection_id = ? AND p.is_deleted = 0 "
        "ORDER BY COALESCE(p.season, 0), COALESCE(p.episode, 0), p.id LIMIT ? OFFSET ?",
        (collection_id, PAGE_SIZE, offset),
    )
    return total, rows


def _render_collection(collection, rows, total, page):
    icon = "📺" if collection["is_ordered"] else "📁"
    if collection["is_ordered"]:
        count = f"{total} {_plural(total, 'серия', 'серии', 'серий')}"
    else:
        count = f"{total} {_plural(total, 'пост', 'поста', 'постов')}"
    lines = [f"{icon} <b>{esc(collection['name'])}</b>", count, ""]
    for index, row in enumerate(rows, start=page * PAGE_SIZE + 1):
        date = format_local_day(row["published_at"])
        author = esc(_author_label(row))
        link = post_link(row["message_id"])
        if collection["is_ordered"] and row["episode"]:
            label = _series_label(row["season"], row["episode"], collection["season"])
            text = f"{label} · {date} · {author}"
        else:
            kind = "🎬" if row["media_type"] == "video" else "🖼"
            title = _plain_title(row["caption"])
            if title:
                text = f"{index}. {kind} «{esc(title)}» · {date} · {author}"
            else:
                text = f"{index}. {kind} без названия · {date} · {author}"
        if link:
            lines.append(f'{text} — <a href="{link}">смотреть</a>')
        else:
            lines.append(text)
    return "\n".join(lines)


def _collection_keyboard(collection, total, page):
    keyboard = []
    if total > 0:
        keyboard.append([_btn(f"▶️ Прислать все ({len(range(page * PAGE_SIZE, min(total, (page + 1) * PAGE_SIZE)))})",
                              f"u:all:{collection['id']}:{page}")])
    navigation = []
    if page > 0:
        navigation.append(_btn("⬅️", f"u:c:{collection['id']}:{page - 1}"))
    if (page + 1) * PAGE_SIZE < total:
        navigation.append(_btn("➡️", f"u:c:{collection['id']}:{page + 1}"))
    if navigation:
        keyboard.append(navigation)
    keyboard.append([_btn("⬅️ К подборкам", "u:cols")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


@router.callback_query(F.data.startswith("u:c:"))
async def cb_collection(callback: CallbackQuery):
    try:
        _, _, collection_id, page = callback.data.split(":")
        collection_id, page = int(collection_id), int(page)
    except ValueError:
        await callback.answer("Некорректная кнопка", show_alert=True)
        return
    collection = await db.fetchone("SELECT * FROM collections WHERE id = ?", (collection_id,))
    if collection is None:
        await callback.answer("Подборка не найдена", show_alert=True)
        return
    total, rows = await _collection_page(collection_id, page)
    await callback.answer()
    await callback.message.answer(
        _render_collection(collection, rows, total, page),
        reply_markup=_collection_keyboard(collection, total, page),
    )


@router.callback_query(F.data.startswith("u:all:"))
async def cb_collection_all(callback: CallbackQuery, bot: Bot):
    try:
        _, _, collection_id, page = callback.data.split(":")
        collection_id, page = int(collection_id), int(page)
    except ValueError:
        await callback.answer("Некорректная кнопка", show_alert=True)
        return
    _, rows = await _collection_page(collection_id, page)
    rows = [row for row in rows if row["file_id"]]
    if not rows:
        await callback.answer("Нечего отправлять", show_alert=True)
        return
    await callback.answer()

    chat_id = callback.message.chat.id
    if len(rows) == 1:
        row = rows[0]
        await bot.copy_message(chat_id, row["channel_id"], row["message_id"])
        return

    media = []
    for index, row in enumerate(rows):
        caption = "📁 Подборка" if index == 0 else None
        if row["media_type"] == "photo":
            media.append(InputMediaPhoto(media=row["file_id"], caption=caption))
        else:
            media.append(InputMediaVideo(media=row["file_id"], caption=caption))
    try:
        await bot.send_media_group(chat_id, media)
    except TelegramBadRequest as error:
        log.warning("media group failed, falling back to copy: %s", error)
        for row in rows:
            try:
                await bot.copy_message(chat_id, row["channel_id"], row["message_id"])
            except TelegramBadRequest:
                continue


async def send_random(message: Message, bot: Bot):
    user_id = message.chat.id
    last = await db.fetchval("SELECT last_random_post FROM users WHERE id = ?", (user_id,))

    for attempt in range(3):
        post = await db.fetchone(
            "SELECT * FROM posts WHERE is_deleted = 0 AND (? IS NULL OR id != ?) "
            "ORDER BY RANDOM() LIMIT 1",
            (last, last),
        )
        if post is None:
            post = await db.fetchone(
                "SELECT * FROM posts WHERE is_deleted = 0 ORDER BY RANDOM() LIMIT 1"
            )
        if post is None:
            break
        try:
            await bot.copy_message(
                chat_id=user_id,
                from_chat_id=post["channel_id"],
                message_id=post["message_id"],
                reply_markup=random_keyboard(),
            )
            await db.execute("UPDATE users SET last_random_post = ? WHERE id = ?", (post["id"], user_id))
            return
        except TelegramBadRequest as error:
            log.warning("random post %s unavailable: %s", post["id"], error)
            await db.execute("UPDATE posts SET is_deleted = 1 WHERE id = ?", (post["id"],))

    await message.answer("Пока нет ни одного поста. Загляни позже.")


@router.callback_query(F.data == "u:random")
async def cb_random(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    await send_random(callback.message, bot)


STATUS_LABELS = {
    "pending": "⏳ на модерации",
    "scheduled": "🕓 запланировано",
    "approved": "✅ опубликовано",
    "rejected": "❌ отклонено",
    "deleted": "🗑 удалено",
}

STATUS_EMOJI = {
    "pending": "⏳",
    "scheduled": "🕓",
    "approved": "✅",
    "rejected": "❌",
}

MY_PAGE_SIZE = 10

MY_SELECT = """
SELECT s.*,
       p.message_id AS post_message_id,
       p.published_at AS post_published_at,
       p.caption AS post_caption,
       p.season AS post_season,
       p.episode AS post_episode,
       pc.name AS post_collection,
       q.publish_at AS publish_at,
       q.caption AS sched_caption,
       q.season AS sched_season,
       q.episode AS sched_episode,
       qc.name AS sched_collection
FROM submissions s
LEFT JOIN posts p ON p.id = (SELECT MAX(id) FROM posts WHERE submission_id = s.id)
LEFT JOIN collections pc ON pc.id = p.collection_id
LEFT JOIN queue q ON q.id = (
    SELECT MAX(id) FROM queue WHERE submission_id = s.id AND status = 'waiting'
)
LEFT JOIN collections qc ON qc.id = q.collection_id
WHERE s.user_id = ? AND s.status != 'deleted'
"""


async def _my_rows(user_id, limit=None, offset=0):
    sql = MY_SELECT + " ORDER BY s.id DESC"
    params = [user_id]
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params += [limit, offset]
    return await db.fetchall(sql, tuple(params))


async def _my_row(user_id, submission_id):
    return await db.fetchone(MY_SELECT + " AND s.id = ?", (user_id, submission_id))


def _my_line(row):
    status = row["status"]
    if status == "scheduled":
        when = format_local_short(row["publish_at"]) if row["publish_at"] else format_local_day(row["created_at"])
        episode = _series_label(row["sched_season"], row["sched_episode"])
        return f"#{row['id']} · 🕓 {when}{(' · ' + episode) if episode else ''}", None
    if status == "approved":
        when = format_local_day(row["post_published_at"] or row["created_at"])
        episode = _series_label(row["post_season"], row["post_episode"])
        text = f"#{row['id']} · ✅ {when}{(' · ' + episode) if episode else ''}"
        link = post_link(row["post_message_id"]) if row["post_message_id"] else None
        return text, link
    if status == "rejected":
        return f"#{row['id']} · ❌ {format_local_day(row['created_at'])}", None
    return f"#{row['id']} · ⏳ {format_local_day(row['created_at'])}", None


async def show_my(message: Message):
    rows = await _my_rows(message.chat.id, limit=10)
    if not rows:
        await message.answer(
            "Ты ещё ничего не присылал.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("⬅️ В меню", "u:menu")]]),
        )
        return

    lines = ["📋 Твои заявки", ""]
    for row in rows:
        text, link = _my_line(row)
        if link:
            lines.append(f'<a href="{link}">{esc(text)}</a>')
        else:
            lines.append(esc(text))

    await message.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [_btn("🔍 Детали", "u:mylist:0"), _btn("📚 Подборки", "u:cols")],
            [_btn("⬅️ В меню", "u:menu")],
        ]),
    )


async def _show_my_list(message: Message, user_id: int, page: int):
    total = int(await db.fetchval(
        "SELECT COUNT(*) FROM submissions WHERE user_id = ? AND status != 'deleted'",
        (user_id,),
        0,
    ))
    if total == 0:
        await message.answer("Ты ещё ничего не присылал.")
        return
    page = max(0, min(page, (total - 1) // MY_PAGE_SIZE))
    rows = await _my_rows(user_id, limit=MY_PAGE_SIZE, offset=page * MY_PAGE_SIZE)
    keyboard = []
    for row in rows:
        emoji = STATUS_EMOJI.get(row["status"], "•")
        keyboard.append([
            _btn(
                f"#{row['id']} · {emoji} {format_local_day(row['created_at'])}",
                f"u:mycard:{row['id']}:{page}",
            )
        ])
    navigation = []
    if page > 0:
        navigation.append(_btn("⬅️", f"u:mylist:{page - 1}"))
    if (page + 1) * MY_PAGE_SIZE < total:
        navigation.append(_btn("➡️", f"u:mylist:{page + 1}"))
    if navigation:
        keyboard.append(navigation)
    keyboard.append([_btn("⬅️ К заявкам", "u:my")])
    await message.answer(
        f"Выбери заявку: {page + 1}/{(total - 1) // MY_PAGE_SIZE + 1}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
    )


def _meta_title(row):
    try:
        meta = json.loads(row["source_meta"] or "{}")
    except (TypeError, ValueError):
        return None
    title = (meta.get("title") or "").strip()
    return title[:60] or None


async def _show_my_card(message: Message, user_id: int, submission_id: int, page: int):
    row = await _my_row(user_id, submission_id)
    if row is None:
        await message.answer("Заявка недоступна.")
        return

    lines = [
        f"📋 <b>Заявка #{row['id']}</b>",
        f"Статус: {STATUS_LABELS.get(row['status'], row['status'])}",
        f"Тип: {'🎬 видео' if row['media_type'] == 'video' else '🖼 фото'}",
        f"Отправлено: {format_local(row['created_at'])}",
    ]

    if row["status"] == "scheduled":
        if row["sched_collection"]:
            lines.append(f"Подборка: {esc(row['sched_collection'])}")
        episode = _series_label(row["sched_season"], row["sched_episode"])
        if episode:
            lines.append(f"Серия: {episode}")
        if row["publish_at"]:
            lines.append(f"Выйдет: {format_local(row['publish_at'])}")
    elif row["status"] == "approved":
        if row["post_collection"]:
            lines.append(f"Подборка: {esc(row['post_collection'])}")
        episode = _series_label(row["post_season"], row["post_episode"])
        if episode:
            lines.append(f"Серия: {episode}")
        if row["post_published_at"]:
            lines.append(f"Опубликовано: {format_local(row['post_published_at'])}")
    elif row["status"] == "rejected" and row["reject_reason"]:
        lines.append(f"Причина: {esc(row['reject_reason'][:300])}")

    title = _plain_title(row["sched_caption"] or row["post_caption"]) or _meta_title(row)
    if title:
        lines.append(f"Заголовок: {esc(title)}")
    if row["comment_text"]:
        lines.append(f"Комментарий: {esc(row['comment_text'][:300])}")

    keyboard = []
    if row["status"] == "approved" and row["post_message_id"]:
        link = post_link(row["post_message_id"])
        if link:
            keyboard.append([_btn("🔗 Открыть пост", url=link)])
    keyboard.append([_btn("⬅️ К списку", f"u:mylist:{page}")])
    await message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))


@router.callback_query(F.data == "u:my")
async def cb_my(callback: CallbackQuery):
    await callback.answer()
    await show_my(callback.message)


@router.callback_query(F.data.startswith("u:mylist:"))
async def cb_my_list(callback: CallbackQuery):
    try:
        page = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        page = 0
    await callback.answer()
    await _show_my_list(callback.message, callback.from_user.id, page)


@router.callback_query(F.data.startswith("u:mycard:"))
async def cb_my_card(callback: CallbackQuery):
    try:
        _, _, submission_id, page = callback.data.split(":")
        submission_id, page = int(submission_id), int(page)
    except ValueError:
        await callback.answer("Некорректная кнопка", show_alert=True)
        return
    await callback.answer()
    await _show_my_card(callback.message, callback.from_user.id, submission_id, page)


@router.message(F.video | F.photo | F.animation | F.document)
async def media_without_state(message: Message, state: FSMContext, bot: Bot):
    await submit_media(message, state, bot)


GENERIC_LINK_ERROR = (
    "К сожалению, не удалось загрузить видео по этой ссылке. "
    "Возможно, публикация недоступна или источник ограничивает скачивание. "
    "Попробуйте отправить видео файлом."
)


def _duplicate_text(row) -> str:
    if row["post_message_id"]:
        link = post_link(row["post_message_id"])
        return f"Эта ссылка уже была в канале: {link}" if link else "Эта ссылка уже была в канале."
    if row["status"] == "scheduled" and row["publish_at"]:
        return f"Эта ссылка уже запланирована на {format_local(row['publish_at'])}."
    if row["status"] == "pending":
        return f"Эта ссылка уже на модерации (заявка #{row['id']})."
    return "Эта ссылка уже была."


@router.message(Submit.waiting_link)
async def submit_link(message: Message, state: FSMContext, bot: Bot):
    if not await db.get_bool_setting("link_upload_enabled", True):
        await state.clear()
        await message.answer("Загрузка по ссылкам отключена.")
        return

    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        await message.answer("Пришли ссылку на YouTube или Instagram.")
        return

    await message.answer("Скачиваю видео…")

    async def cancelled():
        return await state.get_state() != Submit.waiting_link.state

    try:
        submission_id = await link_flow.download_and_submit(
            bot, message.from_user, text, cancelled
        )
    except link_flow.LinkCancelled:
        return
    except link_flow.LinkDuplicate as error:
        error_text = _duplicate_text(error.row)
    except (link_download.InvalidLink, link_download.UnsupportedSource):
        error_text = "Поддерживаются только ссылки на YouTube и Instagram."
    except link_download.LinkBusy:
        error_text = "Эта ссылка уже скачивается. Подожди немного."
    except link_download.TooLarge:
        error_text = "Видео больше 50 МБ — Telegram не даст его опубликовать. Пришли файлом."
    except link_download.TooLong:
        error_text = "Видео длиннее 10 минут. Пришли файлом."
    except link_flow.LinkUploadFailed:
        error_text = "Видео скачано, но не удалось передать его в модерацию. Попробуй позже."
    except link_download.LinkError:
        error_text = GENERIC_LINK_ERROR
    except Exception:  # noqa: BLE001
        log.exception("unexpected link submission error")
        error_text = GENERIC_LINK_ERROR
    else:
        await state.clear()
        await message.answer(f"Заявка #{submission_id} принята. Сообщу, когда её посмотрят.")
        await main_menu(message, message.chat.id)
        return
    finally:
        if await state.get_state() == Submit.waiting_link.state:
            await state.clear()

    await message.answer(error_text, parse_mode=None)


@router.message(F.text)
async def text_fallback(message: Message):
    await main_menu(message, message.chat.id)


@router.message()
async def any_fallback(message: Message):
    await main_menu(message, message.chat.id)
