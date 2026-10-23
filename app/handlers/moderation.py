import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app import config, db, sessions
from app.services import collections as collections_service
from app.services import publishing
from app.keyboards import schedule_keyboard
from app.utils import esc, format_local, now_iso, parse_user_datetime, preset_datetime

log = logging.getLogger(__name__)

router = Router(name="moderation")


def is_admin(user_id: int) -> bool:
    return config.is_admin(user_id)


async def _deny(callback: CallbackQuery) -> bool:
    if is_admin(callback.from_user.id):
        return False
    await callback.answer("Нет доступа", show_alert=True)
    return True


def _btn(text, data=None, url=None):
    if url:
        return InlineKeyboardButton(text=text, url=url)
    return InlineKeyboardButton(text=text, callback_data=data)


cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[_btn("✖️ Отмена", "flow:cancel")]])


def _normalize(text) -> str:
    value = (text or "").strip()
    if value.startswith("/") and "@" in value:
        value = value.split("@", 1)[0]
    return value


def _group_mode() -> bool:
    return config.ADMIN_CHAT_TYPE in ("group", "supergroup")


async def _dm(bot: Bot, user_id: int, text: str, keyboard=None, reply_to=None, mention=None) -> bool:
    chat_id = config.ADMIN_CHAT if _group_mode() else user_id
    kwargs = {}
    if chat_id != user_id:
        if reply_to:
            kwargs["reply_to_message_id"] = reply_to
        if mention:
            text = f"{mention}, {text}"
    try:
        await bot.send_message(chat_id, text, reply_markup=keyboard, **kwargs)
        return True
    except TelegramForbiddenError:
        return False
    except TelegramBadRequest as error:
        log.warning("cannot send admin prompt to %s: %s", chat_id, error)
        return False


class SessionStep(BaseFilter):
    def __init__(self, *steps):
        self.steps = set(steps)

    async def __call__(self, message: Message) -> bool:
        if message.from_user is None:
            return False
        if message.chat.type not in ("private", "group", "supergroup"):
            return False
        if message.chat.type != "private" and message.chat.id != config.ADMIN_CHAT:
            return False
        if not is_admin(message.from_user.id):
            return False
        session = await sessions.get_session(message.from_user.id)
        return bool(session) and session.get("step") in self.steps


@router.callback_query(F.data.startswith("mod:"))
async def card_action(callback: CallbackQuery, bot: Bot):
    if await _deny(callback):
        return
    try:
        _, action, raw_id = callback.data.split(":")
        submission_id = int(raw_id)
    except ValueError:
        await callback.answer("Некорректная кнопка", show_alert=True)
        return

    submission = await db.fetchone("SELECT * FROM submissions WHERE id = ?", (submission_id,))
    if submission is None:
        await callback.answer("Заявка не найдена", show_alert=True)
        return
    if submission["status"] != "pending":
        await callback.answer("Заявка уже обработана", show_alert=True)
        return

    admin_id = callback.from_user.id

    if action == "pub":
        await sessions.set_session(admin_id, step="caption", submission_id=submission_id,
                                   collection_id=None, season=None, episode=None, caption=None)
        ok = await _dm(bot, admin_id,
                       f"Заявка #{submission_id}. Пришли текст подписи к посту или /skip.",
                       cancel_kb, reply_to=callback.message.message_id,
                       mention=callback.from_user.mention_html())
        if not ok:
            await callback.answer("Открой бота в личке и нажми /start", show_alert=True)
        else:
            await callback.answer()
        return

    if action == "rej":
        await sessions.set_session(admin_id, step="reject", submission_id=submission_id)
        ok = await _dm(bot, admin_id,
                       f"Заявка #{submission_id}. Причина отклонения (текст) или /skip.",
                       cancel_kb, reply_to=callback.message.message_id,
                       mention=callback.from_user.mention_html())
        if not ok:
            await callback.answer("Открой бота в личке и нажми /start", show_alert=True)
        else:
            await callback.answer()
        return

    if action == "col":
        await sessions.set_session(admin_id, step="pick", submission_id=submission_id)
        rows = await db.fetchall("SELECT * FROM collections ORDER BY position, id")
        keyboard = []
        for row in rows:
            mark = "📺" if row["is_ordered"] else "📁"
            keyboard.append([_btn(f"{mark} {row['name']}", f"pick:{submission_id}:{row['id']}")])
        keyboard.append([_btn("➕ Новая подборка", f"picknew:{submission_id}")])
        keyboard.append([_btn("✖️ Отмена", "flow:cancel")])
        ok = await _dm(bot, admin_id, f"Заявка #{submission_id}. В какую подборку?",
                       InlineKeyboardMarkup(inline_keyboard=keyboard),
                       reply_to=callback.message.message_id,
                       mention=callback.from_user.mention_html())
        if not ok:
            await callback.answer("Открой бота в личке и нажми /start", show_alert=True)
        else:
            await callback.answer()
        return

    await callback.answer()


