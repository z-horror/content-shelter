import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import BaseFilter, Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app import config, db, sessions
from app.services import collections as collections_service
from app.services import publishing
from app.keyboards import schedule_keyboard
from app.utils import esc, format_local

log = logging.getLogger(__name__)

router = Router(name="admin")

SETTINGS = [
    ("submit_daily_limit", "Лимит заявок в сутки", "int"),
    ("video_max_mb", "Макс. размер видео, МБ", "int"),
    ("video_max_seconds", "Макс. длительность видео, сек", "int"),
    ("photo_max_mb", "Макс. размер фото, МБ", "int"),
    ("require_subscription", "Только подписчики", "bool"),
    ("random_button_under_posts", "Кнопка «Случайный» под постами", "bool"),
    ("rules_text", "Текст «Правила»", "text"),
]
SETTINGS_MAP = {row[0]: row for row in SETTINGS}


class IsAdmin(BaseFilter):
    async def __call__(self, event) -> bool:
        user = getattr(event, "from_user", None)
        return user is not None and config.is_admin(user.id)


class SettingsEdit(StatesGroup):
    value = State()


class Rename(StatesGroup):
    value = State()


class NewCol(StatesGroup):
    name = State()


def _btn(text, data=None, url=None):
    if url:
        return InlineKeyboardButton(text=text, url=url)
    return InlineKeyboardButton(text=text, callback_data=data)


def menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("📥 Очередь", "adm:queue")],
        [_btn("🗓 Запланировано", "adm:sched")],
        [_btn("📊 Статистика", "adm:stats")],
        [_btn("📁 Подборки", "adm:cols")],
        [_btn("⚙️ Настройки", "adm:set")],
        [_btn("🔧 Сервис", "adm:svc")],
        [_btn("⬅️ В меню", "u:menu")],
    ])


