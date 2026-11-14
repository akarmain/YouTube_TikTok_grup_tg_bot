"""Exercise topic routing through real aiogram messages without Telegram requests."""

import asyncio
import os
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher
from aiogram.enums import ChatType
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    EditMessageText,
    SendAudio,
    SendMessage,
    SendVideo,
)
from aiogram.types import Audio, CallbackQuery, Chat, Message, Update, User, Video

os.environ.setdefault("TG_MAIN_BOT_TOKEN", "0:test")

from bot.downloader import router  # noqa: E402
from bot.downloader.media import MediaResult  # noqa: E402


@pytest_asyncio.fixture
async def telegram(monkeypatch):
    bot = Bot("123456:test")
    requests = []
    messages = {}

    async def respond(bot, method, timeout=None):
        requests.append(method)
        if isinstance(method, (AnswerCallbackQuery, DeleteMessage)):
            return True
        if isinstance(method, EditMessageText):
            return messages[method.message_id].model_copy(update={"text": method.text})
        thread_id = method.message_thread_id
        message = Message(
            message_id=1000 + len(requests),
            date=datetime.now(timezone.utc),
            chat=Chat(id=method.chat_id, type=ChatType.SUPERGROUP, is_forum=True),
            message_thread_id=thread_id,
            is_topic_message=True if thread_id is not None else None,
            text=method.text if isinstance(method, SendMessage) else None,
            video=Video(file_id="VIDEO", file_unique_id="video", width=720, height=1280, duration=1)
            if isinstance(method, SendVideo) else None,
            audio=Audio(file_id="AUDIO", file_unique_id="audio", duration=1)
            if isinstance(method, SendAudio) else None,
        ).as_(bot)
        messages[message.message_id] = message
        return message

    monkeypatch.setattr(bot.session, "make_request", AsyncMock(side_effect=respond))
    monkeypatch.setattr(router, "json_db", SimpleNamespace(
        get_cached_file_id=AsyncMock(return_value=None),
        upsert_video=AsyncMock(),
        invalidate_cached_file_id=AsyncMock(),
    ))
    monkeypatch.setattr(router, "DOWNLOAD_WORKERS", 1)
    router._pending_youtube_urls.clear()
    await router.start_workers()
    yield bot, requests, messages
    await router.stop_workers()
    router._QUEUE = None
    router._pending_youtube_urls.clear()
    await bot.session.close()


def incoming(bot, *, thread_id=42, is_topic_message=True,
             chat_type=ChatType.SUPERGROUP, is_forum=True,
             url="https://www.tiktok.com/@user/video/123"):
    return Message(
        message_id=100,
        date=datetime.now(timezone.utc),
        chat=Chat(id=-1001234567890, type=chat_type, is_forum=is_forum),
        from_user=User(id=123, is_bot=False, first_name="User"),
        message_thread_id=thread_id,
        is_topic_message=is_topic_message,
        text=url,
    ).as_(bot)


@pytest.mark.asyncio
@pytest.mark.parametrize("media_kind", ["video", "audio"])
@pytest.mark.parametrize("cached", [False, True], ids=["download", "cache"])
@pytest.mark.parametrize("context,expected_thread", [
    ({"thread_id": 42}, 42),
    ({"thread_id": 1}, 1),
    ({"thread_id": None, "is_topic_message": None}, None),
    ({"thread_id": 777, "is_topic_message": False, "is_forum": False}, None),
    ({"thread_id": None, "is_topic_message": None,
      "chat_type": ChatType.PRIVATE, "is_forum": None}, None),
    ({"thread_id": None, "is_topic_message": None,
      "chat_type": ChatType.CHANNEL, "is_forum": None}, None),
])
async def test_media_uses_source_topic(telegram, monkeypatch, tmp_path,
                                     media_kind, cached, context, expected_thread):
    bot, requests, messages = telegram
    msg = incoming(bot, **context)
    media_path = tmp_path / ("media.mp3" if media_kind == "audio" else "media.mp4")
    media_path.write_bytes(b"media")
    downloader = AsyncMock(return_value=MediaResult(
        platform="youtube", source_url=msg.text,
        media_type=media_kind, path=str(media_path),
    ))
    monkeypatch.setitem(router.DOWNLOADERS, "youtube", downloader)
    monkeypatch.setattr(router, "download_youtube_audio", downloader)
    router.json_db.get_cached_file_id.return_value = "CACHED" if cached else None

    await router._process_job(router._QueueJob(
        msg=msg, source_url=msg.text, platform="youtube", media_kind=media_kind,
        sender_user_id=123, status_msg=None,
    ))

    media_requests = [m for m in requests if isinstance(m, (SendVideo, SendAudio))]
    assert len(media_requests) == 1
    sent = media_requests[0]
    assert sent.chat_id == msg.chat.id
    assert sent.message_thread_id == expected_thread
    assert isinstance(sent, SendAudio if media_kind == "audio" else SendVideo)
    router.json_db.upsert_video.assert_awaited_once()
    assert downloader.await_count == (0 if cached else 1)


@pytest.mark.asyncio
async def test_supergroup_update_keeps_status_and_cached_video_in_topic(telegram):
    bot, requests, messages = telegram
    router.json_db.get_cached_file_id.return_value = "CACHED"
    dp = Dispatcher()
    router.register_handlers(dp)

    for thread_id in (42, 84):
        await dp.feed_update(bot, Update(
            update_id=thread_id, message=incoming(bot, thread_id=thread_id),
        ))
    await asyncio.wait_for(router._QUEUE.join(), timeout=2)

    statuses = [m for m in requests if isinstance(m, SendMessage)]
    videos = [m for m in requests if isinstance(m, SendVideo)]
    assert [m.message_thread_id for m in statuses] == [42, 84]
    assert [m.message_thread_id for m in videos] == [42, 84]
    assert len([m for m in requests if isinstance(m, EditMessageText) and m.text == "✅ Готово"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("media_kind", ["video", "audio"])
async def test_youtube_choice_keeps_topic_after_prompt_deletion(telegram, media_kind):
    bot, requests, messages = telegram
    router.json_db.get_cached_file_id.return_value = "CACHED"
    msg = incoming(bot, url="https://youtu.be/dQw4w9WgXcQ")

    await router.handle_media_url(msg)
    prompt = next(m for m in requests if isinstance(m, SendMessage))
    assert prompt.message_thread_id == 42
    buttons = prompt.reply_markup.inline_keyboard[0]
    button = next(b for b in buttons if b.callback_data.startswith(f"ytchoice:{media_kind}:"))
    # Use the actual aiogram prompt response as the callback's message.
    prompt_message = next(iter(messages.values()))
    await router.handle_youtube_choice(CallbackQuery(
        id="callback", from_user=msg.from_user, chat_instance="chat",
        data=button.callback_data, message=prompt_message,
    ).as_(bot))
    await asyncio.wait_for(router._QUEUE.join(), timeout=2)

    assert any(isinstance(m, DeleteMessage) for m in requests)
    assert all(m.message_thread_id == 42 for m in requests
               if isinstance(m, (SendMessage, SendVideo, SendAudio)))
    assert len([m for m in requests if isinstance(m, (SendVideo, SendAudio))]) == 1
