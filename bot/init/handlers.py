import os
from contextlib import suppress

from aiogram import Dispatcher
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import FSInputFile, Message

from bot.database.json_db import json_db
from bot.settings import ADMIN_ID


def register_handlers(dp: Dispatcher):
    dp.message.register(cmd_start, CommandStart())
    dp.message.register(cmd_send_db, Command("send_db"))


async def cmd_start(msg: Message, state: FSMContext) -> None:
    await state.clear()
    if msg.from_user:
        await json_db.upsert_user(
            user_id=msg.from_user.id,
            username=msg.from_user.username,
            first_name=msg.from_user.first_name,
            last_name=msg.from_user.last_name,
        )
    await msg.answer(
        "👋 Привет!\n\nЯ бот для скачивания коротких видео с YouTube и TikTok. Просто добавь меня в группу с друзьями, и я буду автоматически сохранять видео по ссылкам, которые вы отправляете. 🎥")


async def cmd_send_db(msg: Message) -> None:
    if not msg.from_user or msg.from_user.id != ADMIN_ID:
        await msg.answer("Команда доступна только администратору.")
        return

    export_path = await json_db.export_users_file()
    try:
        await msg.bot.send_document(
            chat_id=ADMIN_ID,
            document=FSInputFile(export_path),
            caption="Экспорт пользователей",
        )
    finally:
        with suppress(OSError):
            os.remove(export_path)

    if msg.chat.id != ADMIN_ID:
        await msg.answer("База пользователей отправлена вам в личные сообщения.")
