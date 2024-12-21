import os
import shutil

from aiogram import Dispatcher
from aiogram.client.bot import DefaultBotProperties, Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types.bot_command import BotCommand
from loguru import logger

from bot.init import register_init
from bot.settings import Env, BOT_NAME, ALL_COMMANDS, BASIC_DIR
from bot.youTube import register_youtube


async def in_start(bot: Bot):
    commands = [
        BotCommand(command=name_cmd, description=desc)
        for name_cmd, desc in ALL_COMMANDS.items()
    ]
    await bot.set_my_commands(commands)
    cache_dir = f"{BASIC_DIR}/cache/"
    if os.path.exists(cache_dir):
        shutil.rmtree(cache_dir)
        os.makedirs(cache_dir)
    logger.info(f"Aiogram START bot: @{BOT_NAME}")


async def in_stop():
    logger.info(f"Aiogram STOP  bot: @{BOT_NAME}")


async def start_bot():
    bot = Bot(token=Env.TG_MAIN_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    dp = Dispatcher(storage=MemoryStorage())

    register_init(dp)
    register_youtube(dp)

    dp.startup.register(in_start)
    dp.shutdown.register(in_stop)
    try:
        await dp.start_polling(bot)
    except TelegramNetworkError:
        logger.warning("Network Error")
    finally:
        await bot.session.close()
