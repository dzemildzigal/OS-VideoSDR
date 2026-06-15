"""Quick RX test for tx_daemon UDP stream with OpenCV display.

This script is intentionally simple and matches tx_daemon packet format:
  - 6-byte UDP prefix per datagram: [frame_id(4), seq(2)] big-endian
  - payload bytes after the 6-byte prefix are rendered as grayscale pixels

Notes:
  - This is a transport/visual sanity test for the current daemon output.
  - If payload is encrypted ciphertext, displayed image content will look noisy.
"""

from __future__ import annotations

import argparse
import socket
import time
from typing import Optional


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Receive tx_daemon UDP frames and display with OpenCV")
    p.add_argument("--bind-ip", default="0.0.0.0", help="Local bind IP")
    p.add_argument("--port", type=int, default=5600, help="Local UDP port")
    p.add_argument("--width", type=int, default=40, help="Frame width in pixels")
    p.add_argument("--height", type=int, default=30, help="Frame height in pixels")
    p.add_argument("--scale", type=int, default=16, help="Display up-scale factor")
    p.add_argument("--payload-offset", type=int, default=0, help="Extra bytes to skip in payload")
    p.add_argument("--max-frames", type=int, default=0, help="Stop after N frames (0 = infinite)")
    p.add_argument("--socket-timeout", type=float, default=1.0, help="UDP recv timeout (seconds)")
    return p.parse_args()


def to_gray_image(payload: bytes, width: int, height: int) -> bytes:
    pixel_count = width * height
    if len(payload) >= pixel_count:
        return payload[:pixel_count]
    return payload + (b"\x00" * (pixel_count - len(payload)))


def main() -> None:
    args = parse_args()

    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except Exception as exc:
        raise RuntimeError("OpenCV test requires opencv-python and numpy") from exc

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.bind_ip, args.port))
    sock.settimeout(args.socket_timeout)

    window = "tx_daemon_rx_test"
    cv2.namedWindow(window, cv2.WINDOW_NORMAL)

    frames = 0
    bytes_rx = 0
    last_frame_id: Optional[int] = None
    t0 = time.time()

    print(f"[rx_test] Listening on {args.bind_ip}:{args.port}")
    print("[rx_test] Press 'q' in the window to quit.")

    try:
        while args.max_frames <= 0 or frames < args.max_frames:
            try:
                datagram, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue

            if len(datagram) < 6:
                continue

            frame_id = int.from_bytes(datagram[0:4], "big")
            seq = int.from_bytes(datagram[4:6], "big")
            payload = datagram[6 + args.payload_offset :]
            if not payload:
                continue

            gray = to_gray_image(payload, args.width, args.height)
            img = np.frombuffer(gray, dtype=np.uint8).reshape((args.height, args.width))

            if args.scale > 1:
                img = cv2.resize(
                    img,
                    (args.width * args.scale, args.height * args.scale),
                    interpolation=cv2.INTER_NEAREST,
                )

            cv2.imshow(window, img)
            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                break

            frames += 1
            bytes_rx += len(payload)
            if frames % 100 == 0:
                dt = max(time.time() - t0, 1e-6)
                mbps = (bytes_rx * 8.0) / (dt * 1_000_000.0)
                fid_delta = 0 if last_frame_id is None else (frame_id - last_frame_id)
                last_frame_id = frame_id
                print(
                    f"[rx_test] frames={frames} bytes={bytes_rx} mbps={mbps:.2f} "
                    f"frame_id={frame_id} seq={seq} fid_delta={fid_delta} from={addr[0]}"
                )

    except KeyboardInterrupt:
        pass
    finally:
        cv2.destroyAllWindows()
        sock.close()
        print(f"[rx_test] Stopped. frames={frames} bytes={bytes_rx}")


if __name__ == "__main__":
    main()
