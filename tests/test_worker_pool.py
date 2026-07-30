"""Bounded download worker pool: queue/status UX, concurrency cap, auto ffmpeg concurrency.

Run: pytest tests/test_worker_pool.py
"""

import asyncio
import os
import tempfile

import pytest
import pytest_asyncio
from aiogram.enums import ChatType

os.environ.setdefault("TG_MAIN_BOT_TOKEN", "0:test")

from bot.downloader import router  # noqa: E402
from bot.downloader.media import MediaResult, VideoTooLargeError  # noqa: E402
from bot.settings import compute_ffmpeg_concurrency  # noqa: E402


class FakeVideo:
    def __init__(self, file_id: str):
        self.file_id = file_id


class FakeSentMessage:
    def __init__(self, video_file_id: str | None):
        self.video = FakeVideo(video_file_id) if video_file_id else None


class FakeBot:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_video(self, chat_id, video, supports_streaming, width, height):
        file_id = video if isinstance(video, str) else "NEWFILE"
        self.sent.append({"chat_id": chat_id, "video": video, "width": width, "height": height})
        return FakeSentMessage(video_file_id=file_id)


class FakeStatusMessage:
    def __init__(self):
        self.texts: list[str] = []

    async def edit_text(self, text: str) -> None:
        self.texts.append(text)


class FakeChat:
    def __init__(self, chat_id: int = 1, chat_type: ChatType = ChatType.PRIVATE):
        self.id = chat_id
        self.type = chat_type


class FakeUser:
    def __init__(self, user_id: int = 42):
        self.id = user_id


class FakeMessage:
    def __init__(
        self,
        bot: FakeBot,
        chat_type: ChatType = ChatType.PRIVATE,
        user_id: int = 42,
        text: str = "https://www.tiktok.com/@user/video/123456",
    ):
        self.bot = bot
        self.chat = FakeChat(chat_type=chat_type)
        self.from_user = FakeUser(user_id)
        self.text = text
        self.caption = None
        self.answers: list[str] = []

    async def answer(self, text: str) -> FakeStatusMessage:
        self.answers.append(text)
        status = FakeStatusMessage()
        return status


def _make_downloaded_file() -> str:
    handle = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    handle.write(b"x" * 1024)
    handle.close()
    return handle.name


class FakeJsonDB:
    def __init__(self):
        self.cache: dict[str, str] = {}
        self.upserts: list[tuple] = []

    async def get_cached_file_id(self, source_url: str) -> str | None:
        return self.cache.get(source_url)

    async def upsert_video(self, source_url, file_id, sender_user_id, platform=None) -> None:
        self.cache[source_url] = file_id
        self.upserts.append((source_url, file_id, sender_user_id, platform))

    async def invalidate_cached_file_id(self, source_url: str) -> None:
        self.cache.pop(source_url, None)


@pytest.fixture(autouse=True)
def fake_json_db(monkeypatch):
    fake = FakeJsonDB()
    monkeypatch.setattr(router, "json_db", fake)
    return fake


@pytest_asyncio.fixture(autouse=True)
async def clean_workers():
    yield
    await router.stop_workers()
    if router._QUEUE is not None:
        while not router._QUEUE.empty():
            router._QUEUE.get_nowait()
            router._QUEUE.task_done()
        router._QUEUE = None


def test_compute_ffmpeg_concurrency_auto_from_cpu_count():
    assert compute_ffmpeg_concurrency(cpu_count=8, explicit_max_jobs=None, explicit_threads=None) == (4, 2)
    assert compute_ffmpeg_concurrency(cpu_count=1, explicit_max_jobs=None, explicit_threads=None) == (1, 1)
    assert compute_ffmpeg_concurrency(cpu_count=5, explicit_max_jobs=None, explicit_threads=None) == (2, 2)


def test_compute_ffmpeg_concurrency_explicit_values_are_respected():
    assert compute_ffmpeg_concurrency(cpu_count=8, explicit_max_jobs=3, explicit_threads=7) == (3, 7)


def test_compute_ffmpeg_concurrency_zero_cpu_count_does_not_crash():
    assert compute_ffmpeg_concurrency(cpu_count=0, explicit_max_jobs=None, explicit_threads=None) == (1, 1)


def test_compute_ffmpeg_concurrency_none_cpu_count_does_not_crash():
    assert compute_ffmpeg_concurrency(cpu_count=None, explicit_max_jobs=None, explicit_threads=None) == (1, 1)


def test_compute_ffmpeg_concurrency_zero_or_negative_explicit_values_clamp_to_one():
    assert compute_ffmpeg_concurrency(cpu_count=8, explicit_max_jobs=0, explicit_threads=0) == (1, 1)
    assert compute_ffmpeg_concurrency(cpu_count=8, explicit_max_jobs=-5, explicit_threads=-5) == (1, 1)


def test_compute_ffmpeg_concurrency_zero_explicit_max_jobs_falls_back_threads_to_auto():
    assert compute_ffmpeg_concurrency(cpu_count=8, explicit_max_jobs=0, explicit_threads=None) == (1, 8)


@pytest.mark.asyncio
async def test_start_workers_is_idempotent(monkeypatch):
    monkeypatch.setattr(router, "DOWNLOAD_WORKERS", 3)
    await router.start_workers()
    tasks_after_first_start = list(router._WORKER_TASKS)
    await router.start_workers()
    assert router._WORKER_TASKS == tasks_after_first_start
    assert len(router._WORKER_TASKS) == 3


@pytest.mark.asyncio
async def test_stop_workers_without_start_is_a_noop():
    await router.stop_workers()
    assert router._WORKER_TASKS == []


