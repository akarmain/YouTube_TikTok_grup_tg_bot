"""SQLiteDB behavior: interface parity with the old JsonDB, concurrency, boundary inputs.

Run: pytest tests/test_sqlite_db.py
"""

import asyncio
import json
import os
import sqlite3
import subprocess
import sys

import pytest
import pytest_asyncio

os.environ.setdefault("TG_MAIN_BOT_TOKEN", "0:test")

from bot.database.json_db import SQLiteDB  # noqa: E402
from bot.database.migrate_json_to_sqlite import migrate  # noqa: E402


@pytest_asyncio.fixture
async def db(tmp_path):
    instance = SQLiteDB(str(tmp_path / "test.sqlite3"))
    yield instance
    await instance.close()


@pytest.mark.asyncio
async def test_upsert_and_get_cached_file_id_roundtrip(db):
    assert await db.get_cached_file_id("https://www.tiktok.com/@user/video/123") is None
    await db.upsert_video("https://www.tiktok.com/@user/video/123", "FILE1", 111, "tiktok")
    assert await db.get_cached_file_id("https://www.tiktok.com/@user/video/123") == "FILE1"


@pytest.mark.asyncio
async def test_upsert_video_merges_by_normalized_url(db):
    await db.upsert_video("https://www.TikTok.com/@user/video/123/", "FILE1", 111, "tiktok")
    await db.upsert_video("https://tiktok.com/@user/video/123", "FILE2", 222, "tiktok")
    conn = await db._connection()
    async with conn.execute("SELECT count(*) FROM videos") as cursor:
        (count,) = await cursor.fetchone()
    assert count == 1
    assert await db.get_cached_file_id("https://www.tiktok.com/@user/video/123") == "FILE2"


@pytest.mark.asyncio
async def test_invalidate_clears_all_urls_sharing_canonical_ref(db):
    await db.upsert_video("https://www.tiktok.com/@user/video/123?a=1", "FILE1", 111, "tiktok")
    await db.upsert_video("https://www.tiktok.com/@user/video/123?a=2", "FILE2", 222, "tiktok")
    await db.invalidate_cached_file_id("https://www.tiktok.com/@user/video/123")
    assert await db.get_cached_file_id("https://www.tiktok.com/@user/video/123?a=1") is None
    assert await db.get_cached_file_id("https://www.tiktok.com/@user/video/123?a=2") is None


@pytest.mark.asyncio
async def test_upsert_video_idempotent_repeated_calls_stay_consistent(db):
    for _ in range(5):
        await db.upsert_video("https://youtu.be/dQw4w9WgXcQ", "SAME_FILE", 42, "youtube")
    conn = await db._connection()
    async with conn.execute("SELECT send_count, sender_user_ids, file_id FROM videos") as cursor:
        row = await cursor.fetchone()
    assert row[0] == 5
    assert json.loads(row[1]) == [42]
    assert row[2] == "SAME_FILE"


@pytest.mark.asyncio
async def test_concurrent_upserts_to_same_key_do_not_lose_updates(db):
    url = "https://www.tiktok.com/@user/video/999"
    concurrency = 50

    async def upsert(sender_id: int) -> None:
        await db.upsert_video(url, f"FILE{sender_id}", sender_id, "tiktok")

    await asyncio.gather(*(upsert(i) for i in range(concurrency)))

    conn = await db._connection()
    async with conn.execute("SELECT send_count, sender_user_ids FROM videos") as cursor:
        row = await cursor.fetchone()
    assert row[0] == concurrency
    assert json.loads(row[1]) == list(range(concurrency))


@pytest.mark.asyncio
async def test_concurrent_mixed_reads_writes_invalidate_on_shared_instance(db):
    url = "https://www.tiktok.com/@user/video/555"
    await db.upsert_video(url, "FILE0", 0, "tiktok")

    async def writer(i: int) -> None:
        await db.upsert_video(url, f"FILE{i}", i, "tiktok")

    async def reader() -> str | None:
        return await db.get_cached_file_id(url)

    async def invalidator() -> None:
        await db.invalidate_cached_file_id(url)

    tasks = (
        [writer(i) for i in range(1, 21)]
        + [reader() for _ in range(20)]
        + [invalidator() for _ in range(5)]
    )
    await asyncio.gather(*tasks)

    conn = await db._connection()
    async with conn.execute("SELECT send_count FROM videos") as cursor:
        (send_count,) = await cursor.fetchone()
    assert send_count == 21


@pytest.mark.asyncio
async def test_empty_and_whitespace_source_url_do_not_crash(db):
    assert await db.get_cached_file_id("") is None
    assert await db.get_cached_file_id("   ") is None
    await db.upsert_video("   ", "FILE", 1, "unknown")
    assert await db.get_cached_file_id("   ") == "FILE"


