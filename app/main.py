import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, ErrorEvent

from app import config, db
from app.handlers import admin, moderation, user
from app.scheduler import scheduler_loop
from app.services import link_download

log = logging.getLogger(__name__)


async def resolve_bot(bot: Bot):
    me = await bot.get_me()
    config.BOT_USERNAME = me.username or ""
    log.info("bot: @%s", config.BOT_USERNAME)

    if config.CHANNEL_CHAT is None:
        log.error("CHANNEL_ID is not configured")
        return
    try:
        chat = await bot.get_chat(config.CHANNEL_CHAT)
        config.CHANNEL_NUMERIC_ID = chat.id
        config.CHANNEL_USERNAME = chat.username
        config.CHANNEL_CHAT = chat.id
        log.info("channel: %s (%s)", chat.title, chat.id)
    except Exception as error:  # noqa: BLE001
        log.error("cannot resolve channel %s: %s", config.CHANNEL_CHAT, error)

    if config.ADMIN_CHAT is not None:
        try:
            admin_chat = await bot.get_chat(config.ADMIN_CHAT)
            config.ADMIN_CHAT = admin_chat.id
            config.ADMIN_CHAT_TYPE = admin_chat.type
            log.info("admin chat: %s (%s, %s)", admin_chat.title, admin_chat.id, admin_chat.type)
        except Exception as error:  # noqa: BLE001
            log.error("cannot resolve admin chat %s: %s", config.ADMIN_CHAT, error)


async def setup_commands(bot: Bot):
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Меню"),
            BotCommand(command="menu", description="Меню"),
            BotCommand(command="admin", description="Админка"),
        ])
    except Exception as error:  # noqa: BLE001
        log.warning("cannot set bot commands: %s", error)


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)

    if not config.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is not set (check .env)")

    await db.init()
    link_download.cleanup_stale()
    await db.execute("DELETE FROM admin_sessions WHERE updated_at < datetime('now', '-1 day')")

    bot = Bot(
        config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode="HTML", link_preview_is_disabled=True),
    )
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(admin.router)
    dispatcher.include_router(moderation.router)
    dispatcher.include_router(user.router)

    await resolve_bot(bot)
    await setup_commands(bot)

    @dispatcher.errors()
    async def on_error(event: ErrorEvent):
        if isinstance(event.exception, TelegramBadRequest):
            if "query is too old" in str(event.exception):
                log.warning("stale callback query ignored")
                return True
        log.error("unhandled error: %s", event.exception, exc_info=event.exception)
        return True

    scheduler = asyncio.create_task(scheduler_loop(bot))
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        log.info("starting polling")
        await dispatcher.start_polling(bot)
    finally:
        scheduler.cancel()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