@pytest.mark.asyncio
async def test_worker_pool_never_exceeds_configured_concurrency(monkeypatch):
    monkeypatch.setattr(router, "DOWNLOAD_WORKERS", 2)
    active = 0
    max_active = 0
    lock = asyncio.Lock()

    async def slow_downloader(url: str) -> MediaResult:
        nonlocal active, max_active
        async with lock:
            active += 1
            max_active = max(max_active, active)
        await asyncio.sleep(0.05)
        async with lock:
            active -= 1
        return MediaResult(platform="tiktok", source_url=url, media_type="video", path=_make_downloaded_file())

    monkeypatch.setitem(router.DOWNLOADERS, "tiktok", slow_downloader)
    await router.start_workers()

    bot = FakeBot()
    for i in range(6):
        msg = FakeMessage(bot)
        await router._QUEUE.put(
            router._QueueJob(msg=msg, source_url=f"https://tiktok.com/@u/video/{i}", platform="tiktok", status_msg=None)
        )
    await router._QUEUE.join()

    assert max_active == 2


@pytest.mark.asyncio
async def test_handle_media_url_shows_processing_when_queue_is_empty(monkeypatch):
    monkeypatch.setattr(router, "DOWNLOAD_WORKERS", 1)

    async def never_returns(url: str) -> MediaResult:
        await asyncio.sleep(10)
        raise AssertionError("should not be reached in this test")

    monkeypatch.setitem(router.DOWNLOADERS, "tiktok", never_returns)
    await router.start_workers()

    bot = FakeBot()
    msg = FakeMessage(bot)
    await router.handle_media_url(msg)
    assert msg.answers == ["🔄 Обрабатываю..."]


@pytest.mark.asyncio
async def test_handle_media_url_shows_queue_position_when_busy(monkeypatch):
    monkeypatch.setattr(router, "DOWNLOAD_WORKERS", 1)
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking_downloader(url: str) -> MediaResult:
        started.set()
        await release.wait()
        return MediaResult(platform="tiktok", source_url=url, media_type="video", path=_make_downloaded_file())

    monkeypatch.setitem(router.DOWNLOADERS, "tiktok", blocking_downloader)
    await router.start_workers()

    bot = FakeBot()
    first = FakeMessage(bot)
    await router.handle_media_url(first)
    await asyncio.wait_for(started.wait(), timeout=1)

    second = FakeMessage(bot)
    await router.handle_media_url(second)
    assert second.answers == ["⏳ В очереди, позиция: 1"]

    release.set()
    await router._QUEUE.join()


@pytest.mark.asyncio
async def test_handle_media_url_skips_status_message_for_channel_posts(monkeypatch):
    monkeypatch.setattr(router, "DOWNLOAD_WORKERS", 1)

    async def instant(url: str) -> MediaResult:
        return MediaResult(platform="tiktok", source_url=url, media_type="video", path=_make_downloaded_file())

    monkeypatch.setitem(router.DOWNLOADERS, "tiktok", instant)
    await router.start_workers()

    bot = FakeBot()
    msg = FakeMessage(bot, chat_type=ChatType.CHANNEL)
    await router.handle_media_url(msg)
    await router._QUEUE.join()

    assert msg.answers == []


@pytest.mark.asyncio
async def test_process_job_cache_hit_sends_by_file_id_and_marks_done(fake_json_db):
    fake_json_db.cache["https://tiktok.com/@u/video/1"] = "CACHED_FILE_ID"
    bot = FakeBot()
    msg = FakeMessage(bot)
    status = FakeStatusMessage()
    job = router._QueueJob(msg=msg, source_url="https://tiktok.com/@u/video/1", platform="tiktok", status_msg=status)

    await router._process_job(job)

    assert bot.sent[0]["video"] == "CACHED_FILE_ID"
    assert status.texts[-1] == "✅ Готово"


@pytest.mark.asyncio
async def test_process_job_reports_video_too_large_error(monkeypatch):
    async def too_large(url: str) -> MediaResult:
        raise VideoTooLargeError("640.0 MB")

    monkeypatch.setitem(router.DOWNLOADERS, "tiktok", too_large)
    bot = FakeBot()
    msg = FakeMessage(bot)
    status = FakeStatusMessage()
    job = router._QueueJob(msg=msg, source_url="https://tiktok.com/@u/video/2", platform="tiktok", status_msg=status)

    await router._process_job(job)

    assert status.texts[-1].startswith("❌")
    assert "@akarmain" in status.texts[-1]


@pytest.mark.asyncio
async def test_process_job_reports_generic_error_without_a_stack_trace(monkeypatch):
    async def boom(url: str) -> MediaResult:
        raise RuntimeError("connection reset by peer")

    monkeypatch.setitem(router.DOWNLOADERS, "tiktok", boom)
    bot = FakeBot()
    msg = FakeMessage(bot)
    status = FakeStatusMessage()
    job = router._QueueJob(msg=msg, source_url="https://tiktok.com/@u/video/3", platform="tiktok", status_msg=status)

    await router._process_job(job)

    assert status.texts[-1] == "❌ Не удалось скачать видео. Проверьте ссылку и попробуйте снова."
    assert "connection reset" not in status.texts[-1]


@pytest.mark.asyncio
async def test_duplicate_concurrent_requests_for_same_url_both_complete(monkeypatch):
    monkeypatch.setattr(router, "DOWNLOAD_WORKERS", 2)

    async def instant(url: str) -> MediaResult:
        return MediaResult(platform="tiktok", source_url=url, media_type="video", path=_make_downloaded_file())

    monkeypatch.setitem(router.DOWNLOADERS, "tiktok", instant)
    await router.start_workers()

    bot = FakeBot()
    first = FakeMessage(bot)
    second = FakeMessage(bot)
    await router.handle_media_url(first)
    await router.handle_media_url(second)
    await router._QUEUE.join()

    assert len(bot.sent) == 2
