from aiogram import Dispatcher

from bot.tiktok.handlers import register_handlers


def register_tiktok(dp: Dispatcher) -> None:
    register_handlers(dp)