@router.callback_query(F.data.startswith("pick:"))
async def pick_collection(callback: CallbackQuery, bot: Bot):
    if await _deny(callback):
        return
    try:
        _, raw_submission, raw_collection = callback.data.split(":")
        submission_id, collection_id = int(raw_submission), int(raw_collection)
    except ValueError:
        await callback.answer("Некорректная кнопка", show_alert=True)
        return

    collection = await db.fetchone("SELECT * FROM collections WHERE id = ?", (collection_id,))
    if collection is None:
        await callback.answer("Подборка не найдена", show_alert=True)
        return

    admin_id = callback.from_user.id
    season = int(collection["season"])
    if collection["is_ordered"]:
        episode = await collections_service.next_episode(collection_id, season)
        await sessions.set_session(admin_id, step="episode_confirm", submission_id=submission_id,
                                   collection_id=collection_id, season=season, episode=episode,
                                   caption=None)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [_btn("✅ Ок", "ep:ok"), _btn("✏️ Изменить номер", "ep:edit")],
            [_btn("✖️ Отмена", "flow:cancel")],
        ])
        await callback.answer()
        await _dm(bot, admin_id,
                  f"Подборка «{collection['name']}».\nСледующая серия: S{season}E{episode}.",
                  keyboard)
        return

    await sessions.set_session(admin_id, step="caption", submission_id=submission_id,
                               collection_id=collection_id, season=None, episode=None, caption=None)
    await callback.answer()
    await _dm(bot, admin_id,
              f"Подборка «{collection['name']}». Пришли текст подписи или /skip.",
              cancel_kb)


@router.callback_query(F.data.startswith("picknew:"))
async def pick_new_collection(callback: CallbackQuery, bot: Bot):
    if await _deny(callback):
        return
    try:
        _, raw_submission = callback.data.split(":")
        submission_id = int(raw_submission)
    except ValueError:
        await callback.answer("Некорректная кнопка", show_alert=True)
        return
    admin_id = callback.from_user.id
    await sessions.set_session(admin_id, step="newcol_name", submission_id=submission_id)
    await callback.answer()
    await _dm(bot, admin_id, "Название новой подборки:", cancel_kb)


@router.callback_query(F.data.startswith("newcol:type:"))
async def new_collection_type(callback: CallbackQuery, bot: Bot):
    if await _deny(callback):
        return
    session = await sessions.get_session(callback.from_user.id)
    if not session or session.get("step") != "newcol_type":
        await callback.answer("Сессия устарела, начни заново", show_alert=True)
        return
    is_ordered = callback.data.endswith(":1")
    name = session.get("new_name") or "Без названия"
    collection_id = await collections_service.create_collection(name, is_ordered)
    admin_id = callback.from_user.id
    submission_id = int(session["submission_id"])

    if is_ordered:
        season = 1
        episode = await collections_service.next_episode(collection_id, season)
        await sessions.set_session(admin_id, step="episode_confirm", submission_id=submission_id,
                                   collection_id=collection_id, season=season, episode=episode,
                                   caption=None)
        await callback.answer()
        await _dm(bot, admin_id,
                  f"Подборка создана. Первая серия: S{season}E{episode}.",
                  InlineKeyboardMarkup(inline_keyboard=[
                      [_btn("✅ Ок", "ep:ok"), _btn("✏️ Изменить номер", "ep:edit")],
                      [_btn("✖️ Отмена", "flow:cancel")],
                  ]))
        return

    await sessions.set_session(admin_id, step="caption", submission_id=submission_id,
                               collection_id=collection_id, season=None, episode=None, caption=None)
    await callback.answer()
    await _dm(bot, admin_id, "Подборка создана. Пришли текст подписи или /skip.", cancel_kb)


