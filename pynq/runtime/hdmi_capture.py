"""PYNQ HDMI capture interface skeleton."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator


@dataclass(slots=True)
class HdmiCaptureConfig:
    width: int
    height: int
    fps: int
    pixel_format: str


class HdmiCapture:
    def __init__(self, config: HdmiCaptureConfig) -> None:
        self.config = config

    def frames(self) -> Iterator[bytes]:
        """Yield raw frame buffers from HDMI input.

        Implement board-specific capture plumbing in this method.
        """
        raise NotImplementedError("Connect this to the PYNQ HDMI capture pipeline")

    def close(self) -> None:
        """Release capture resources."""
        return None