@router.message(Command("admin"), IsAdmin())
async def cmd_admin(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("⚙️ Админка", reply_markup=menu_keyboard())


@router.message(CommandStart(), F.chat.type.in_({"group", "supergroup"}))
async def group_start(message: Message):
    await message.answer(
        f"Бот работает в личке: @{config.BOT_USERNAME}\n"
        "Здесь, в админ-группе, появляются заявки и идёт модерация."
    )


@router.message(Command("id"), IsAdmin())
async def cmd_id(message: Message):
    await message.answer(f"chat_id: <code>{message.chat.id}</code>\ntype: {message.chat.type}")


@router.callback_query(F.data == "adm:menu", IsAdmin())
async def cb_admin_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    await callback.message.answer("⚙️ Админка", reply_markup=menu_keyboard())


@router.callback_query(F.data == "adm:queue", IsAdmin())
async def cb_queue(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    rows = await db.fetchall(
        "SELECT s.*, u.username, u.first_name FROM submissions s "
        "LEFT JOIN users u ON u.id = s.user_id WHERE s.status = 'pending' ORDER BY s.id"
    )
    if not rows:
        await callback.message.answer(
            "📥 Очередь пуста.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("⬅️ Назад", "adm:menu")]]),
        )
        return
    keyboard = []
    for row in rows:
        name = f"@{row['username']}" if row["username"] else (row["first_name"] or str(row["user_id"]))
        mark = "🎬" if row["media_type"] == "video" else "🖼"
        keyboard.append([_btn(f"{mark} #{row['id']} · {name}", f"q:show:{row['id']}")])
    keyboard.append([_btn("⬅️ Назад", "adm:menu")])
    await callback.message.answer(
        f"📥 Неразобранные заявки: {len(rows)}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
    )


@router.callback_query(F.data.startswith("q:show:"), IsAdmin())
async def cb_queue_show(callback: CallbackQuery, bot: Bot):
    submission_id = int(callback.data.split(":")[2])
    submission = await db.fetchone("SELECT * FROM submissions WHERE id = ?", (submission_id,))
    if submission is None:
        await callback.answer("Заявка не найдена", show_alert=True)
        return
    if submission["status"] != "pending":
        await callback.answer("Заявка уже обработана", show_alert=True)
        return
    card = submission["card_caption"] or f"📥 <b>Заявка #{submission_id}</b>"
    try:
        sent = await bot.copy_message(
            chat_id=config.ADMIN_CHAT,
            from_chat_id=submission["source_chat_id"],
            message_id=submission["source_message_id"],
            caption=card,
            parse_mode="HTML",
            reply_markup=publishing.card_keyboard(submission_id),
            message_thread_id=config.ADMIN_TOPIC_ID,
        )
    except Exception as error:  # noqa: BLE001
        log.warning("cannot resend submission %s: %s", submission_id, error)
        await callback.answer("Не удалось переотправить: исходное сообщение недоступно", show_alert=True)
        return
    await db.execute(
        "UPDATE submissions SET admin_chat_id = ?, admin_message_id = ? WHERE id = ?",
        (config.ADMIN_CHAT, sent.message_id, submission_id),
    )
    await callback.answer("Отправил карточку")


@router.message(Command("queue"), IsAdmin())
async def cmd_queue(message: Message):
    rows = await db.fetchall(
        "SELECT s.*, u.username, u.first_name FROM submissions s "
        "LEFT JOIN users u ON u.id = s.user_id WHERE s.status = 'pending' ORDER BY s.id"
    )
    if not rows:
        await message.answer("📥 Очередь пуста.")
        return
    keyboard = []
    for row in rows:
        name = f"@{row['username']}" if row["username"] else (row["first_name"] or str(row["user_id"]))
        mark = "🎬" if row["media_type"] == "video" else "🖼"
        keyboard.append([_btn(f"{mark} #{row['id']} · {name}", f"q:show:{row['id']}")])
    keyboard.append([_btn("⚙️ Админка", "adm:menu")])
    await message.answer(
        f"📥 Неразобранные заявки: {len(rows)}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
    )


async def _send_scheduled(target):
    rows = await db.fetchall(
        "SELECT q.*, u.username, u.first_name, c.name AS collection_name "
        "FROM queue q "
        "JOIN submissions s ON s.id = q.submission_id "
        "LEFT JOIN users u ON u.id = s.user_id "
        "LEFT JOIN collections c ON c.id = q.collection_id "
        "WHERE q.status = 'waiting' ORDER BY q.publish_at"
    )
    if not rows:
        await target.answer("🗓 Ничего не запланировано.")
        return
    keyboard = []
    for row in rows:
        title = f"{format_local(row['publish_at'])} · #{row['submission_id']}"
        if row["collection_name"]:
            title += f" · {row['collection_name']}"
            if row["episode"]:
                title += f" S{int(row['season'] or 1)}E{int(row['episode'])}"
        if len(title) > 60:
            title = title[:57] + "..."
        keyboard.append([_btn(f"🕓 {title}", f"sch:v:{row['id']}")])
    keyboard.append([_btn("⚙️ Админка", "adm:menu")])
    await target.answer(
        f"🗓 Запланировано: {len(rows)}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
    )


@router.callback_query(F.data == "adm:sched", IsAdmin())
async def cb_scheduled(callback: CallbackQuery):
    await callback.answer()
    await _send_scheduled(callback.message)


@router.message(Command("scheduled"), IsAdmin())
async def cmd_scheduled(message: Message):
    await _send_scheduled(message)


@router.callback_query(F.data.startswith("sch:v:"), IsAdmin())
async def cb_sched_view(callback: CallbackQuery):
    queue_id = int(callback.data.split(":")[2])
    row = await db.fetchone(
        "SELECT q.*, u.username, u.first_name, c.name AS collection_name "
        "FROM queue q "
        "JOIN submissions s ON s.id = q.submission_id "
        "LEFT JOIN users u ON u.id = s.user_id "
        "LEFT JOIN collections c ON c.id = q.collection_id "
        "WHERE q.id = ?",
        (queue_id,),
    )
    if row is None or row["status"] != "waiting":
        await callback.answer("Запись уже неактуальна", show_alert=True)
        return
    await callback.answer()
    author = f"@{row['username']}" if row["username"] else (row["first_name"] or "—")
    lines = [
        f"🕓 Заявка #{row['submission_id']}",
        f"Время: {format_local(row['publish_at'])}",
        f"Автор: {author}",
    ]
    if row["collection_name"]:
        lines.append(f"Подборка: {row['collection_name']}")
        if row["episode"]:
            lines.append(f"Серия: S{int(row['season'] or 1)}E{int(row['episode'])}")
    await callback.message.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [_btn("🚀 Опубликовать сейчас", f"sch:now:{queue_id}")],
            [_btn("✏️ Изменить время", f"sch:time:{queue_id}")],
            [_btn("🗑 Отменить", f"sch:cancel:{queue_id}")],
            [_btn("⬅️ К списку", "adm:sched")],
        ]),
    )


