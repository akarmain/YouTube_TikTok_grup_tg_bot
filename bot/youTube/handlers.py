import os
from contextlib import suppress
from urllib.parse import urlparse

import loguru
from aiogram import Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, Message

from bot.database.json_db import json_db
from bot.youTube.sourse import SUPPORTED_URL_PATTERN, DownloadedVideo, VideoTooLargeError, download_best_video


def register_handlers(dp: Dispatcher):
    dp.message.register(handle_video_url, F.text.regexp(SUPPORTED_URL_PATTERN))


def _detect_platform(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if "tiktok.com" in host:
        return "tiktok"
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube"
    return "unknown"


async def handle_video_url(msg: Message):
    source_url = (msg.text or "").strip()
    cached_file_id = await json_db.get_cached_file_id(source_url)
    if cached_file_id:
        try:
            sent_msg = await msg.answer_video(video=cached_file_id, supports_streaming=True)
            if sent_msg.video:
                await json_db.upsert_video(
                    source_url=source_url,
                    file_id=sent_msg.video.file_id,
                    sender_user_id=msg.from_user.id if msg.from_user else None,
                    platform=_detect_platform(source_url),
                )
            return
        except TelegramBadRequest:
            await json_db.invalidate_cached_file_id(source_url)
        except Exception as e:
            loguru.logger.exception(e)

    temp_msg = await msg.answer("Скачиваю видео ⌛️")
    downloaded: DownloadedVideo | None = None
    try:
        downloaded = await download_best_video(source_url)
        sent_msg = await msg.answer_video(
            video=FSInputFile(downloaded.path),
            supports_streaming=True,
            width=downloaded.width,
            height=downloaded.height,
        )
        if sent_msg.video:
            await json_db.upsert_video(
                source_url=source_url,
                file_id=sent_msg.video.file_id,
                sender_user_id=msg.from_user.id if msg.from_user else None,
                platform=_detect_platform(source_url),
            )
    except VideoTooLargeError as e:
        await msg.answer(f"Видео слишком большое для обработки. Напиши @akarmain чтобы исправить это: {e}")
    except Exception as e:
        loguru.logger.exception(e)
        await msg.answer("Не удалось скачать видео. Проверьте ссылку и попробуйте снова.")
    finally:
        with suppress(Exception):
            await temp_msg.delete()
        if downloaded and os.path.exists(downloaded.path):
            os.remove(downloaded.path)
