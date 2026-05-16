"""PYNQ HDMI capture runtime backend.

This module provides a minimal board-side capture abstraction that can be used
by runtime entrypoints without binding to a single overlay layout.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


def _resolve_overlay_path(value: str | None) -> str | None:
    if not value:
        return None

    candidate = Path(value)
    if candidate.is_absolute():
        return str(candidate)

    project_root = Path(__file__).resolve().parents[2]
    search = [
        (Path.cwd() / candidate).resolve(),
        (project_root / candidate).resolve(),
        (project_root / "pynq" / candidate).resolve(),
        (project_root.parent / "AES-256-SystemVerilog" / candidate).resolve(),
        (project_root.parent / "AES-256-SystemVerilog" / "pynq" / candidate).resolve(),
    ]
    for path in search:
        if path.exists():
            return str(path)

    return str((project_root / candidate).resolve())


def _pixel_bytes(pixel_format: str) -> int:
    fmt = pixel_format.upper()
    if "RGB" in fmt:
        return 3
    if "YUV" in fmt:
        return 2
    return 1


@dataclass(slots=True)
class HdmiCaptureConfig:
    width: int
    height: int
    fps: int
    pixel_format: str
    bitstream_path: str | None = None
    frame_timeout_s: float = 2.0


class HdmiCapture:
    def __init__(self, config: HdmiCaptureConfig) -> None:
        self.config = config
        self._overlay = None
        self._hdmi_in = None
        self._started = False
        self._expected_frame_bytes = self.config.width * self.config.height * _pixel_bytes(
            self.config.pixel_format
        )

        try:
            import numpy as np  # type: ignore

            self._np = np
        except Exception as exc:  # pragma: no cover - runtime environment dependent
            raise RuntimeError("numpy is required for HDMI capture runtime") from exc

        try:
            from pynq import Overlay  # type: ignore
        except Exception as exc:  # pragma: no cover - runtime environment dependent
            raise RuntimeError("pynq package is required for HDMI capture runtime") from exc

        resolved = _resolve_overlay_path(self.config.bitstream_path)
        if resolved:
            self._overlay = Overlay(resolved)

        self._hdmi_in = self._locate_hdmi_in()
        if self._hdmi_in is None:
            raise RuntimeError(
                "Unable to locate HDMI input object in loaded overlay; "
                "expected overlay.video.hdmi_in or overlay.hdmi_in"
            )

    def _locate_hdmi_in(self):
        if self._overlay is None:
            return None

        video_obj = getattr(self._overlay, "video", None)
        if video_obj is not None and hasattr(video_obj, "hdmi_in"):
            return getattr(video_obj, "hdmi_in")

        if hasattr(self._overlay, "hdmi_in"):
            return getattr(self._overlay, "hdmi_in")

        return None

    def _start_if_needed(self) -> None:
        if self._started:
            return

        if hasattr(self._hdmi_in, "configure"):
            try:
                self._hdmi_in.configure()
            except TypeError:
                # Some overlays expect an explicit mode argument.
                mode = getattr(self._hdmi_in, "mode", None)
                if mode is None:
                    raise
                self._hdmi_in.configure(mode)

        if hasattr(self._hdmi_in, "start"):
            self._hdmi_in.start()

        self._started = True

    def frames(self) -> Iterator[bytes]:
        """Yield raw frame buffers from HDMI input."""
        self._start_if_needed()

        while True:
            frame = self._hdmi_in.readframe()
            try:
                arr = self._np.asarray(frame)
                frame_bytes = arr.tobytes()
            finally:
                if hasattr(frame, "freebuffer"):
                    frame.freebuffer()

            if len(frame_bytes) < self._expected_frame_bytes:
                frame_bytes = frame_bytes + (b"\x00" * (self._expected_frame_bytes - len(frame_bytes)))
            elif len(frame_bytes) > self._expected_frame_bytes:
                frame_bytes = frame_bytes[: self._expected_frame_bytes]

            yield frame_bytes

    def close(self) -> None:
        """Release capture resources."""
        if self._hdmi_in is not None and self._started and hasattr(self._hdmi_in, "stop"):
            self._hdmi_in.stop()
        self._started = False
        return None