@router.callback_query(F.data.startswith("sch:now:"), IsAdmin())
async def cb_sched_now(callback: CallbackQuery, bot: Bot):
    queue_id = int(callback.data.split(":")[2])
    row = await db.fetchone("SELECT * FROM queue WHERE id = ? AND status = 'waiting'", (queue_id,))
    if row is None:
        await callback.answer("Запись уже неактуальна", show_alert=True)
        return
    moved = await db.execute(
        "UPDATE queue SET status = 'processing' WHERE id = ? AND status = 'waiting'", (queue_id,)
    )
    if moved.rowcount == 0:
        await callback.answer("Запись уже неактуальна", show_alert=True)
        return
    await callback.answer("Публикую...")
    try:
        result = await publishing.publish_submission(
            bot,
            submission_id=row["submission_id"],
            caption_text=row["caption"],
            collection_id=row["collection_id"],
            season=row["season"],
            episode=row["episode"],
            reviewer_id=callback.from_user.id,
        )
    except Exception as error:  # noqa: BLE001
        log.exception("manual publish from queue failed")
        await db.execute("UPDATE queue SET status = 'waiting' WHERE id = ?", (queue_id,))
        await callback.message.answer(f"⚠️ Не удалось опубликовать: {esc(error)}")
        return
    await db.execute("UPDATE queue SET status = 'done' WHERE id = ?", (queue_id,))
    await callback.message.answer(f"✅ Опубликовано: {result['link']}")


@router.callback_query(F.data.startswith("sch:cancel:"), IsAdmin())
async def cb_sched_cancel(callback: CallbackQuery, bot: Bot):
    queue_id = int(callback.data.split(":")[2])
    row = await db.fetchone("SELECT * FROM queue WHERE id = ? AND status = 'waiting'", (queue_id,))
    if row is None:
        await callback.answer("Запись уже неактуальна", show_alert=True)
        return
    updated = await db.execute(
        "UPDATE queue SET status = 'cancelled' WHERE id = ? AND status = 'waiting'", (queue_id,)
    )
    if updated.rowcount == 0:
        await callback.answer("Запись уже неактуальна", show_alert=True)
        return
    await db.execute(
        "UPDATE submissions SET status = 'pending' WHERE id = ? AND status = 'scheduled'",
        (row["submission_id"],),
    )
    submission = await db.fetchone("SELECT * FROM submissions WHERE id = ?", (row["submission_id"],))
    if submission is not None:
        await publishing.edit_card(
            bot,
            submission,
            "⏳ Возвращено в очередь",
            keyboard=publishing.card_keyboard(row["submission_id"]),
        )
    await callback.answer("Отменено")
    await callback.message.answer(
        f"Заявка #{row['submission_id']} снята с публикации и вернулась в очередь."
    )


@router.callback_query(F.data.startswith("sch:time:"), IsAdmin())
async def cb_sched_time(callback: CallbackQuery):
    queue_id = int(callback.data.split(":")[2])
    row = await db.fetchone("SELECT * FROM queue WHERE id = ? AND status = 'waiting'", (queue_id,))
    if row is None:
        await callback.answer("Запись уже неактуальна", show_alert=True)
        return
    await sessions.set_session(callback.from_user.id, step="sched_when", queue_id=queue_id)
    await callback.answer()
    await callback.message.answer(
        "Когда публиковать?",
        reply_markup=schedule_keyboard(),
    )


