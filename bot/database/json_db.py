import asyncio
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlunparse

import aiosqlite

from bot.settings import SQLITE_DB_PATH

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    started_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS videos (
    key TEXT PRIMARY KEY,
    source_url TEXT NOT NULL,
    normalized_url TEXT NOT NULL,
    canonical_ref TEXT,
    platform TEXT,
    file_id TEXT,
    first_sent_at TEXT NOT NULL,
    last_sent_at TEXT NOT NULL,
    send_count INTEGER NOT NULL DEFAULT 0,
    sender_user_ids TEXT NOT NULL DEFAULT '[]',
    invalidated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_videos_normalized_url ON videos(normalized_url);
CREATE INDEX IF NOT EXISTS idx_videos_canonical_ref ON videos(canonical_ref);
"""

_UPSERT_USER_SQL = """
INSERT INTO users (user_id, username, first_name, last_name, started_at, last_seen_at)
VALUES (:user_id, :username, :first_name, :last_name, :now, :now)
ON CONFLICT(user_id) DO UPDATE SET
    username = excluded.username,
    first_name = excluded.first_name,
    last_name = excluded.last_name,
    last_seen_at = excluded.last_seen_at
"""

_UPSERT_VIDEO_SQL = """
INSERT INTO videos (
    key, source_url, normalized_url, canonical_ref, platform, file_id,
    first_sent_at, last_sent_at, send_count, sender_user_ids
)
VALUES (
    :key, :source_url, :normalized_url, :canonical_ref, :platform, :file_id,
    :now, :now, 1,
    CASE WHEN :sender_id IS NULL THEN '[]' ELSE json_array(:sender_id) END
)
ON CONFLICT(key) DO UPDATE SET
    source_url = excluded.source_url,
    normalized_url = excluded.normalized_url,
    canonical_ref = excluded.canonical_ref,
    platform = excluded.platform,
    file_id = excluded.file_id,
    last_sent_at = excluded.last_sent_at,
    send_count = videos.send_count + 1,
    sender_user_ids = CASE WHEN :sender_id IS NULL THEN videos.sender_user_ids ELSE (
        SELECT json_group_array(v) FROM (
            SELECT value AS v FROM json_each(videos.sender_user_ids)
            UNION
            SELECT :sender_id
        ) ORDER BY v
    ) END
"""

_INVALIDATE_VIDEO_SQL = """
UPDATE videos SET file_id = NULL, invalidated_at = :now
WHERE file_id IS NOT NULL AND (
    key IN (:normalized_key, :legacy_key, :canonical_key) OR
    normalized_url = :normalized_url OR
    (:canonical_ref IS NOT NULL AND canonical_ref = :canonical_ref)
)
"""


class SQLiteDB:
    def __init__(self, db_path: str):
        self._db_path = Path(db_path)
        self._conn: aiosqlite.Connection | None = None
        self._connect_lock = asyncio.Lock()

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _normalized_host(host: str) -> str:
        normalized = (host or "").lower()
        if normalized.startswith("www."):
            normalized = normalized[4:]
        return normalized

    @classmethod
    def _normalize_source_url(cls, source_url: str) -> str:
        parsed = urlparse(source_url.strip())
        scheme = (parsed.scheme or "https").lower()
        host = cls._normalized_host(parsed.netloc)
        path = re.sub(r"/{2,}", "/", parsed.path or "/")
        if path != "/":
            path = path.rstrip("/")

        query_items = sorted(parse_qsl(parsed.query, keep_blank_values=False))
        query = urlencode(query_items, doseq=True)
        return urlunparse((scheme, host, path, "", query, ""))

    @staticmethod
    def _extract_youtube_id(url: str) -> str | None:
        parsed = urlparse(url)
        host = SQLiteDB._normalized_host(parsed.netloc)
        path = parsed.path.strip("/")

        if "youtu.be" in host and path:
            return path.split("/")[0]

        query_id = parse_qs(parsed.query).get("v")
        if query_id and query_id[0]:
            return query_id[0]

        match = re.search(r"(?:shorts|embed|live|v)/([A-Za-z0-9_-]{11})", path)
        if match:
            return match.group(1)
        return None

    @staticmethod
    def _extract_tiktok_id(url: str) -> str | None:
        parsed = urlparse(url)
        path = parsed.path
        for pattern in (
            r"/video/(\d+)",
            r"/v/(\d+)(?:\.html)?",
            r"/embed(?:/v2)?/(\d+)",
        ):
            match = re.search(pattern, path)
            if match:
                return match.group(1)
        return None

    @classmethod
    def _canonical_video_ref(cls, source_url: str) -> tuple[str | None, str]:
        url = source_url.strip()
        parsed = urlparse(url)
        host = cls._normalized_host(parsed.netloc)

        if host.endswith("youtube.com") or host == "youtu.be":
            yt_id = cls._extract_youtube_id(url)
            if yt_id:
                return f"youtube:{yt_id}", "youtube"
            return None, "youtube"

        if host.endswith("tiktok.com"):
            tt_id = cls._extract_tiktok_id(url)
            if tt_id:
                return f"tiktok:{tt_id}", "tiktok"
            return None, "tiktok"

        return None, "unknown"

    @staticmethod
    def _video_key(seed: str) -> str:
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()

    async def _connection(self) -> aiosqlite.Connection:
        if self._conn is not None:
            return self._conn
        async with self._connect_lock:
            if self._conn is None:
                self._db_path.parent.mkdir(parents=True, exist_ok=True)
                conn = await aiosqlite.connect(self._db_path)
                await conn.execute("PRAGMA journal_mode=WAL")
                await conn.execute("PRAGMA synchronous=NORMAL")
                await conn.execute("PRAGMA busy_timeout=5000")
                await conn.executescript(SCHEMA_SQL)
                await conn.commit()
                self._conn = conn
        return self._conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def upsert_user(
        self,
        user_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
    ) -> None:
        conn = await self._connection()
        await conn.execute(
            _UPSERT_USER_SQL,
            {
                "user_id": user_id,
                "username": username,
                "first_name": first_name,
                "last_name": last_name,
                "now": self._now_iso(),
            },
        )
        await conn.commit()

    async def upsert_video(
        self,
        source_url: str,
        file_id: str,
        sender_user_id: int | None,
        platform: str | None = None,
    ) -> None:
        conn = await self._connection()
        normalized_url = self._normalize_source_url(source_url)
        canonical_ref, detected_platform = self._canonical_video_ref(source_url)
        await conn.execute(
            _UPSERT_VIDEO_SQL,
            {
                "key": self._video_key(normalized_url),
                "source_url": source_url,
                "normalized_url": normalized_url,
                "canonical_ref": canonical_ref,
                "platform": platform or detected_platform,
                "file_id": file_id,
                "now": self._now_iso(),
                "sender_id": sender_user_id,
            },
        )
        await conn.commit()

    async def _file_id_by_key(self, conn: aiosqlite.Connection, key: str) -> str | None:
        async with conn.execute(
            "SELECT file_id FROM videos WHERE key = ? AND file_id IS NOT NULL", (key,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

    async def get_cached_file_id(self, source_url: str) -> str | None:
        conn = await self._connection()
        normalized_url = self._normalize_source_url(source_url)

        file_id = await self._file_id_by_key(conn, self._video_key(normalized_url))
        if file_id:
            return file_id

        file_id = await self._file_id_by_key(conn, self._video_key(source_url.strip()))
        if file_id:
            return file_id

        async with conn.execute(
            "SELECT file_id FROM videos WHERE normalized_url = ? AND file_id IS NOT NULL "
            "ORDER BY rowid LIMIT 1",
            (normalized_url,),
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return row[0]

        canonical_ref, _ = self._canonical_video_ref(source_url)
        if not canonical_ref:
            return None

        async with conn.execute(
            "SELECT file_id FROM videos WHERE key = ? AND canonical_ref = ? AND file_id IS NOT NULL",
            (self._video_key(canonical_ref), canonical_ref),
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return row[0]

        async with conn.execute(
            "SELECT file_id FROM videos WHERE canonical_ref = ? AND file_id IS NOT NULL "
            "ORDER BY last_sent_at DESC LIMIT 1",
            (canonical_ref,),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

    async def invalidate_cached_file_id(self, source_url: str) -> None:
        conn = await self._connection()
        normalized_url = self._normalize_source_url(source_url)
        canonical_ref, _ = self._canonical_video_ref(source_url)
        normalized_key = self._video_key(normalized_url)
        canonical_key = self._video_key(canonical_ref) if canonical_ref else normalized_key

        await conn.execute(
            _INVALIDATE_VIDEO_SQL,
            {
                "now": self._now_iso(),
                "normalized_key": normalized_key,
                "legacy_key": self._video_key(source_url.strip()),
                "canonical_key": canonical_key,
                "normalized_url": normalized_url,
                "canonical_ref": canonical_ref,
            },
        )
        await conn.commit()

    async def export_users_file(self) -> str:
        conn = await self._connection()
        async with conn.execute(
            "SELECT user_id, username, first_name, last_name, started_at, last_seen_at "
            "FROM users ORDER BY user_id"
        ) as cursor:
            rows = await cursor.fetchall()

        users = [
            {
                "user_id": row[0],
                "username": row[1],
                "first_name": row[2],
                "last_name": row[3],
                "last_seen_at": row[5],
                "started_at": row[4],
            }
            for row in rows
        ]
        export_payload = {
            "exported_at": self._now_iso(),
            "total_users": len(users),
            "users": users,
        }
        export_path = self._db_path.parent / "users_export.json"
        with export_path.open("w", encoding="utf-8") as file:
            json.dump(export_payload, file, ensure_ascii=False, indent=2)
        return str(export_path)


json_db = SQLiteDB(SQLITE_DB_PATH)
