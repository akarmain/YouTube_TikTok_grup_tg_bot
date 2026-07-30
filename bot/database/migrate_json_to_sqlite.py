import argparse
import json
import sqlite3
import sys
from pathlib import Path

from bot.database.json_db import SCHEMA_SQL
from bot.settings import JSON_DB_PATH, SQLITE_DB_PATH

_UPSERT_USER_SQL = """
INSERT INTO users (user_id, username, first_name, last_name, started_at, last_seen_at)
VALUES (:user_id, :username, :first_name, :last_name, :started_at, :last_seen_at)
ON CONFLICT(user_id) DO UPDATE SET
    username = excluded.username,
    first_name = excluded.first_name,
    last_name = excluded.last_name,
    started_at = excluded.started_at,
    last_seen_at = excluded.last_seen_at
"""

_UPSERT_VIDEO_SQL = """
INSERT INTO videos (
    key, source_url, normalized_url, canonical_ref, platform, file_id,
    first_sent_at, last_sent_at, send_count, sender_user_ids, invalidated_at
)
VALUES (
    :key, :source_url, :normalized_url, :canonical_ref, :platform, :file_id,
    :first_sent_at, :last_sent_at, :send_count, :sender_user_ids, :invalidated_at
)
ON CONFLICT(key) DO UPDATE SET
    source_url = excluded.source_url,
    normalized_url = excluded.normalized_url,
    canonical_ref = excluded.canonical_ref,
    platform = excluded.platform,
    file_id = excluded.file_id,
    first_sent_at = excluded.first_sent_at,
    last_sent_at = excluded.last_sent_at,
    send_count = excluded.send_count,
    sender_user_ids = excluded.sender_user_ids,
    invalidated_at = excluded.invalidated_at
"""


def load_json_dump(json_path: Path) -> dict:
    with json_path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError(f"{json_path} does not contain a JSON object at the top level")
    return data


def migrate(json_path: Path, sqlite_path: Path) -> tuple[int, int]:
    data = load_json_dump(json_path)
    users = data.get("users", {})
    videos = data.get("videos", {})

    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(sqlite_path)
    try:
        connection.executescript(SCHEMA_SQL)

        for user in users.values():
            connection.execute(
                _UPSERT_USER_SQL,
                {
                    "user_id": user["user_id"],
                    "username": user.get("username"),
                    "first_name": user.get("first_name"),
                    "last_name": user.get("last_name"),
                    "started_at": user.get("started_at") or user.get("last_seen_at"),
                    "last_seen_at": user.get("last_seen_at") or user.get("started_at"),
                },
            )

        for key, video in videos.items():
            connection.execute(
                _UPSERT_VIDEO_SQL,
                {
                    "key": key,
                    "source_url": video["source_url"],
                    "normalized_url": video.get("normalized_url") or video["source_url"],
                    "canonical_ref": video.get("canonical_ref"),
                    "platform": video.get("platform"),
                    "file_id": video.get("file_id"),
                    "first_sent_at": video.get("first_sent_at") or video.get("last_sent_at"),
                    "last_sent_at": video.get("last_sent_at") or video.get("first_sent_at"),
                    "send_count": int(video.get("send_count", 0)),
                    "sender_user_ids": json.dumps(sorted(set(video.get("sender_user_ids", [])))),
                    "invalidated_at": video.get("invalidated_at"),
                },
            )

        connection.commit()
    finally:
        connection.close()

    return len(users), len(videos)


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate the legacy JSON DB into the SQLite DB.")
    parser.add_argument("--json-path", type=Path, default=Path(JSON_DB_PATH))
    parser.add_argument("--sqlite-path", type=Path, default=Path(SQLITE_DB_PATH))
    args = parser.parse_args()

    if not args.json_path.exists():
        print(f"No JSON DB found at {args.json_path}, nothing to migrate.")
        return 0

    user_count, video_count = migrate(args.json_path, args.sqlite_path)
    print(f"Migrated {user_count} users and {video_count} videos into {args.sqlite_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
