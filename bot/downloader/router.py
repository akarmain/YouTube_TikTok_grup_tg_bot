"""Central URL routing: one handler detects the platform and dispatches to its downloader."""

import asyncio
import os
from contextlib import suppress
from dataclasses import dataclass
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
from bot.settings import DOWNLOAD_WORKERS, MAX_VIDEO_SIZE_BYTES, MAX_VIDEO_SIZE_MB
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

_QUEUE: asyncio.Queue | None = None
_WORKER_TASKS: list[asyncio.Task] = []
_ACTIVE_WORKERS = 0


@dataclass(slots=True)
class _QueueJob:
    msg: Message
    source_url: str
    platform: str
    status_msg: Message | None


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
    dp.startup.register(start_workers)
    dp.shutdown.register(stop_workers)


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


async def _safe_edit(status_msg: Message | None, text: str) -> None:
    if status_msg is None:
        return
    with suppress(Exception):
        await status_msg.edit_text(text)


def _queued_text(position: int) -> str:
    return f"⏳ В очереди, позиция: {position}"


def _processing_text() -> str:
    return "🔄 Обрабатываю..."


def _done_text() -> str:
    return "✅ Готово"


def _error_text(reason: str) -> str:
    return f"❌ {reason}"


async def _ensure_size_limit(result: MediaResult) -> None:
    if not result.path or os.path.getsize(result.path) <= MAX_VIDEO_SIZE_BYTES:
        return
    fitted = await asyncio.to_thread(compress_to_limit, result.path, MAX_VIDEO_SIZE_BYTES)
    if not fitted:
        raise VideoTooLargeError(
            f"Video is still over {MAX_VIDEO_SIZE_MB} MB after compression."
        )
    result.path = fitted


async def _process_job(job: _QueueJob) -> None:
    await _safe_edit(job.status_msg, _processing_text())

    cached_file_id = await json_db.get_cached_file_id(job.source_url)
    if cached_file_id:
        try:
            sent_msg = await _send_video(job.msg, video=cached_file_id)
            if sent_msg.video:
                await json_db.upsert_video(
                    source_url=job.source_url,
                    file_id=sent_msg.video.file_id,
                    sender_user_id=job.msg.from_user.id if job.msg.from_user else None,
                    platform=job.platform,
                )
            await _safe_edit(job.status_msg, _done_text())
            return
        except TelegramBadRequest:
            await json_db.invalidate_cached_file_id(job.source_url)
        except Exception as e:
            loguru.logger.exception(e)

    result: MediaResult | None = None
    try:
        result = await DOWNLOADERS[job.platform](job.source_url)
        await _ensure_size_limit(result)
        sent_msg = await _send_video(
            job.msg,
            video=FSInputFile(result.path),
            width=result.width,
            height=result.height,
        )
        if sent_msg.video:
            await json_db.upsert_video(
                source_url=job.source_url,
                file_id=sent_msg.video.file_id,
                sender_user_id=job.msg.from_user.id if job.msg.from_user else None,
                platform=job.platform,
            )
        await _safe_edit(job.status_msg, _done_text())
    except VideoTooLargeError as e:
        await _safe_edit(
            job.status_msg,
            _error_text(f"Видео слишком большое для обработки. Напиши @akarmain чтобы исправить это: {e}"),
        )
    except Exception as e:
        loguru.logger.exception(e)
        await _safe_edit(job.status_msg, _error_text("Не удалось скачать видео. Проверьте ссылку и попробуйте снова."))
    finally:
        if result and result.path and os.path.exists(result.path):
            os.remove(result.path)


async def _worker_loop() -> None:
    global _ACTIVE_WORKERS
    while True:
        job = await _QUEUE.get()
        _ACTIVE_WORKERS += 1
        try:
            await _process_job(job)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            loguru.logger.exception(e)
        finally:
            _ACTIVE_WORKERS -= 1
            _QUEUE.task_done()


async def start_workers() -> None:
    global _QUEUE, _ACTIVE_WORKERS
    if _WORKER_TASKS:
        return
    _QUEUE = asyncio.Queue()
    _ACTIVE_WORKERS = 0
    for _ in range(DOWNLOAD_WORKERS):
        _WORKER_TASKS.append(asyncio.create_task(_worker_loop()))


async def stop_workers() -> None:
    for task in _WORKER_TASKS:
        task.cancel()
    await asyncio.gather(*_WORKER_TASKS, return_exceptions=True)
    _WORKER_TASKS.clear()


async def handle_media_url(msg: Message):
    source_url = _extract_first_supported_url(msg)
    platform = detect_platform(source_url) if source_url else None
    if not source_url or not platform:
        return

    assert _QUEUE is not None, "start_workers() must run before the dispatcher accepts updates"
    idle_workers = len(_WORKER_TASKS) - _ACTIVE_WORKERS
    queued_ahead = _QUEUE.qsize()
    status_msg: Message | None = None
    if not _is_channel_message(msg):
        status_text = _processing_text() if idle_workers > queued_ahead else _queued_text(queued_ahead + 1)
        with suppress(Exception):
            status_msg = await msg.answer(status_text)

    await _QUEUE.put(
        _QueueJob(msg=msg, source_url=source_url, platform=platform, status_msg=status_msg)
    )
