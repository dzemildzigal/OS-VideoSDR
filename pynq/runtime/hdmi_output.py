"""PYNQ HDMI output runtime backend."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


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
class HdmiOutputConfig:
    width: int
    height: int
    fps: int
    pixel_format: str
    bitstream_path: str | None = None


class HdmiOutput:
    def __init__(self, config: HdmiOutputConfig) -> None:
        self.config = config
        self._overlay = None
        self._hdmi_out = None
        self._started = False
        self._expected_frame_bytes = self.config.width * self.config.height * _pixel_bytes(
            self.config.pixel_format
        )

        try:
            import numpy as np  # type: ignore

            self._np = np
        except Exception as exc:  # pragma: no cover - runtime environment dependent
            raise RuntimeError("numpy is required for HDMI output runtime") from exc

        try:
            from pynq import Overlay  # type: ignore
        except Exception as exc:  # pragma: no cover - runtime environment dependent
            raise RuntimeError("pynq package is required for HDMI output runtime") from exc

        resolved = _resolve_overlay_path(self.config.bitstream_path)
        if resolved:
            self._overlay = Overlay(resolved)

        self._hdmi_out = self._locate_hdmi_out()
        if self._hdmi_out is None:
            raise RuntimeError(
                "Unable to locate HDMI output object in loaded overlay; "
                "expected overlay.video.hdmi_out or overlay.hdmi_out"
            )

    def _locate_hdmi_out(self):
        if self._overlay is None:
            return None

        video_obj = getattr(self._overlay, "video", None)
        if video_obj is not None and hasattr(video_obj, "hdmi_out"):
            return getattr(video_obj, "hdmi_out")

        if hasattr(self._overlay, "hdmi_out"):
            return getattr(self._overlay, "hdmi_out")

        return None

    def _start_if_needed(self) -> None:
        if self._started:
            return

        if hasattr(self._hdmi_out, "configure"):
            try:
                self._hdmi_out.configure()
            except TypeError:
                mode = getattr(self._hdmi_out, "mode", None)
                if mode is None:
                    raise
                self._hdmi_out.configure(mode)

        if hasattr(self._hdmi_out, "start"):
            self._hdmi_out.start()

        self._started = True

    def render_frame(self, frame_bytes: bytes) -> None:
        """Render one frame to HDMI output."""
        self._start_if_needed()

        if len(frame_bytes) < self._expected_frame_bytes:
            payload = frame_bytes + (b"\x00" * (self._expected_frame_bytes - len(frame_bytes)))
        else:
            payload = frame_bytes[: self._expected_frame_bytes]

        frame = self._hdmi_out.newframe()
        target = self._np.asarray(frame)
        flat_target = target.reshape(-1)
        src = self._np.frombuffer(payload, dtype=self._np.uint8)

        copy_count = min(flat_target.size, src.size)
        flat_target[:copy_count] = src[:copy_count]
        if copy_count < flat_target.size:
            flat_target[copy_count:] = 0

        self._hdmi_out.writeframe(frame)

    def close(self) -> None:
        """Release output resources."""
        if self._hdmi_out is not None and self._started and hasattr(self._hdmi_out, "stop"):
            self._hdmi_out.stop()
        self._started = False
        return None
