"""YouTube audio download: video/audio choice UX, media_kind cache separation.

Run: pytest tests/test_audio_download.py
"""

import asyncio
import os
import tempfile

import pytest
import pytest_asyncio
from aiogram.enums import ChatType

os.environ.setdefault("TG_MAIN_BOT_TOKEN", "0:test")

from bot.database.json_db import SQLiteDB  # noqa: E402
from bot.downloader import router  # noqa: E402
from bot.downloader.media import MediaResult, VideoTooLargeError  # noqa: E402


def _make_file(suffix: str) -> str:
    handle = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    handle.write(b"x" * 256)
    handle.close()
    return handle.name


class FakeVideo:
    def __init__(self, file_id: str):
        self.file_id = file_id


class FakeAudio:
    def __init__(self, file_id: str):
        self.file_id = file_id


class FakeSentMessage:
    def __init__(self, video_file_id: str | None = None, audio_file_id: str | None = None):
        self.video = FakeVideo(video_file_id) if video_file_id else None
        self.audio = FakeAudio(audio_file_id) if audio_file_id else None


class FakeBot:
    def __init__(self):
        self.sent_videos: list[dict] = []
        self.sent_audios: list[dict] = []

    async def send_video(self, chat_id, video, supports_streaming, width, height):
        file_id = video if isinstance(video, str) else "NEW_VIDEO_FILE"
        self.sent_videos.append({"chat_id": chat_id, "video": video})
        return FakeSentMessage(video_file_id=file_id)

    async def send_audio(self, chat_id, audio, title=None, performer=None, duration=None, thumbnail=None):
        file_id = audio if isinstance(audio, str) else "NEW_AUDIO_FILE"
        self.sent_audios.append(
            {"chat_id": chat_id, "audio": audio, "title": title, "performer": performer, "duration": duration}
        )
        return FakeSentMessage(audio_file_id=file_id)

    async def send_message(self, chat_id, text):
        return FakeSentMessage()


class FakeChat:
    def __init__(self, chat_id: int = 1, chat_type: ChatType = ChatType.PRIVATE):
        self.id = chat_id
        self.type = chat_type


class FakeUser:
    def __init__(self, user_id: int = 42):
        self.id = user_id


class FakeStatusMessage:
    def __init__(self, bot: FakeBot):
        self.bot = bot
        self.chat = FakeChat()
        self.deleted = False

    async def delete(self) -> None:
        self.deleted = True

    async def answer(self, text: str, reply_markup=None) -> "FakeStatusMessage":
        return FakeStatusMessage(self.bot)


class FakeMessage:
    def __init__(
        self,
        bot: FakeBot,
        chat_type: ChatType = ChatType.PRIVATE,
        user_id: int = 42,
        text: str = "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    ):
        self.bot = bot
        self.chat = FakeChat(chat_type=chat_type)
        self.from_user = FakeUser(user_id)
        self.text = text
        self.caption = None
        self.answers: list[tuple[str, object]] = []

    async def answer(self, text: str, reply_markup=None) -> FakeStatusMessage:
        status = FakeStatusMessage(self.bot)
        self.answers.append((text, reply_markup))
        return status


class FakeCallbackQuery:
    def __init__(self, data: str, message: FakeStatusMessage, user_id: int = 99):
        self.data = data
        self.message = message
        self.from_user = FakeUser(user_id)
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))


class FakeJsonDB:
    def __init__(self):
        self.cache: dict[tuple[str, str], str] = {}
        self.upserts: list[dict] = []
        self.invalidations: list[tuple[str, str]] = []

    async def get_cached_file_id(self, source_url: str, media_kind: str = "video") -> str | None:
        return self.cache.get((source_url, media_kind))

    async def upsert_video(self, source_url, file_id, sender_user_id, platform=None, media_kind="video") -> None:
        self.cache[(source_url, media_kind)] = file_id
        self.upserts.append(
            {
                "source_url": source_url,
                "file_id": file_id,
                "sender_user_id": sender_user_id,
                "platform": platform,
                "media_kind": media_kind,
            }
        )

    async def invalidate_cached_file_id(self, source_url: str, media_kind: str = "video") -> None:
        self.cache.pop((source_url, media_kind), None)
        self.invalidations.append((source_url, media_kind))


