from aiogram import Dispatcher

from bot.init.handlers import register_handlers


def register_init(dp: Dispatcher) -> None:
    register_handlers(dp)
