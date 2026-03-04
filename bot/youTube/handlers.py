import os
from contextlib import suppress
from urllib.parse import urlparse

import loguru
from aiogram import Dispatcher
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, Message

from bot.database.json_db import json_db
from bot.youTube.sourse import DownloadedVideo, VideoTooLargeError, download_best_video, is_supported_video_url


def register_handlers(dp: Dispatcher):
    dp.message.register(handle_video_url, _has_supported_url)
    dp.channel_post.register(handle_video_url, _has_supported_url)


def _extract_first_supported_url(message: Message) -> str | None:
    text_blob = "\n".join(
        part.strip()
        for part in (message.text, message.caption)
        if part and part.strip()
    )
    if not text_blob:
        return None

    for raw in text_blob.split():
        candidate = raw.strip().strip("<>()[]{}\"'.,!?;:")
        if is_supported_video_url(candidate):
            return candidate
    return None


def _has_supported_url(message: Message) -> bool:
    return _extract_first_supported_url(message) is not None


def _detect_platform(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if "tiktok.com" in host:
        return "tiktok"
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube"
    return "unknown"


def _is_channel_message(msg: Message) -> bool:
    return msg.chat.type == ChatType.CHANNEL


async def _send_video(msg: Message, video: str | FSInputFile, width: int | None = None, height: int | None = None) -> Message:
    return await msg.bot.send_video(
        chat_id=msg.chat.id,
        video=video,
        supports_streaming=True,
        width=width,
        height=height,
    )


async def _safe_notify(msg: Message, text: str) -> None:
    with suppress(Exception):
        await msg.bot.send_message(chat_id=msg.chat.id, text=text)


async def handle_video_url(msg: Message):
    source_url = _extract_first_supported_url(msg)
    if not source_url:
        return

    cached_file_id = await json_db.get_cached_file_id(source_url)
    if cached_file_id:
        try:
            sent_msg = await _send_video(msg, video=cached_file_id)
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

    temp_msg: Message | None = None
    if not _is_channel_message(msg):
        temp_msg = await msg.answer("Скачиваю видео ⌛️")

    downloaded: DownloadedVideo | None = None
    try:
        downloaded = await download_best_video(source_url)
        sent_msg = await _send_video(
            msg,
            video=FSInputFile(downloaded.path),
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
        await _safe_notify(msg, f"Видео слишком большое для обработки. Напиши @akarmain чтобы исправить это: {e}")
    except Exception as e:
        loguru.logger.exception(e)
        await _safe_notify(msg, "Не удалось скачать видео. Проверьте ссылку и попробуйте снова.")
    finally:
        if temp_msg:
            with suppress(Exception):
                await temp_msg.delete()
        if downloaded and os.path.exists(downloaded.path):
            os.remove(downloaded.path)