@pytest.fixture(autouse=True)
def fake_json_db(monkeypatch):
    fake = FakeJsonDB()
    monkeypatch.setattr(router, "json_db", fake)
    return fake


@pytest.fixture(autouse=True)
def clean_pending_tokens():
    router._pending_youtube_urls.clear()
    yield
    router._pending_youtube_urls.clear()


@pytest_asyncio.fixture(autouse=True)
async def workers(monkeypatch):
    monkeypatch.setattr(router, "DOWNLOAD_WORKERS", 1)
    await router.start_workers()
    yield
    await router.stop_workers()
    if router._QUEUE is not None:
        while not router._QUEUE.empty():
            router._QUEUE.get_nowait()
            router._QUEUE.task_done()
        router._QUEUE = None


class TestSQLiteDBMediaKindSeparation:
    @pytest_asyncio.fixture
    async def db(self, tmp_path):
        instance = SQLiteDB(str(tmp_path / "db.sqlite3"))
        yield instance
        await instance.close()

    @pytest.mark.asyncio
    async def test_video_and_audio_file_ids_do_not_collide(self, db):
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        await db.upsert_video(url, "VIDEO_FILE", 1, "youtube", media_kind="video")
        await db.upsert_video(url, "AUDIO_FILE", 1, "youtube", media_kind="audio")

        assert await db.get_cached_file_id(url, media_kind="video") == "VIDEO_FILE"
        assert await db.get_cached_file_id(url, media_kind="audio") == "AUDIO_FILE"

    @pytest.mark.asyncio
    async def test_invalidating_audio_does_not_touch_video(self, db):
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        await db.upsert_video(url, "VIDEO_FILE", 1, "youtube", media_kind="video")
        await db.upsert_video(url, "AUDIO_FILE", 1, "youtube", media_kind="audio")

        await db.invalidate_cached_file_id(url, media_kind="audio")

        assert await db.get_cached_file_id(url, media_kind="video") == "VIDEO_FILE"
        assert await db.get_cached_file_id(url, media_kind="audio") is None

    @pytest.mark.asyncio
    async def test_default_media_kind_is_video_for_backward_compatibility(self, db):
        url = "https://www.tiktok.com/@user/video/123"
        await db.upsert_video(url, "FILE", 1, "tiktok")
        assert await db.get_cached_file_id(url) == "FILE"


