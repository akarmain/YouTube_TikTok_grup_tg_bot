import argparse
import asyncio
import importlib.util
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from bot.database.json_db import SQLiteDB

REPO_ROOT = Path(__file__).resolve().parents[2]


def load_legacy_json_db(git_ref: str):
    source = subprocess.run(
        ["git", "show", f"{git_ref}:bot/database/json_db.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    module_path = Path(tempfile.mkdtemp()) / "legacy_json_db.py"
    module_path.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("legacy_json_db", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["legacy_json_db"] = module
    spec.loader.exec_module(module)
    return module.JsonDB


async def seed(upsert_video, seed_count: int) -> None:
    for i in range(seed_count):
        await upsert_video(f"https://www.tiktok.com/@seed/video/{i}", f"SEED{i}", i, "tiktok")


async def bench_concurrent_upserts(upsert_video, concurrency: int, distinct_keys: bool) -> float:
    async def one(i: int) -> None:
        video_id = i if distinct_keys else 0
        await upsert_video(
            f"https://www.tiktok.com/@bench/video/{video_id}", f"FILE{i}", i, "tiktok"
        )

    start = time.perf_counter()
    await asyncio.gather(*(one(i) for i in range(concurrency)))
    return time.perf_counter() - start


async def run_scenario(label: str, db, seed_count: int, concurrency: int, distinct_keys: bool) -> float:
    if seed_count:
        await seed(db.upsert_video, seed_count)
    elapsed = await bench_concurrent_upserts(db.upsert_video, concurrency, distinct_keys)
    qps = concurrency / elapsed if elapsed > 0 else float("inf")
    print(
        f"{label:<28} seed={seed_count:<6} n={concurrency:<5} "
        f"distinct_keys={str(distinct_keys):<5} elapsed={elapsed:.4f}s qps={qps:,.0f}"
    )
    return elapsed


async def main() -> None:
    parser = argparse.ArgumentParser(description="Compare legacy JsonDB vs SQLiteDB upsert throughput.")
    parser.add_argument("--concurrency", type=int, default=200)
    parser.add_argument("--seed", type=int, default=2000)
    parser.add_argument("--legacy-ref", default="master")
    args = parser.parse_args()

    JsonDB = load_legacy_json_db(args.legacy_ref)

    with tempfile.TemporaryDirectory() as tmp_dir:
        json_db = JsonDB(str(Path(tmp_dir) / "legacy.json"))
        sqlite_db = SQLiteDB(str(Path(tmp_dir) / "new.sqlite3"))

        print("--- same-key contention (all writers hit one video row) ---")
        await run_scenario("legacy JsonDB", json_db, args.seed, args.concurrency, distinct_keys=False)
        await run_scenario("SQLiteDB", sqlite_db, args.seed, args.concurrency, distinct_keys=False)

        json_db = JsonDB(str(Path(tmp_dir) / "legacy2.json"))
        sqlite_db = SQLiteDB(str(Path(tmp_dir) / "new2.sqlite3"))

        print("--- distinct keys (each writer creates its own video row) ---")
        await run_scenario("legacy JsonDB", json_db, args.seed, args.concurrency, distinct_keys=True)
        await run_scenario("SQLiteDB", sqlite_db, args.seed, args.concurrency, distinct_keys=True)

        await sqlite_db.close()


if __name__ == "__main__":
    asyncio.run(main())
