"""Reusable ffmpeg/yt-dlp helpers shared by the TikTok, YouTube and Instagram downloaders."""

import os
import subprocess
from pathlib import Path
from typing import Any

from yt_dlp import YoutubeDL

from bot.ffmpeg import ffmpeg_command, run_ffmpeg
from bot.settings import MAX_VIDEO_SIZE_BYTES

SLIDESHOW_WIDTH = 720
SLIDESHOW_HEIGHT = 1280
NO_AUDIO_SECONDS_PER_PHOTO = 5.0
LONG_AUDIO_THRESHOLD = 40.0
LONG_AUDIO_SECONDS_PER_PHOTO = 7.0
# (max height, crf) steps tried in order until the file fits the Telegram limit.
COMPRESSION_STEPS = ((720, 23), (480, 26), (360, 28))


def ffprobe_duration(path: str) -> float | None:
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        duration = float(out)
        return duration if duration > 0 else None
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None


def photo_durations(photo_count: int, audio_duration: float | None) -> list[float]:
    """Seconds each photo stays on screen; sum equals the audio duration when audio exists."""
    if photo_count <= 0:
        raise ValueError("photo_count must be positive")
    if not audio_duration or audio_duration <= 0:
        return [NO_AUDIO_SECONDS_PER_PHOTO] * photo_count
    if (
        audio_duration <= LONG_AUDIO_THRESHOLD
        or LONG_AUDIO_SECONDS_PER_PHOTO * photo_count > audio_duration
    ):
        return [audio_duration / photo_count] * photo_count
    head = LONG_AUDIO_SECONDS_PER_PHOTO * (photo_count - 1)
    return [LONG_AUDIO_SECONDS_PER_PHOTO] * (photo_count - 1) + [audio_duration - head]


def build_slideshow(image_paths: list[str], audio_path: str | None, out_path: str) -> None:
    """Builds one vertical 720x1280 mp4 (H.264/AAC, yuv420p, +faststart) from photos + optional audio."""
    if not image_paths:
        raise RuntimeError("no images provided for slideshow")

    audio_duration = ffprobe_duration(audio_path) if audio_path else None
    if audio_path and audio_duration is None:
        audio_path = None
    durations = photo_durations(len(image_paths), audio_duration)
    total_duration = sum(durations)

    inputs: list[str] = []
    for image, duration in zip(image_paths, durations):
        inputs += ["-loop", "1", "-t", f"{duration:.3f}", "-i", image]

    audio_args: list[str] = []
    audio_input_index = len(image_paths)
    if audio_path:
        inputs += ["-i", audio_path]
        audio_args = ["-map", f"{audio_input_index}:a:0", "-c:a", "aac", "-b:a", "128k"]

    size = f"{SLIDESHOW_WIDTH}:{SLIDESHOW_HEIGHT}"
    # out_range=limited: photos decode as full-range yuvj420p; Telegram expects plain yuv420p.
    per_image_filter = (
        f"scale={size}:force_original_aspect_ratio=decrease:out_range=limited,"
        f"pad={size}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps=30,"
        "format=yuv420p,setpts=PTS-STARTPTS"
    )
    if len(image_paths) == 1:
        filter_parts = [f"[0:v]{per_image_filter}[vout]"]
    else:
        filter_parts = [
            f"[{index}:v]{per_image_filter}[v{index}]"
            for index in range(len(image_paths))
        ]
        concat_inputs = "".join(f"[v{index}]" for index in range(len(image_paths)))
        filter_parts.append(f"{concat_inputs}concat=n={len(image_paths)}:v=1:a=0[vout]")
    filter_complex = ";".join(filter_parts)

    command = ffmpeg_command(
        "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", "[vout]",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        *audio_args,
        "-movflags", "+faststart",
        "-avoid_negative_ts", "make_zero",
        "-t", f"{total_duration:.3f}",
        out_path,
    )
    process = run_ffmpeg(command, capture_output=True, text=True)

    if process.returncode != 0 or not os.path.exists(out_path):
        tail = (process.stderr or "").strip().splitlines()[-3:]
        raise RuntimeError(f"ffmpeg slideshow failed: {' | '.join(tail) or 'unknown error'}")


def compress_to_limit(
    path: str,
    limit_bytes: int = MAX_VIDEO_SIZE_BYTES,
    steps: tuple[tuple[int, int], ...] = COMPRESSION_STEPS,
) -> str | None:
    """Recompresses through the configured steps until the file fits.

    On success the original file is replaced (deleted); on failure the original is kept.
    """
    if os.path.getsize(path) <= limit_bytes:
        return path
    src = Path(path)
    for height, crf in steps:
        dest = src.with_name(f"{src.stem}.c{height}.mp4")
        command = ffmpeg_command(
            "-y",
            "-i", str(src),
            "-vf", f"scale=-2:min({height}\\,ih)",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", str(crf),
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            str(dest),
        )
        process = run_ffmpeg(command, capture_output=True, text=True)
        if process.returncode == 0 and dest.exists() and dest.stat().st_size <= limit_bytes:
            src.unlink(missing_ok=True)
            return str(dest)
        dest.unlink(missing_ok=True)
    return None


