from dataclasses import dataclass
from typing import Literal

MediaType = Literal["video", "slideshow_video", "audio", "unsupported"]


@dataclass(slots=True)
class MediaResult:
    platform: str
    source_url: str
    media_type: MediaType
    path: str | None = None
    width: int | None = None
    height: int | None = None
    title: str | None = None
    description: str | None = None
    duration: float | None = None
    performer: str | None = None
    thumbnail_path: str | None = None


class VideoTooLargeError(RuntimeError):
    pass
