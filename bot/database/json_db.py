import asyncio
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlunparse

from bot.settings import JSON_DB_PATH


class JsonDB:
    def __init__(self, db_path: str):
        self._db_path = Path(db_path)
        self._lock = asyncio.Lock()

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"users": {}, "videos": {}}

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

    def _load_unlocked(self) -> dict[str, Any]:
        if not self._db_path.exists():
            return self._empty()
        try:
            with self._db_path.open("r", encoding="utf-8") as file:
                data = json.load(file)
        except (json.JSONDecodeError, OSError):
            return self._empty()

        if not isinstance(data, dict):
            return self._empty()
        data.setdefault("users", {})
        data.setdefault("videos", {})
        return data

    def _save_unlocked(self, data: dict[str, Any]) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._db_path.with_suffix(f"{self._db_path.suffix}.tmp")
        with temp_path.open("w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
        os.replace(temp_path, self._db_path)

    @staticmethod
    def _extract_youtube_id(url: str) -> str | None:
        parsed = urlparse(url)
        host = JsonDB._normalized_host(parsed.netloc)
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

    async def upsert_user(
        self,
        user_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
    ) -> None:
        async with self._lock:
            data = self._load_unlocked()
            users = data["users"]
            key = str(user_id)
            now = self._now_iso()

            user_record = users.get(key, {"user_id": user_id, "started_at": now})
            user_record["username"] = username
            user_record["first_name"] = first_name
            user_record["last_name"] = last_name
            user_record["last_seen_at"] = now

            users[key] = user_record
            self._save_unlocked(data)

    async def upsert_video(
        self,
        source_url: str,
        file_id: str,
        sender_user_id: int | None,
        platform: str | None = None,
    ) -> None:
        async with self._lock:
            data = self._load_unlocked()
            videos = data["videos"]
            normalized_url = self._normalize_source_url(source_url)
            canonical_ref, detected_platform = self._canonical_video_ref(source_url)
            key = self._video_key(normalized_url)
            now = self._now_iso()

            video_record = videos.get(
                key,
                {
                    "source_url": source_url,
                    "normalized_url": normalized_url,
                    "canonical_ref": canonical_ref,
                    "platform": platform or detected_platform,
                    "first_sent_at": now,
                    "send_count": 0,
                    "sender_user_ids": [],
                },
            )
            video_record["source_url"] = source_url
            video_record["normalized_url"] = normalized_url
            video_record["canonical_ref"] = canonical_ref
            video_record["platform"] = platform or detected_platform
            video_record["file_id"] = file_id
            video_record["last_sent_at"] = now
            video_record["send_count"] = int(video_record.get("send_count", 0)) + 1

            if sender_user_id is not None:
                sender_ids = set(video_record.get("sender_user_ids", []))
                sender_ids.add(sender_user_id)
                video_record["sender_user_ids"] = sorted(sender_ids)

            videos[key] = video_record
            self._save_unlocked(data)

    async def get_cached_file_id(self, source_url: str) -> str | None:
        async with self._lock:
            data = self._load_unlocked()
            videos = data.get("videos", {})
            normalized_url = self._normalize_source_url(source_url)

            exact_key = self._video_key(normalized_url)
            record = videos.get(exact_key)
            if record and record.get("file_id"):
                return str(record["file_id"])

            legacy_key = self._video_key(source_url.strip())
            legacy_record = videos.get(legacy_key)
            if legacy_record and legacy_record.get("file_id"):
                return str(legacy_record["file_id"])

            for record in videos.values():
                if not isinstance(record, dict):
                    continue
                if record.get("normalized_url") == normalized_url and record.get("file_id"):
                    return str(record["file_id"])

            canonical_ref, _ = self._canonical_video_ref(source_url)
            if not canonical_ref:
                return None

            key = self._video_key(canonical_ref)
            record = videos.get(key)
            if record and record.get("file_id") and record.get("canonical_ref") == canonical_ref:
                return str(record["file_id"])

            matched_records = [
                record
                for record in videos.values()
                if isinstance(record, dict)
                and record.get("canonical_ref") == canonical_ref
                and record.get("file_id")
            ]
            if matched_records:
                matched_records.sort(key=lambda row: str(row.get("last_sent_at", "")), reverse=True)
                return str(matched_records[0]["file_id"])
            return None

    async def invalidate_cached_file_id(self, source_url: str) -> None:
        async with self._lock:
            data = self._load_unlocked()
            videos = data.get("videos", {})
            normalized_url = self._normalize_source_url(source_url)
            canonical_ref, _ = self._canonical_video_ref(source_url)

            keys_to_invalidate = {
                self._video_key(normalized_url),
                self._video_key(source_url.strip()),
            }
            if canonical_ref:
                keys_to_invalidate.add(self._video_key(canonical_ref))

            for key in keys_to_invalidate:
                if key in videos and videos[key].get("file_id"):
                    videos[key]["file_id"] = None
                    videos[key]["invalidated_at"] = self._now_iso()

            for record in videos.values():
                if not isinstance(record, dict) or not record.get("file_id"):
                    continue
                if record.get("normalized_url") == normalized_url or (
                    canonical_ref and record.get("canonical_ref") == canonical_ref
                ):
                    record["file_id"] = None
                    record["invalidated_at"] = self._now_iso()

            self._save_unlocked(data)

    async def export_users_file(self) -> str:
        async with self._lock:
            data = self._load_unlocked()
            users = sorted(
                data.get("users", {}).values(),
                key=lambda row: int(row.get("user_id", 0)),
            )
            export_payload = {
                "exported_at": self._now_iso(),
                "total_users": len(users),
                "users": users,
            }
            export_path = self._db_path.parent / "users_export.json"
            with export_path.open("w", encoding="utf-8") as file:
                json.dump(export_payload, file, ensure_ascii=False, indent=2)
        return str(export_path)


json_db = JsonDB(JSON_DB_PATH)
