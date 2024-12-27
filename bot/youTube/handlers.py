import os

import loguru
from aiogram import Dispatcher, F
from aiogram.types import FSInputFile, Message

from bot.youTube.sourse import get_best_video_format, download_video, get_best_tiktok_video_format, download_tiktok_video, get_tiktok_video_dimensions


def register_handlers(dp: Dispatcher):
    dp.message.register(get_youtube, F.text.regexp(r'^https?://(?:www\.)?youtube\.com/shorts/[\w-]+'))
    dp.message.register(get_tiktok, F.text.regexp(r'^https?://(?:www\.)?tiktok\.com/[\w@/.-]+'))


async def get_youtube(msg: Message):
    temp_msg = await msg.answer("Скачиваю видео ⌛️")
    url = msg.text
    t = await get_best_video_format(url)
    video_dimensions = await get_tiktok_video_dimensions(url)
    file_path = await download_video(url, t)
    try:
        await msg.answer_video(video=FSInputFile(file_path), supports_streaming=True, width=video_dimensions["width"],
                               height=video_dimensions["height"])
    except Exception as e:
        loguru.logger.exception(e)
    await temp_msg.delete()
    os.remove(file_path)


async def get_tiktok(msg: Message):
    temp_msg = await msg.answer("Скачиваю видео ⌛️")
    url = msg.text
    t = await get_best_tiktok_video_format(url)
    video_dimensions = await get_tiktok_video_dimensions(url)
    file_path = await download_tiktok_video(url, t)
    try:
        await msg.answer_video(video=FSInputFile(file_path), supports_streaming=True, width=video_dimensions["width"],
                               height=video_dimensions["height"])
    except Exception as e:
        loguru.logger.exception(e)
    await temp_msg.delete()
    os.remove(file_path)
