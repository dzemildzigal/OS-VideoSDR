"""PYNQ HDMI output interface skeleton."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class HdmiOutputConfig:
    width: int
    height: int
    fps: int
    pixel_format: str


class HdmiOutput:
    def __init__(self, config: HdmiOutputConfig) -> None:
        self.config = config

    def render_frame(self, frame_bytes: bytes) -> None:
        """Render one frame to HDMI output.

        Implement board-specific output plumbing in this method.
        """
        raise NotImplementedError("Connect this to the PYNQ HDMI output pipeline")

    def close(self) -> None:
        """Release output resources."""
        return None
