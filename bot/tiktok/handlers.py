import os

from loguru import logger
from aiogram import Dispatcher, F
from aiogram.types import FSInputFile, Message

from bot.tiktok.sourse import TikTokDownloadError, download_tiktok_media

TIKTOK_URL_PATTERN = r"^https?://(?:www\.)?(?:m\.)?(?:vm\.)?tiktok\.com/[^\s]+"


def register_handlers(dp: Dispatcher):
    dp.message.register(get_tiktok, F.text.regexp(TIKTOK_URL_PATTERN))


async def get_tiktok(msg: Message):
    temp_msg = await msg.answer("Скачиваю материалы ⌛️")
    url = msg.text.strip()
    media = None

    try:
        media = await download_tiktok_media(url)

        caption_parts = []
        if media.title:
            caption_parts.append(f"<b>{media.title}</b>")
        if media.description:
            caption_parts.append(media.description)
        caption = "\n".join(caption_parts) or None

        if media.video_path:
            await msg.answer_video(
                video=FSInputFile(media.video_path),
                supports_streaming=True,
                width=media.width,
                height=media.height,
                caption=caption,
            )

        if media.audio_path:
            await msg.answer_audio(audio=FSInputFile(media.audio_path), caption="Оригинальный звук")

        await temp_msg.delete()
    except TikTokDownloadError as e:
        await temp_msg.edit_text(f"Не удалось скачать материалы из TikTok: {e}")
    except Exception:
        logger.exception("Unexpected error while downloading TikTok media")
        await temp_msg.edit_text("Произошла ошибка при скачивании из TikTok.")
    finally:
        if media:
            for path in [media.video_path, media.audio_path, media.cover_path]:
                if path and os.path.exists(path):
                    os.remove(path)