@router.callback_query(F.data == "adm:stats", IsAdmin())
async def cb_stats(callback: CallbackQuery):
    await callback.answer()
    users = await db.fetchval("SELECT COUNT(*) FROM users", default=0)
    pending = await db.fetchval("SELECT COUNT(*) FROM submissions WHERE status = 'pending'", default=0)
    scheduled = await db.fetchval("SELECT COUNT(*) FROM submissions WHERE status = 'scheduled'", default=0)
    approved = await db.fetchval("SELECT COUNT(*) FROM submissions WHERE status = 'approved'", default=0)
    rejected = await db.fetchval("SELECT COUNT(*) FROM submissions WHERE status = 'rejected'", default=0)
    posts = await db.fetchval("SELECT COUNT(*) FROM posts WHERE is_deleted = 0", default=0)
    collections = await db.fetchval("SELECT COUNT(*) FROM collections", default=0)
    top = await db.fetchall(
        "SELECT username, first_name, approved_total FROM users WHERE approved_total > 0 "
        "ORDER BY approved_total DESC LIMIT 5"
    )
    lines = [
        "📊 Статистика",
        "",
        f"Пользователи: {users}",
        f"Посты в канале: {posts}",
        f"Подборки: {collections}",
        "",
        f"⏳ На модерации: {pending}",
        f"🕓 Запланировано: {scheduled}",
        f"✅ Опубликовано: {approved}",
        f"❌ Отклонено: {rejected}",
    ]
    if top:
        lines += ["", "🏆 Топ авторов:"]
        for row in top:
            name = f"@{row['username']}" if row["username"] else esc(row["first_name"])
            lines.append(f"• {name} — {row['approved_total']}")
    await callback.message.answer("\n".join(lines))


@router.callback_query(F.data == "adm:svc", IsAdmin())
async def cb_service(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    lines = ["🔧 Сервис", ""]
    try:
        chat = await bot.get_chat(config.CHANNEL_CHAT)
        lines.append(f"Канал: OK — {esc(chat.title)} (`{chat.id}`)")
        member = await bot.get_chat_member(config.CHANNEL_CHAT, (await bot.get_me()).id)
        lines.append(f"Бот в канале: {member.status}")
    except Exception as error:  # noqa: BLE001
        lines.append(f"Канал: ❌ {error}")
    try:
        group = await bot.get_chat(config.ADMIN_CHAT)
        lines.append(f"Админ-чат: OK — {esc(group.title)} (`{group.id}`)")
    except Exception as error:  # noqa: BLE001
        lines.append(f"Админ-чат: ❌ {error}")
    lines.append(f"Бот: @{config.BOT_USERNAME}")
    await callback.message.answer("\n".join(lines))


@router.callback_query(F.data == "adm:cols", IsAdmin())
async def cb_collections(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    rows = await db.fetchall(
        "SELECT c.*, (SELECT COUNT(*) FROM posts p WHERE p.collection_id = c.id AND p.is_deleted = 0) AS cnt "
        "FROM collections c ORDER BY c.position, c.id"
    )
    keyboard = []
    for row in rows:
        mark = "📺" if row["is_ordered"] else "📁"
        keyboard.append([_btn(f"{mark} {row['name']} ({row['cnt']})", f"ac:v:{row['id']}")])
    keyboard.append([_btn("➕ Новая подборка", "ac:new")])
    keyboard.append([_btn("⬅️ Назад", "adm:menu")])
    await callback.message.answer("📁 Подборки:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))


@router.callback_query(F.data.startswith("ac:v:"), IsAdmin())
async def cb_collection_view(callback: CallbackQuery):
    await callback.answer()
    collection_id = int(callback.data.split(":")[2])
    row = await db.fetchone("SELECT * FROM collections WHERE id = ?", (collection_id,))
    if row is None:
        await callback.message.answer("Подборка не найдена.")
        return
    count = await db.fetchval(
        "SELECT COUNT(*) FROM posts WHERE collection_id = ? AND is_deleted = 0",
        (collection_id,),
        default=0,
    )
    kind = "сериал" if row["is_ordered"] else "папка"
    text = (
        f"📁 <b>{esc(row['name'])}</b>\n"
        f"Тип: {kind}\n"
        f"Хештег: #{row['slug']}\n"
        f"Постов: {count}"
    )
    toggle_text = "📁 Сделать папкой" if row["is_ordered"] else "📺 Сделать сериалом"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [_btn("✏️ Переименовать", f"ac:ren:{collection_id}")],
        [_btn(toggle_text, f"ac:type:{collection_id}")],
        [_btn("⬆️ Выше", f"ac:up:{collection_id}"), _btn("⬇️ Ниже", f"ac:dn:{collection_id}")],
        [_btn("🗑 Удалить", f"ac:del:{collection_id}")],
        [_btn("⬅️ Назад", "adm:cols")],
    ])
    await callback.message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("ac:ren:"), IsAdmin())
