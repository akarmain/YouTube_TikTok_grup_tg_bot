"""Central URL routing: one handler detects the platform and dispatches to its downloader."""

import asyncio
import os
from contextlib import suppress
from urllib.parse import urlparse

import loguru
from aiogram import Dispatcher
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, Message

from bot.database.json_db import json_db
from bot.downloader.media import MediaResult, VideoTooLargeError
from bot.downloader.video_tools import compress_to_limit
from bot.instagram.source import download_instagram
from bot.settings import MAX_VIDEO_SIZE_BYTES, MAX_VIDEO_SIZE_MB
from bot.tiktok.sourse import download_tiktok
from bot.youTube.sourse import download_youtube

_PLATFORM_DOMAINS = (
    ("tiktok.com", "tiktok"),
    ("youtube.com", "youtube"),
    ("youtu.be", "youtube"),
    ("instagram.com", "instagram"),
)

DOWNLOADERS = {
    "tiktok": download_tiktok,
    "youtube": download_youtube,
    "instagram": download_instagram,
}


def detect_platform(url: str) -> str | None:
    try:
        parsed = urlparse((url or "").strip())
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    host = parsed.netloc.rsplit("@", 1)[-1].split(":")[0].lower()
    for domain, platform in _PLATFORM_DOMAINS:
        if host == domain or host.endswith("." + domain):
            return platform
    return None


def register_handlers(dp: Dispatcher) -> None:
    dp.message.register(handle_media_url, _has_supported_url)
    dp.channel_post.register(handle_media_url, _has_supported_url)


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
        if detect_platform(candidate):
            return candidate
    return None


def _has_supported_url(message: Message) -> bool:
    return _extract_first_supported_url(message) is not None


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


async def _ensure_size_limit(result: MediaResult) -> None:
    if not result.path or os.path.getsize(result.path) <= MAX_VIDEO_SIZE_BYTES:
        return
    fitted = await asyncio.to_thread(compress_to_limit, result.path, MAX_VIDEO_SIZE_BYTES)
    if not fitted:
        raise VideoTooLargeError(
            f"Video is still over {MAX_VIDEO_SIZE_MB} MB after compression."
        )
    result.path = fitted


async def handle_media_url(msg: Message):
    source_url = _extract_first_supported_url(msg)
    platform = detect_platform(source_url) if source_url else None
    if not source_url or not platform:
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
                    platform=platform,
                )
            return
        except TelegramBadRequest:
            await json_db.invalidate_cached_file_id(source_url)
        except Exception as e:
            loguru.logger.exception(e)

    temp_msg: Message | None = None
    if not _is_channel_message(msg):
        temp_msg = await msg.answer("Скачиваю видео ⌛️")

    result: MediaResult | None = None
    try:
        result = await DOWNLOADERS[platform](source_url)
        await _ensure_size_limit(result)
        sent_msg = await _send_video(
            msg,
            video=FSInputFile(result.path),
            width=result.width,
            height=result.height,
        )
        if sent_msg.video:
            await json_db.upsert_video(
                source_url=source_url,
                file_id=sent_msg.video.file_id,
                sender_user_id=msg.from_user.id if msg.from_user else None,
                platform=platform,
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
        if result and result.path and os.path.exists(result.path):
            os.remove(result.path)
