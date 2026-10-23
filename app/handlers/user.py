import logging
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
from app.services.publishing import card_keyboard, random_keyboard
from app.utils import esc, human_duration, human_size, now_iso, post_link

log = logging.getLogger(__name__)

router = Router(name="user")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.data.startswith("u:"))


class Submit(StatesGroup):
    waiting_media = State()


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
                "size": message.video.file_size, "duration": message.video.duration}
    if message.animation:
        return {"type": "video", "file_id": message.animation.file_id,
                "size": message.animation.file_size, "duration": message.animation.duration}
    if message.photo:
        photo = message.photo[-1]
        return {"type": "photo", "file_id": photo.file_id, "size": photo.file_size, "duration": None}
    if message.document:
        mime = message.document.mime_type or ""
        if mime.startswith("video/"):
            return {"type": "video", "file_id": message.document.file_id,
                    "size": message.document.file_size, "duration": None}
        if mime.startswith("image/"):
            return {"type": "photo", "file_id": message.document.file_id,
                    "size": message.document.file_size, "duration": None}
    return None


async def begin_submit(message: Message, state: FSMContext, bot: Bot, user):
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

    await state.set_state(Submit.waiting_media)
    await message.answer(
        "Пришли одно видео или фото.\n"
        "Одна заявка = один файл. Можно добавить подпись — она не публикуется, это просто комментарий.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("✖️ Отмена", "u:cancel")]]),
    )


@router.message(Submit.waiting_media, Command("cancel"))
async def submit_cancel_command(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.")
    await main_menu(message, message.chat.id)


@router.message(Submit.waiting_media)
async def submit_media(message: Message, state: FSMContext, bot: Bot):
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
    cur = await db.execute(
        "INSERT INTO submissions (user_id, source_chat_id, source_message_id, media_type, file_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user.id, message.chat.id, message.message_id, media["type"], media["file_id"], now_iso()),
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

    keyboard = card_keyboard(submission_id)
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
        "FROM collections c ORDER BY c.position, c.id"
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
        keyboard.append([_btn(f"📁 {name} ({row['cnt']})", f"u:c:{row['id']}:0")])
    keyboard.append([_btn("⬅️ В меню", "u:menu")])
    await message.answer(
        "📚 Подборки:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
    )


PAGE_SIZE = 10


async def _collection_page(collection_id: int, page: int):
    total = int(await db.fetchval(
        "SELECT COUNT(*) FROM posts WHERE collection_id = ? AND is_deleted = 0",
        (collection_id,),
        0,
    ))
    offset = max(page, 0) * PAGE_SIZE
    rows = await db.fetchall(
        "SELECT * FROM posts WHERE collection_id = ? AND is_deleted = 0 "
        "ORDER BY COALESCE(season, 0), COALESCE(episode, 0), id LIMIT ? OFFSET ?",
        (collection_id, PAGE_SIZE, offset),
    )
    return total, rows


def _render_collection(collection, rows, total, page):
    lines = [f"📁 <b>{esc(collection['name'])}</b>", f"Всего: {total}", ""]
    for index, row in enumerate(rows, start=page * PAGE_SIZE + 1):
        if row["episode"]:
            label = f"S{int(row['season'] or collection['season'])}E{int(row['episode'])}"
        else:
            label = f"{index}."
        link = post_link(row["message_id"])
        if link:
            lines.append(f'• {label} — <a href="{link}">смотреть</a>')
        else:
            lines.append(f"• {label}")
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
}


async def show_my(message: Message):
    rows = await db.fetchall(
        "SELECT s.*, p.message_id AS post_message_id FROM submissions s "
        "LEFT JOIN posts p ON p.submission_id = s.id "
        "WHERE s.user_id = ? ORDER BY s.id DESC LIMIT 20",
        (message.chat.id,),
    )
    if not rows:
        await message.answer("Ты ещё ничего не присылал.")
        return
    lines = ["📋 Твои заявки:", ""]
    for row in rows:
        status = STATUS_LABELS.get(row["status"], row["status"])
        line = f"#{row['id']} · {status}"
        if row["status"] == "rejected" and row["reject_reason"]:
            line += f" — {esc(row['reject_reason'])}"
        if row["post_message_id"]:
            link = post_link(row["post_message_id"])
            if link:
                line += f"\n<a href=\"{link}\">Открыть пост</a>"
        lines.append(line)
    await message.answer("\n".join(lines))


@router.callback_query(F.data == "u:my")
async def cb_my(callback: CallbackQuery):
    await callback.answer()
    await show_my(callback.message)

@router.message(F.video | F.photo | F.animation | F.document)
async def media_without_state(message: Message, state: FSMContext, bot: Bot):
    await submit_media(message, state, bot)


@router.message(F.text)
async def text_fallback(message: Message):
    await main_menu(message, message.chat.id)


@router.message()
async def any_fallback(message: Message):
    await main_menu(message, message.chat.id)