async def cb_rename(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    collection_id = int(callback.data.split(":")[2])
    await state.set_state(Rename.value)
    await state.update_data(collection_id=collection_id)
    await callback.message.answer(
        "Пришли новое название подборки.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("✖️ Отмена", "adm:cancel")]]),
    )


@router.message(Rename.value, IsAdmin())
async def on_rename(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if len(name) < 2:
        await message.answer("Слишком короткое название. Ещё раз.")
        return
    data = await state.get_data()
    await state.clear()
    await db.execute("UPDATE collections SET name = ? WHERE id = ?", (name[:80], data["collection_id"]))
    await message.answer(f"Переименовано в «{name[:80]}».")


@router.callback_query(F.data.startswith("ac:type:"), IsAdmin())
async def cb_toggle_type(callback: CallbackQuery):
    await callback.answer()
    collection_id = int(callback.data.split(":")[2])
    row = await db.fetchone("SELECT * FROM collections WHERE id = ?", (collection_id,))
    if row is None:
        return
    new_value = 0 if row["is_ordered"] else 1
    await db.execute("UPDATE collections SET is_ordered = ? WHERE id = ?", (new_value, collection_id))
    note = ""
    if new_value == 1:
        note = "\n\nТеперь у новых постов будет номер серии. Старые посты останутся без номеров."
    await callback.message.answer(f"Тип подборки изменён.{note}")


@router.callback_query(F.data.startswith("ac:up:"), IsAdmin())
async def cb_move_up(callback: CallbackQuery):
    await callback.answer()
    await _move(callback.data, -1)


@router.callback_query(F.data.startswith("ac:dn:"), IsAdmin())
async def cb_move_down(callback: CallbackQuery):
    await callback.answer()
    await _move(callback.data, 1)


async def _move(data: str, direction: int):
    collection_id = int(data.split(":")[2])
    current = await db.fetchone("SELECT * FROM collections WHERE id = ?", (collection_id,))
    if current is None:
        return
    neighbour = await db.fetchone(
        "SELECT * FROM collections WHERE position {} ? ORDER BY position {} LIMIT 1".format(
            "<" if direction < 0 else ">", "DESC" if direction < 0 else "ASC"
        ),
        (current["position"],),
    )
    if neighbour is None:
        return
    await db.execute("UPDATE collections SET position = ? WHERE id = ?", (neighbour["position"], current["id"]))
    await db.execute("UPDATE collections SET position = ? WHERE id = ?", (current["position"], neighbour["id"]))


@router.callback_query(F.data.startswith("ac:del:"), IsAdmin())
async def cb_delete(callback: CallbackQuery):
    await callback.answer()
    collection_id = int(callback.data.split(":")[2])
    await callback.message.answer(
        "Удалить подборку? Посты останутся в канале, но выйдут из подборки.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [_btn("🗑 Да, удалить", f"ac:delok:{collection_id}")],
            [_btn("⬅️ Назад", f"ac:v:{collection_id}")],
        ]),
    )


@router.callback_query(F.data.startswith("ac:delok:"), IsAdmin())
async def cb_delete_ok(callback: CallbackQuery):
    await callback.answer()
    collection_id = int(callback.data.split(":")[2])
    await db.execute("UPDATE posts SET collection_id = NULL, season = NULL, episode = NULL WHERE collection_id = ?",
                     (collection_id,))
    await db.execute("DELETE FROM queue WHERE collection_id = ? AND status = 'waiting'", (collection_id,))
    await db.execute("DELETE FROM collections WHERE id = ?", (collection_id,))
    await callback.message.answer("Подборка удалена.")


