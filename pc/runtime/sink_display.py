"""PC-side video sink display skeleton."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class SinkDisplayConfig:
    width: int
    height: int
    pixel_format: str


class SinkDisplay:
    def __init__(self, config: SinkDisplayConfig) -> None:
        self.config = config

    def render_frame(self, frame_bytes: bytes) -> None:
        """Render one frame on host display.

        Implement integration with OpenCV or GStreamer display path.
        """
        raise NotImplementedError("Connect this to a host display sink")

    def close(self) -> None:
        return None