class TestYoutubeChoicePrompt:
    @pytest.mark.asyncio
    async def test_youtube_link_shows_video_audio_choice(self):
        bot = FakeBot()
        msg = FakeMessage(bot)
        await router.handle_media_url(msg)

        assert len(msg.answers) == 1
        text, keyboard = msg.answers[0]
        buttons = keyboard.inline_keyboard[0]
        assert buttons[0].text == "🎬 Видео"
        assert buttons[1].text == "🎵 Аудио"
        assert buttons[0].callback_data.startswith("ytchoice:video:")
        assert buttons[1].callback_data.startswith("ytchoice:audio:")
        assert len(router._pending_youtube_urls) == 1

    @pytest.mark.asyncio
    async def test_tiktok_link_bypasses_the_choice_prompt(self, monkeypatch):
        async def instant(url: str) -> MediaResult:
            return MediaResult(platform="tiktok", source_url=url, media_type="video", path=_make_file(".mp4"))

        monkeypatch.setitem(router.DOWNLOADERS, "tiktok", instant)
        bot = FakeBot()
        msg = FakeMessage(bot, text="https://www.tiktok.com/@user/video/123")
        await router.handle_media_url(msg)
        await router._QUEUE.join()

        assert router._pending_youtube_urls == {}
        assert len(bot.sent_videos) == 1

    @pytest.mark.asyncio
    async def test_channel_post_bypasses_the_choice_prompt_and_downloads_video(self, monkeypatch):
        async def instant(url: str) -> MediaResult:
            return MediaResult(platform="youtube", source_url=url, media_type="video", path=_make_file(".mp4"))

        monkeypatch.setitem(router.DOWNLOADERS, "youtube", instant)
        bot = FakeBot()
        msg = FakeMessage(bot, chat_type=ChatType.CHANNEL)
        await router.handle_media_url(msg)
        await router._QUEUE.join()

        assert router._pending_youtube_urls == {}
        assert len(bot.sent_videos) == 1

    def test_pending_urls_are_capped_and_evict_oldest(self, monkeypatch):
        monkeypatch.setattr(router, "_MAX_PENDING_YOUTUBE_CHOICES", 3)
        tokens = [router._remember_youtube_url(f"https://youtu.be/{i:011d}") for i in range(5)]
        assert len(router._pending_youtube_urls) == 3
        assert tokens[0] not in router._pending_youtube_urls
        assert tokens[-1] in router._pending_youtube_urls