@router.callback_query(F.data == "ep:ok")
async def episode_ok(callback: CallbackQuery, bot: Bot):
    if await _deny(callback):
        return
    session = await sessions.get_session(callback.from_user.id)
    if not session:
        await callback.answer("Сессия устарела, начни заново", show_alert=True)
        return
    await callback.answer()
    await _ask_caption(bot, callback.from_user.id, session)


@router.callback_query(F.data == "ep:edit")
async def episode_edit(callback: CallbackQuery, bot: Bot):
    if await _deny(callback):
        return
    if not await sessions.get_session(callback.from_user.id):
        await callback.answer("Сессия устарела, начни заново", show_alert=True)
        return
    await sessions.update_session(callback.from_user.id, step="episode")
    await callback.answer()
    await _dm(bot, callback.from_user.id, "Введи номер серии числом:", cancel_kb)


async def _ask_caption(bot: Bot, admin_id: int, session: dict):
    await sessions.update_session(admin_id, step="caption")
    await _dm(bot, admin_id, "Пришли текст подписи или /skip.", cancel_kb)


@router.message(SessionStep("episode"))
async def on_episode_number(message: Message, bot: Bot):
    session = await sessions.get_session(message.from_user.id)
    if not session:
        return
    try:
        episode = int(message.text.strip())
        if episode < 1:
            raise ValueError
    except (TypeError, ValueError):
        await message.answer("Нужно положительное число. Попробуй ещё раз.")
        return
    await sessions.update_session(message.from_user.id, episode=episode)
    session = await sessions.get_session(message.from_user.id)
    await _ask_caption(bot, message.from_user.id, session)


@router.message(SessionStep("newcol_name"))
async def on_new_collection_name(message: Message, bot: Bot):
    name = (message.text or "").strip()
    if name.startswith("/") or len(name) < 2:
        await message.answer("Пришли название подборки текстом.")
        return
    await sessions.update_session(message.from_user.id, step="newcol_type", new_name=name[:80])
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [_btn("📺 Сериал (с номерами серий)", "newcol:type:1")],
        [_btn("📁 Папка (без номеров)", "newcol:type:0")],
        [_btn("✖️ Отмена", "flow:cancel")],
    ])
    await message.answer("Тип подборки?", reply_markup=keyboard)


@router.message(SessionStep("caption"))
async def on_caption(message: Message, bot: Bot):
    if not message.text:
        await message.answer("Нужен текст подписи или /skip.")
        return
    text = _normalize(message.text)
    if text == "/skip":
        caption = None
    elif text.startswith("/"):
        await message.answer("Пришли текст подписи или /skip.")
        return
    else:
        caption = text[:900]
    await sessions.update_session(message.from_user.id, step="when", caption=caption)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [_btn("🚀 Сейчас", "when:now"), _btn("🕓 Отложить", "when:later")],
        [_btn("✖️ Отмена", "flow:cancel")],
    ])
    await message.answer("Когда публикуем?", reply_markup=keyboard)


@router.callback_query(F.data == "when:now")
async def when_now(callback: CallbackQuery, bot: Bot):
    if await _deny(callback):
        return
    session = await sessions.get_session(callback.from_user.id)
    if not session:
        await callback.answer("Сессия устарела, начни заново", show_alert=True)
        return
    await callback.answer("Публикую...")
    admin_id = callback.from_user.id
    await sessions.clear_session(admin_id)
    try:
        result = await publishing.publish_submission(
            bot,
            submission_id=int(session["submission_id"]),
            caption_text=session.get("caption"),
            collection_id=session.get("collection_id"),
            season=session.get("season"),
            episode=session.get("episode"),
            reviewer_id=admin_id,
        )
    except Exception as error:  # noqa: BLE001
        log.exception("publish failed")
        await _dm(bot, admin_id, f"⚠️ Не удалось опубликовать: {esc(error)}")
        return
    await _dm(bot, admin_id, f"✅ Опубликовано: {result['link']}")