def extract_dimensions(info: dict[str, Any]) -> tuple[int | None, int | None]:
    width = info.get("width")
    height = info.get("height")
    if width and height:
        return width, height

    for key in ("requested_formats", "formats"):
        for fmt in info.get(key) or []:
            if fmt.get("width") and fmt.get("height"):
                return fmt["width"], fmt["height"]

    return None, None


def resolve_output_path(info: dict[str, Any], ydl: YoutubeDL) -> str:
    candidates: list[Path] = []
    for item in info.get("requested_downloads") or []:
        filepath = item.get("filepath")
        if filepath:
            candidates.append(Path(filepath))

    filename = info.get("_filename")
    if filename:
        candidates.append(Path(filename))

    prepared_filename = ydl.prepare_filename(info)
    if prepared_filename:
        prepared_path = Path(prepared_filename)
        candidates.append(prepared_path)
        candidates.append(prepared_path.with_suffix(".mp4"))

    for path in candidates:
        if path.exists():
            return str(path)

    raise RuntimeError("Downloaded file was not found on disk.")


def _extract_selected_codecs(info: dict[str, Any]) -> tuple[str | None, str | None, float | None]:
    requested_formats = info.get("requested_formats") or []
    video_codec: str | None = None
    audio_codec: str | None = None
    fps: float | None = None

    for fmt in requested_formats:
        if not video_codec and fmt.get("vcodec") and fmt.get("vcodec") != "none":
            video_codec = str(fmt.get("vcodec"))
            fps = fmt.get("fps")
        if not audio_codec and fmt.get("acodec") and fmt.get("acodec") != "none":
            audio_codec = str(fmt.get("acodec"))

    if not video_codec and info.get("vcodec") and info.get("vcodec") != "none":
        video_codec = str(info.get("vcodec"))
        fps = info.get("fps")
    if not audio_codec and info.get("acodec") and info.get("acodec") != "none":
        audio_codec = str(info.get("acodec"))

    return video_codec, audio_codec, fps


def needs_telegram_normalization(info: dict[str, Any]) -> bool:
    video_codec, audio_codec, fps = _extract_selected_codecs(info)
    ext = str(info.get("ext", "")).lower()

    video_ok = bool(video_codec and (video_codec.startswith("avc1") or video_codec.startswith("h264")))
    audio_ok = audio_codec is None or audio_codec.startswith("mp4a") or audio_codec.startswith("aac")
    fps_ok = fps is None or float(fps) <= 30.0
    container_ok = ext == "mp4"

    return not (video_ok and audio_ok and fps_ok and container_ok)


def _normalization_scale_args(max_height: int | None) -> list[str]:
    if max_height is None:
        return []
    return ["-vf", f"scale=-2:min({max_height}\\,ih)"]


def normalize_for_telegram(
    path: str,
    *,
    max_height: int | None = None,
    crf: int = 20,
) -> str:
    src_path = Path(path)
    normalized_path = src_path.with_name(f"{src_path.stem}.tgfix.mp4")
    scale_args = _normalization_scale_args(max_height)
    relaxed_crf = max(22, crf + 2)

    strict_command = ffmpeg_command(
        "-y",
        "-i", str(src_path),
        "-map", "0:v:0",
        "-map", "0:a:0?",
        *scale_args,
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-profile:v", "high",
        "-level:v", "4.1",
        "-preset", "veryfast",
        "-crf", str(crf),
        "-r", "30",
        "-g", "60",
        "-keyint_min", "60",
        "-sc_threshold", "0",
        "-c:a", "aac",
        "-b:a", "160k",
        "-ar", "48000",
        "-ac", "2",
        "-movflags", "+faststart",
        str(normalized_path),
    )
    relaxed_command = ffmpeg_command(
        "-y",
        "-i", str(src_path),
        "-map", "0:v:0",
        "-map", "0:a:0?",
        *scale_args,
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-preset", "veryfast",
        "-crf", str(relaxed_crf),
        "-r", "30",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        str(normalized_path),
    )

    last_output: list[str] = []
    for command in (strict_command, relaxed_command):
        try:
            process = run_ffmpeg(command, capture_output=True, text=True, check=False)
        except FileNotFoundError as exc:
            raise RuntimeError("ffmpeg not found. Install ffmpeg and retry.") from exc

        if process.returncode == 0 and normalized_path.exists():
            src_path.unlink(missing_ok=True)
            normalized_path.replace(src_path.with_suffix(".mp4"))
            return str(src_path.with_suffix(".mp4"))

        output = (process.stderr or process.stdout or "").strip().splitlines()
        if output:
            last_output = output

    tail = " | ".join(last_output[-3:]) if last_output else "unknown ffmpeg error"
    raise RuntimeError(f"ffmpeg normalization failed: {tail}")