class TestYoutubeChoiceCallback:
    @pytest.mark.asyncio
    async def test_expired_token_shows_alert_and_does_not_crash(self):
        bot = FakeBot()
        status = FakeStatusMessage(bot)
        callback = FakeCallbackQuery("ytchoice:video:doesnotexist", status)

        await router.handle_youtube_choice(callback)

        assert callback.answers == [("Ссылка устарела, отправьте её ещё раз.", True)]
        assert status.deleted is False

    @pytest.mark.asyncio
    async def test_double_click_on_the_same_button_only_downloads_once(self, monkeypatch):
        call_count = 0

        async def instant(url: str) -> MediaResult:
            nonlocal call_count
            call_count += 1
            return MediaResult(platform="youtube", source_url=url, media_type="video", path=_make_file(".mp4"))

        monkeypatch.setitem(router.DOWNLOADERS, "youtube", instant)
        token = router._remember_youtube_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")

        bot = FakeBot()
        first_click = FakeCallbackQuery(f"ytchoice:video:{token}", FakeStatusMessage(bot))
        second_click = FakeCallbackQuery(f"ytchoice:video:{token}", FakeStatusMessage(bot))

        await router.handle_youtube_choice(first_click)
        await router.handle_youtube_choice(second_click)
        await router._QUEUE.join()

        assert call_count == 1
        assert second_click.answers == [("Ссылка устарела, отправьте её ещё раз.", True)]

    @pytest.mark.asyncio
    async def test_non_ascii_title_and_performer_round_trip(self, monkeypatch):
        async def instant_audio(url: str) -> MediaResult:
            return MediaResult(
                platform="youtube",
                source_url=url,
                media_type="audio",
                path=_make_file(".mp3"),
                title="Ленинград - Экспонат",
                performer="Артур Пирожков 🎵",
            )

        monkeypatch.setattr(router, "download_youtube_audio", instant_audio)
        token = router._remember_youtube_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=тест")

        bot = FakeBot()
        callback = FakeCallbackQuery(f"ytchoice:audio:{token}", FakeStatusMessage(bot))
        await router.handle_youtube_choice(callback)
        await router._QUEUE.join()

        assert bot.sent_audios[0]["title"] == "Ленинград - Экспонат"
        assert bot.sent_audios[0]["performer"] == "Артур Пирожков 🎵"

    @pytest.mark.asyncio
    async def test_video_choice_downloads_and_sends_video(self, monkeypatch):
        async def instant(url: str) -> MediaResult:
            return MediaResult(platform="youtube", source_url=url, media_type="video", path=_make_file(".mp4"))

        monkeypatch.setitem(router.DOWNLOADERS, "youtube", instant)
        token = router._remember_youtube_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")

        bot = FakeBot()
        status = FakeStatusMessage(bot)
        callback = FakeCallbackQuery(f"ytchoice:video:{token}", status, user_id=777)

        await router.handle_youtube_choice(callback)
        await router._QUEUE.join()

        assert status.deleted is True
        assert len(bot.sent_videos) == 1
        assert router.json_db.upserts[0]["media_kind"] == "video"
        assert router.json_db.upserts[0]["sender_user_id"] == 777

    @pytest.mark.asyncio
    async def test_audio_choice_downloads_and_sends_audio_with_metadata(self, monkeypatch):
        async def instant_audio(url: str) -> MediaResult:
            return MediaResult(
                platform="youtube",
                source_url=url,
                media_type="audio",
                path=_make_file(".mp3"),
                title="Never Gonna Give You Up",
                performer="Rick Astley",
                duration=213,
                thumbnail_path=_make_file(".jpg"),
            )

        monkeypatch.setattr(router, "download_youtube_audio", instant_audio)
        token = router._remember_youtube_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")

        bot = FakeBot()
        status = FakeStatusMessage(bot)
        callback = FakeCallbackQuery(f"ytchoice:audio:{token}", status, user_id=777)

        await router.handle_youtube_choice(callback)
        await router._QUEUE.join()

        assert len(bot.sent_audios) == 1
        sent = bot.sent_audios[0]
        assert sent["title"] == "Never Gonna Give You Up"
        assert sent["performer"] == "Rick Astley"
        assert sent["duration"] == 213
        assert router.json_db.upserts[0]["media_kind"] == "audio"

    @pytest.mark.asyncio
    async def test_audio_cache_hit_sends_by_file_id_without_redownloading(self, monkeypatch, fake_json_db):
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        fake_json_db.cache[(url, "audio")] = "CACHED_AUDIO_FILE"

        async def should_not_be_called(_: str) -> MediaResult:
            raise AssertionError("downloader should not run on a cache hit")

        monkeypatch.setattr(router, "download_youtube_audio", should_not_be_called)
        token = router._remember_youtube_url(url)

        bot = FakeBot()
        status = FakeStatusMessage(bot)
        callback = FakeCallbackQuery(f"ytchoice:audio:{token}", status)

        await router.handle_youtube_choice(callback)
        await router._QUEUE.join()

        assert bot.sent_audios[0]["audio"] == "CACHED_AUDIO_FILE"

    @pytest.mark.asyncio
    async def test_video_too_large_reports_friendly_error(self, monkeypatch):
        async def too_large(url: str) -> MediaResult:
            raise VideoTooLargeError("2500.0 MB")

        monkeypatch.setitem(router.DOWNLOADERS, "youtube", too_large)
        token = router._remember_youtube_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")

        bot = FakeBot()
        status = FakeStatusMessage(bot)
        callback = FakeCallbackQuery(f"ytchoice:video:{token}", status)

        await router.handle_youtube_choice(callback)
        await router._QUEUE.join()

        assert bot.sent_videos == []

    @pytest.mark.asyncio
    async def test_audio_download_cleans_up_both_audio_and_thumbnail_files(self, monkeypatch):
        audio_path = _make_file(".mp3")
        thumb_path = _make_file(".jpg")

        async def instant_audio(url: str) -> MediaResult:
            return MediaResult(
                platform="youtube", source_url=url, media_type="audio",
                path=audio_path, thumbnail_path=thumb_path,
            )

        monkeypatch.setattr(router, "download_youtube_audio", instant_audio)
        token = router._remember_youtube_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")

        bot = FakeBot()
        status = FakeStatusMessage(bot)
        callback = FakeCallbackQuery(f"ytchoice:audio:{token}", status)

        await router.handle_youtube_choice(callback)
        await router._QUEUE.join()

        assert not os.path.exists(audio_path)
        assert not os.path.exists(thumb_path)
