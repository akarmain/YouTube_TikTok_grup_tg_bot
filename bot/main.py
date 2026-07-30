import os
import shutil

from aiogram import Dispatcher
from aiogram.client.bot import DefaultBotProperties, Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types.bot_command import BotCommand
from loguru import logger

from bot.downloader.router import register_handlers as register_downloader
from bot.init import register_init
from bot.settings import ALL_COMMANDS, BOT_NAME, CACHE_DIR, Env, LOCAL_BOT_API_BASE_URL


def build_bot_session() -> AiohttpSession | None:
    if not LOCAL_BOT_API_BASE_URL:
        return None
    return AiohttpSession(api=TelegramAPIServer.from_base(LOCAL_BOT_API_BASE_URL, is_local=True))


def _reset_cache_dir(cache_dir: str) -> None:
    os.makedirs(cache_dir, exist_ok=True)
    with os.scandir(cache_dir) as entries:
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path)
            else:
                os.unlink(entry.path)


async def in_start(bot: Bot):
    commands = [
        BotCommand(command=name_cmd, description=desc)
        for name_cmd, desc in ALL_COMMANDS.items()
    ]
    await bot.set_my_commands(commands)
    _reset_cache_dir(CACHE_DIR)
    logger.info(f"Aiogram START bot: @{BOT_NAME}")


async def in_stop():
    logger.info(f"Aiogram STOP  bot: @{BOT_NAME}")


async def start_bot():
    bot = Bot(
        token=Env.TG_MAIN_BOT_TOKEN,
        session=build_bot_session(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    dp = Dispatcher(storage=MemoryStorage())

    register_init(dp)
    register_downloader(dp)

    dp.startup.register(in_start)
    dp.shutdown.register(in_stop)
    try:
        await dp.start_polling(bot)
    except TelegramNetworkError:
        logger.warning("Network Error")
    finally:
        await bot.session.close()
