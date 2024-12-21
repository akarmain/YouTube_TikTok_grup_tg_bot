from aiogram import Dispatcher

from bot.youTube.handlers import register_handlers


def register_youtube(dp: Dispatcher) -> None:
    register_handlers(dp)