@router.callback_query(F.data == "when:later")
async def when_later(callback: CallbackQuery, bot: Bot):
    if await _deny(callback):
        return
    session = await sessions.get_session(callback.from_user.id)
    if not session or session.get("step") != "when":
        await callback.answer("Сессия устарела, начни заново", show_alert=True)
        return
    await callback.answer()
    await _dm(bot, callback.from_user.id, "Когда публиковать?", schedule_keyboard())


@router.callback_query(F.data.startswith("sched:"))
async def schedule_preset(callback: CallbackQuery, bot: Bot):
    if await _deny(callback):
        return
    admin_id = callback.from_user.id
    session = await sessions.get_session(admin_id)
    if not session or session.get("step") not in ("when", "sched_when"):
        await callback.answer("Сессия устарела, начни заново", show_alert=True)
        return

    value = callback.data.split(":", 1)[1]
    if value == "manual":
        step = "time" if session.get("step") == "when" else "sched_time"
        await sessions.update_session(admin_id, step=step)
        await callback.answer()
        await _dm(
            bot,
            admin_id,
            "Пришли время: `через 2 часа`, `завтра 10:00`, `сегодня в 19`, "
            "`25.10 18:00`, `+30m`.\n"
            f"Таймзона: {config.TIMEZONE}.",
            cancel_kb,
        )
        return

    publish_at = preset_datetime(value)
    if not publish_at:
        await callback.answer("Не понял время", show_alert=True)
        return

    if session.get("step") == "when":
        await sessions.clear_session(admin_id)
        try:
            await publishing.enqueue(
                bot,
                submission_id=int(session["submission_id"]),
                publish_at=publish_at,
                caption_text=session.get("caption"),
                collection_id=session.get("collection_id"),
                season=session.get("season"),
                episode=session.get("episode"),
                reviewer_id=admin_id,
            )
        except Exception as error:  # noqa: BLE001
            log.exception("enqueue failed")
            await callback.answer("Ошибка", show_alert=True)
            await _dm(bot, admin_id, f"⚠️ Не удалось запланировать: {esc(error)}")
            return
        await callback.answer("Запланировано")
        await _dm(bot, admin_id, f"🕓 Запланировано на {format_local(publish_at)}.")
        return

    queue_id = int(session["queue_id"])
    row = await db.fetchone("SELECT * FROM queue WHERE id = ? AND status = 'waiting'", (queue_id,))
    if row is None:
        await sessions.clear_session(admin_id)
        await callback.answer("Запись уже неактуальна", show_alert=True)
        return
    await db.execute("UPDATE queue SET publish_at = ? WHERE id = ?", (publish_at, queue_id))
    await sessions.clear_session(admin_id)
    await callback.answer("Время изменено")
    await _dm(bot, admin_id, f"🕓 Новое время: {format_local(publish_at)}.")


@router.message(SessionStep("time"))
async def on_time(message: Message, bot: Bot):
    publish_at = parse_user_datetime(message.text or "")
    if publish_at is None:
        await message.answer("Не понял время. Пример: `14:30`, `25.10 18:00`, `+2h`.", parse_mode="Markdown")
        return
    session = await sessions.get_session(message.from_user.id)
    if not session:
        return
    admin_id = message.from_user.id
    await sessions.clear_session(admin_id)
    try:
        await publishing.enqueue(
            bot,
            submission_id=int(session["submission_id"]),
            publish_at=publish_at,
            caption_text=session.get("caption"),
            collection_id=session.get("collection_id"),
            season=session.get("season"),
            episode=session.get("episode"),
            reviewer_id=admin_id,
        )
    except Exception as error:  # noqa: BLE001
        log.exception("enqueue failed")
        await message.answer(f"⚠️ Не удалось запланировать: {esc(error)}")
        return
    await message.answer(f"🕓 Запланировано на {format_local(publish_at)}.")


