import os

import loguru
from aiogram import Dispatcher, F
from aiogram.types import FSInputFile, Message

from bot.youTube.sourse import (
    VideoFormatError,
    download_video,
    get_best_video_format,
    get_tiktok_video_dimensions,
)

YOUTUBE_URL_PATTERN = r'^https?://(?:www\.)?(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)[\w-]+'


def register_handlers(dp: Dispatcher):
    dp.message.register(get_youtube, F.text.regexp(YOUTUBE_URL_PATTERN))


async def get_youtube(msg: Message):
	
    temp_msg = await msg.reply("Скачиваю видео ⌛️")
    
    url = msg.text
    file_path = None

    try:
        format_info = await get_best_video_format(url)
        video_dimensions = await get_tiktok_video_dimensions(url)
        file_path = await download_video(url, format_info)
        await msg.answer_video(
            video=FSInputFile(file_path),
            supports_streaming=True,
            width=video_dimensions["width"],
            height=video_dimensions["height"],
        )
        await temp_msg.delete()
    except VideoFormatError as e:
        await temp_msg.edit_text(f"Не удалось скачать видео: {e}")
    except Exception as e:
        loguru.logger.exception(e)
        await temp_msg.edit_text("Произошла ошибка при скачивании видео.")
    finally:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
