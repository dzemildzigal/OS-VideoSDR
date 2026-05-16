"""Quick HDMI runtime preflight for OS-VideoSDR on PYNQ.

Checks:
1) optional HDMI capture path
2) optional HDMI output path
3) optional one-frame capture -> render handoff
"""

from __future__ import annotations

import argparse

from hdmi_capture import HdmiCapture, HdmiCaptureConfig
from hdmi_output import HdmiOutput, HdmiOutputConfig


def log(msg: str) -> None:
    print(msg, flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="HDMI runtime preflight")
    parser.add_argument(
        "--bitstream",
        default="",
        help="HDMI-capable overlay bitstream path (.bit) with matching .hwh",
    )
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--fps", type=int, default=10)
    parser.add_argument("--pixel-format", default="RGB888")
    parser.add_argument("--frames", type=int, default=2, help="Number of capture frames to probe")
    parser.add_argument("--skip-capture", action="store_true")
    parser.add_argument("--skip-output", action="store_true")
    parser.add_argument("--render-captured-frame", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    needs_hdmi_overlay = (not args.skip_capture) or (not args.skip_output)
    if needs_hdmi_overlay and not args.bitstream:
        raise ValueError(
            "--bitstream is required for HDMI preflight in this revision. "
            "Provide a .bit whose overlay exposes video.hdmi_in/video.hdmi_out (or hdmi_in/hdmi_out)."
        )

    capture = None
    sink = None
    frame0 = None

    try:
        if not args.skip_capture:
            log("Preflight: opening HDMI capture backend")
            capture = HdmiCapture(
                HdmiCaptureConfig(
                    width=args.width,
                    height=args.height,
                    fps=args.fps,
                    pixel_format=args.pixel_format,
                    bitstream_path=args.bitstream or None,
                )
            )
            gen = capture.frames()
            for i in range(args.frames):
                frame = next(gen)
                if frame0 is None:
                    frame0 = frame
                log(f"Capture frame {i + 1}/{args.frames}: {len(frame)} bytes")

        if not args.skip_output:
            log("Preflight: opening HDMI output backend")
            sink = HdmiOutput(
                HdmiOutputConfig(
                    width=args.width,
                    height=args.height,
                    fps=args.fps,
                    pixel_format=args.pixel_format,
                    bitstream_path=args.bitstream or None,
                )
            )
            if args.render_captured_frame and frame0 is not None:
                sink.render_frame(frame0)
                log("Rendered captured frame to HDMI output")

        log("PREFLIGHT PASS")
        return 0
    finally:
        if capture is not None:
            capture.close()
        if sink is not None:
            sink.close()


if __name__ == "__main__":
    raise SystemExit(main())