@router.message(SessionStep("sched_time"))
async def on_reschedule_time(message: Message, bot: Bot):
    publish_at = parse_user_datetime(message.text or "")
    if publish_at is None:
        await message.answer(
            "Не понял время. Примеры: `через 2 часа`, `завтра 10:00`, `25.10 18:00`, `+30m`.",
            parse_mode="Markdown",
        )
        return
    session = await sessions.get_session(message.from_user.id)
    if not session:
        return
    admin_id = message.from_user.id
    queue_id = int(session["queue_id"])
    await sessions.clear_session(admin_id)
    row = await db.fetchone("SELECT * FROM queue WHERE id = ? AND status = 'waiting'", (queue_id,))
    if row is None:
        await message.answer("Запись уже неактуальна.")
        return
    await db.execute("UPDATE queue SET publish_at = ? WHERE id = ?", (publish_at, queue_id))
    await message.answer(f"🕓 Новое время: {format_local(publish_at)}.")


@router.message(SessionStep("reject"))
async def on_reject(message: Message, bot: Bot):
    text = _normalize(message.text)
    reason = None if text == "/skip" else text[:300]
    session = await sessions.get_session(message.from_user.id)
    if not session:
        return
    admin_id = message.from_user.id
    await sessions.clear_session(admin_id)

    submission_id = int(session["submission_id"])
    submission = await db.fetchone("SELECT * FROM submissions WHERE id = ?", (submission_id,))
    if submission is None:
        await message.answer("Заявка не найдена.")
        return
    if submission["status"] != "pending":
        await message.answer("Заявка уже обработана.")
        return

    await db.execute(
        "UPDATE submissions SET status = 'rejected', reject_reason = ?, reviewed_at = ?, reviewer_id = ? "
        "WHERE id = ?",
        (reason, now_iso(), admin_id, submission_id),
    )
    await publishing.edit_card(bot, submission, "❌ Отклонено")
    try:
        notice = f"❌ Заявка #{submission_id} отклонена."
        if reason:
            notice += f"\nПричина: {reason}"
        await bot.send_message(submission["user_id"], notice, parse_mode=None)
    except (TelegramForbiddenError, TelegramBadRequest):
        pass
    await message.answer(f"Заявка #{submission_id} отклонена.")


@router.callback_query(F.data == "flow:cancel")
async def flow_cancel(callback: CallbackQuery):
    await sessions.clear_session(callback.from_user.id)
    await callback.answer("Отменено")
    await callback.message.answer("Отменено.")

@router.message(F.chat.id == config.ADMIN_CHAT, F.video | F.photo | F.document)
async def admin_direct_upload(message: Message, bot: Bot):
    if message.from_user is None or not is_admin(message.from_user.id):
        return
    if message.media_group_id:
        await message.answer("По одному файлу за раз, без альбомов.")
        return

    from app.handlers.user import extract_media  # локальный импорт, чтобы не плодить циклы

    media = extract_media(message)
    if media is None:
        await message.answer("Не понял файл. Нужно видео или фото.")
        return

    user = message.from_user
    await db.execute(
        "INSERT INTO users (id, username, first_name, created_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET username = excluded.username, first_name = excluded.first_name",
        (user.id, user.username, user.first_name, now_iso()),
    )
    cur = await db.execute(
        "INSERT INTO submissions (user_id, source_chat_id, source_message_id, media_type, file_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user.id, message.chat.id, message.message_id, media["type"], media["file_id"], now_iso()),
    )
    submission_id = cur.lastrowid

    media_title = "видео" if media["type"] == "video" else "фото"
    card = (
        f"📥 <b>Заявка #{submission_id}</b>\n"
        f"👤 {user.mention_html()}\n"
        f"📦 {media_title} (загружено в админ-чат)"
    )
    sent = await message.reply(card, reply_markup=publishing.card_keyboard(submission_id))
    await db.execute(
        "UPDATE submissions SET admin_chat_id = ?, admin_message_id = ?, card_caption = ? WHERE id = ?",
        (message.chat.id, sent.message_id, card, submission_id),
    )
    await db.execute("UPDATE users SET submissions_total = submissions_total + 1 WHERE id = ?", (user.id,))
