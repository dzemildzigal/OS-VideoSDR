"""PC-side video source capture skeleton."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator


@dataclass(slots=True)
class SourceCaptureConfig:
    width: int
    height: int
    fps: int
    pixel_format: str


class SourceCapture:
    def __init__(self, config: SourceCaptureConfig) -> None:
        self.config = config

    def frames(self) -> Iterator[bytes]:
        """Yield raw or encoded frame buffers from host source.

        Implement integration with OpenCV, GStreamer, or camera APIs.
        """
        raise NotImplementedError("Connect this to a host video source")

    def close(self) -> None:
        return None
