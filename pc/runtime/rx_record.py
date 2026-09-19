#!/usr/bin/env python3
"""Record the B.3 stream to a video file instead of showing it live.

No window is opened, so the picture can never contain itself (the HDMI output
mirrors the desktop a live window would sit on, which produced the
hall-of-mirrors effect).

Frames are written at a fixed 30 fps from the newest frame buffer. A frame
buffer starts as a copy of the last written picture, so a segment that never
arrived keeps the previous frame's content instead of leaving a hole. The tool
also reports how complete the written frames were.

Usage: rx_record.py [seconds] [output.mp4]
"""
from __future__ import annotations

import socket
import struct
import sys
import time
from pathlib import Path

import cv2
import numpy as np
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY = bytes.fromhex("000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f")
SLOT, BODY, PREFIX, HEADER, TAG, PAYLOAD = 1408, 1384, 8, 40, 16, 1320
HDR = struct.Struct("!HBBIHIHHQBBHQBB")
NONCE_PREFIX = b"\x00\x00\x00\x01"
WIDTH, HEIGHT, FPS = 1280, 720, 30.0
REAL_BYTES = WIDTH * HEIGHT * 3
MAX_ACTIVE = 8
OUT_DIR = Path(__file__).resolve().parents[2] / "recordings"


def open_writer(path: Path):
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (WIDTH, HEIGHT))
    if writer.isOpened():
        return writer, path
    writer.release()
    path = path.with_suffix(".avi")
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), FPS, (WIDTH, HEIGHT))
    if writer.isOpened():
        return writer, path
    return None, path


def main() -> int:
    argv = sys.argv[1:]
    duration = 60.0
    if argv and argv[0].replace(".", "").isdigit():
        duration = float(argv[0])
        argv = argv[1:]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    requested = Path(argv[0]) if argv else OUT_DIR / f"osv_720p30_{stamp}.mp4"
    writer, out_path = open_writer(requested)
    if writer is None:
        print("ERROR: no video writer available (tried mp4v and MJPG)")
        return 1

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 64 * 1024 * 1024)
    sock.bind(("0.0.0.0", 5600))
    sock.setblocking(False)

    aes = AESGCM(KEY)
    recv = bytearray(65535)
    frames: dict[int, bytearray] = {}
    seen: dict[int, bytearray] = {}
    seg_counts: dict[int, int] = {}
    newest_fid = -1
    last_written = bytearray(REAL_BYTES)

    packets = auth_bad = malformed = written = 0
    completeness: list[float] = []
    start = time.monotonic()
    next_write = start
    last_report = start

    while time.monotonic() - start < duration:
        got = 0
        while got < 800:
            try:
                n = sock.recvfrom_into(recv)
            except (BlockingIOError, socket.timeout):
                break
            got += 1
            n = n[0] if isinstance(n, tuple) else n
            if n != SLOT:
                continue
            packets += 1
            counter = struct.unpack_from(">Q", recv, 0)[0]
            nonce = NONCE_PREFIX + counter.to_bytes(8, "big")
            try:
                plain = aes.decrypt(nonce, memoryview(recv)[PREFIX:BODY].toreadonly(), b"")
            except Exception:
                auth_bad += 1
                continue
            magic, ver, _fl, sess, stream, fid, seg, seg_count, _ts, _pt, _kid, plen, hdr_n, _tag, _r = HDR.unpack_from(plain)
            if (magic != 0x4F56 or ver != 0 or sess != 1 or stream != 1
                    or plen != PAYLOAD or hdr_n != counter or seg >= seg_count or seg_count == 0):
                malformed += 1
                continue

            fbuf = frames.get(fid)
            if fbuf is None:
                if len(frames) > MAX_ACTIVE:
                    for old_fid in sorted(frames)[: len(frames) - MAX_ACTIVE]:
                        frames.pop(old_fid, None)
                        seen.pop(old_fid, None)
                        seg_counts.pop(old_fid, None)
                fbuf = bytearray(last_written)      # inherit the previous picture
                frames[fid] = fbuf
                seen[fid] = bytearray(seg_count)
                seg_counts[fid] = seg_count
            off = seg * PAYLOAD
            if off < REAL_BYTES:
                fbuf[off:off + PAYLOAD] = plain[HEADER:HEADER + PAYLOAD]
                seen[fid][seg] = 1
        if got == 0:
            time.sleep(0.0005)

        now = time.monotonic()
        if now >= next_write:
            next_write += 1.0 / FPS
            if frames:
                newest_fid = max(frames)
                mv = memoryview(frames[newest_fid])[:REAL_BYTES]
                arr = np.frombuffer(mv, dtype=np.uint8).reshape((HEIGHT, WIDTH, 3))
                img_bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
                del arr, mv
                writer.write(img_bgr)
                last_written[:] = frames[newest_fid]
                written += 1
                total = seg_counts[newest_fid]
                completeness.append(sum(seen[newest_fid]) / total)

        if now - last_report >= 5.0:
            el = now - start
            mean_c = 100.0 * sum(completeness) / len(completeness) if completeness else 0.0
            print(f"[rec] {el:5.0f}s packets={packets} ({packets/el:.0f}/s) auth_bad={auth_bad} "
                  f"malformed={malformed} written={written} mean_completeness={mean_c:.1f}%",
                  flush=True)
            last_report = now

    writer.release()
    size = out_path.stat().st_size if out_path.exists() else 0
    mean_c = 100.0 * sum(completeness) / len(completeness) if completeness else 0.0
    print(f"RECORDING {out_path}")
    print(f"  written_frames={written} at {FPS:.0f} fps  size={size} bytes ({size/1e6:.1f} MB)")
    print(f"  packets={packets} auth_bad={auth_bad} malformed={malformed}")
    print(f"  mean_frame_completeness={mean_c:.1f}%  (100% = every segment of every written frame arrived)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