@router.callback_query(F.data == "ac:new", IsAdmin())
async def cb_new_collection(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(NewCol.name)
    await callback.message.answer(
        "Название новой подборки:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("✖️ Отмена", "adm:cancel")]]),
    )


@router.message(NewCol.name, IsAdmin())
async def on_new_collection_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if name.startswith("/") or len(name) < 2:
        await message.answer("Слишком короткое название. Ещё раз.")
        return
    await state.set_state(None)
    await state.update_data(new_name=name[:80])
    await message.answer(
        "Тип подборки?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [_btn("📺 Сериал (с номерами серий)", "ac:newtype:1")],
            [_btn("📁 Папка (без номеров)", "ac:newtype:0")],
            [_btn("✖️ Отмена", "adm:cancel")],
        ]),
    )


@router.callback_query(F.data.startswith("ac:newtype:"), IsAdmin())
async def cb_new_collection_type(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    is_ordered = callback.data.endswith(":1")
    data = await state.get_data()
    await state.clear()
    name = data.get("new_name") or "Без названия"
    await collections_service.create_collection(name, is_ordered)
    kind = "сериал" if is_ordered else "папка"
    await callback.message.answer(f"Подборка «{name}» создана ({kind}).")


@router.callback_query(F.data == "adm:cancel", IsAdmin())
async def cb_admin_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer("Отменено")
    await callback.message.answer("Отменено.", reply_markup=menu_keyboard())


async def _settings_view():
    rows = await db.fetchall("SELECT key, value FROM settings")
    values = {row["key"]: row["value"] for row in rows}
    lines = ["⚙️ Настройки", ""]
    keyboard = []
    for key, label, kind in SETTINGS:
        value = values.get(key, "")
        if kind == "bool":
            shown = "вкл" if value == "1" else "выкл"
        else:
            shown = str(value)
            if len(shown) > 40:
                shown = shown[:37] + "..."
        lines.append(f"{label}: {shown}")
        keyboard.append([_btn(f"✏️ {label}", f"st:{key}")])
    keyboard.append([_btn("⬅️ Назад", "adm:menu")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=keyboard)


@router.callback_query(F.data == "adm:set", IsAdmin())
async def cb_settings(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    text, keyboard = await _settings_view()
    await callback.message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("st:"), IsAdmin())
async def cb_setting(callback: CallbackQuery, state: FSMContext):
    key = callback.data.split(":", 1)[1]
    meta = SETTINGS_MAP.get(key)
    if meta is None:
        await callback.answer("Неизвестная настройка", show_alert=True)
        return
    _, label, kind = meta

    if kind == "bool":
        current = await db.get_bool_setting(key)
        await db.set_setting(key, "0" if current else "1")
        await callback.answer("Переключено")
        text, keyboard = await _settings_view()
        try:
            await callback.message.edit_text(text, reply_markup=keyboard)
        except TelegramBadRequest:
            await callback.message.answer(text, reply_markup=keyboard)
        return

    await callback.answer()
    await state.set_state(SettingsEdit.value)
    await state.update_data(key=key)
    hint = "число" if kind == "int" else "текст"
    await callback.message.answer(
        f"Новое значение для «{label}» ({hint}):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("✖️ Отмена", "adm:cancel")]]),
    )


@router.message(SettingsEdit.value, IsAdmin())
async def on_setting_value(message: Message, state: FSMContext):
    data = await state.get_data()
    key = data.get("key")
    meta = SETTINGS_MAP.get(key)
    if meta is None:
        await state.clear()
        return
    _, label, kind = meta
    raw = (message.text or "").strip()
    if kind == "int":
        if not raw.isdigit():
            await message.answer("Нужно неотрицательное число. Ещё раз.")
            return
    elif kind == "text":
        if not raw:
            await message.answer("Текст пустой. Ещё раз.")
            return
    await db.set_setting(key, raw)
    await state.clear()
    await message.answer(f"«{label}» обновлено.", reply_markup=menu_keyboard())
