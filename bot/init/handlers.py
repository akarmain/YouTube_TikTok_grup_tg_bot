from aiogram import Dispatcher
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message


def register_handlers(dp: Dispatcher):
    dp.message.register(cmd_start, CommandStart())


async def cmd_start(msg: Message, state: FSMContext) -> None:
    await state.clear()
    await msg.answer(
        "👋 Привет!\n\nЯ бот для скачивания коротких видео с YouTube и TikTok. Просто добавь меня в группу с друзьями, и я буду автоматически сохранять видео по ссылкам, которые вы отправляете. 🎥")