@pytest.mark.asyncio
async def test_malformed_url_falls_back_gracefully(db):
    malformed = "not a url at all ://???"
    await db.upsert_video(malformed, "FILE", 1, "unknown")
    assert await db.get_cached_file_id(malformed) == "FILE"


@pytest.mark.asyncio
async def test_very_long_url_is_handled(db):
    long_url = "https://www.tiktok.com/@user/video/123?" + "a=1&" * 5000
    await db.upsert_video(long_url, "FILE_LONG", 1, "tiktok")
    assert await db.get_cached_file_id(long_url) == "FILE_LONG"


@pytest.mark.asyncio
async def test_non_ascii_url_and_username(db):
    url = "https://www.tiktok.com/@пользователь/video/123?q=видео"
    await db.upsert_video(url, "FILE_RU", 1, "tiktok")
    assert await db.get_cached_file_id(url) == "FILE_RU"

    await db.upsert_user(2, "имя_🎬", "Имя", "Фамилия")
    export_path = await db.export_users_file()
    with open(export_path, encoding="utf-8") as file:
        payload = json.load(file)
    os.remove(export_path)
    assert payload["users"][0]["username"] == "имя_🎬"


@pytest.mark.asyncio
async def test_negative_and_zero_user_ids_do_not_crash(db):
    await db.upsert_user(0, "zero", None, None)
    await db.upsert_user(-1, "negative", None, None)
    await db.upsert_video("https://youtu.be/abc12345678", "FILE", -1, "youtube")
    conn = await db._connection()
    async with conn.execute("SELECT count(*) FROM users") as cursor:
        (count,) = await cursor.fetchone()
    assert count == 2


@pytest.mark.asyncio
async def test_export_users_file_with_zero_users(db):
    export_path = await db.export_users_file()
    with open(export_path, encoding="utf-8") as file:
        payload = json.load(file)
    os.remove(export_path)
    assert payload["total_users"] == 0
    assert payload["users"] == []


@pytest.mark.asyncio
async def test_wal_journal_mode_is_active(db):
    conn = await db._connection()
    async with conn.execute("PRAGMA journal_mode") as cursor:
        (mode,) = await cursor.fetchone()
    assert mode.lower() == "wal"


def test_migrate_missing_json_file_is_a_noop(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "bot.database.migrate_json_to_sqlite",
            "--json-path",
            str(tmp_path / "missing.json"),
            "--sqlite-path",
            str(tmp_path / "out.sqlite3"),
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "TG_MAIN_BOT_TOKEN": "0:test"},
    )
    assert result.returncode == 0
    assert "nothing to migrate" in result.stdout
    assert not (tmp_path / "out.sqlite3").exists()


def test_migrate_corrupted_json_raises(tmp_path):
    json_path = tmp_path / "broken.json"
    json_path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        migrate(json_path, tmp_path / "out.sqlite3")


def test_migrate_is_idempotent_on_rerun(tmp_path):
    json_path = tmp_path / "users_videos.json"
    json_path.write_text(
        json.dumps(
            {
                "users": {"1": {"user_id": 1, "username": "vlad", "started_at": "t0", "last_seen_at": "t1"}},
                "videos": {
                    "key1": {
                        "source_url": "https://tiktok.com/@u/video/1",
                        "normalized_url": "https://tiktok.com/@u/video/1",
                        "canonical_ref": "tiktok:1",
                        "platform": "tiktok",
                        "file_id": "FILE",
                        "first_sent_at": "t0",
                        "last_sent_at": "t1",
                        "send_count": 3,
                        "sender_user_ids": [1, 2],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    sqlite_path = tmp_path / "out.sqlite3"

    users_1, videos_1 = migrate(json_path, sqlite_path)
    users_2, videos_2 = migrate(json_path, sqlite_path)
    assert (users_1, videos_1) == (1, 1)
    assert (users_2, videos_2) == (1, 1)

    connection = sqlite3.connect(sqlite_path)
    try:
        assert connection.execute("SELECT count(*) FROM users").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM videos").fetchone() == (1,)
        row = connection.execute("SELECT send_count, sender_user_ids FROM videos").fetchone()
        assert row[0] == 3
        assert json.loads(row[1]) == [1, 2]
    finally:
        connection.close()


def test_migrate_creates_expected_schema(tmp_path):
    json_path = tmp_path / "users_videos.json"
    json_path.write_text(json.dumps({"users": {}, "videos": {}}), encoding="utf-8")
    sqlite_path = tmp_path / "out.sqlite3"

    migrate(json_path, sqlite_path)

    connection = sqlite3.connect(sqlite_path)
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"users", "videos"} <= tables
    finally:
        connection.close()
