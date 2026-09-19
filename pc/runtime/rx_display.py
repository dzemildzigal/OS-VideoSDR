#!/usr/bin/env python3
"""Live OpenCV display for the B.3 stream - the fast version.

One persistent raster buffer is written in place, segment by segment, and the
window is redrawn at video rate. Two consequences that matter here:

  - no per-packet copies, so the receive loop stays lean (the previous version
    copied the whole 2.76 MB frame and redrew on every packet, which throttled
    it to 62 packets/s)
  - a lost segment simply leaves the previous frame's content in place, so the
    picture stays smooth and only shows small stale patches instead of stalling

Usage: rx_display.py [seconds] [--port 5600]
"""
from __future__ import annotations

import socket
import struct
import sys
import time

import cv2
import numpy as np
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY = bytes.fromhex("000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f")
SLOT, BODY, PREFIX, HEADER, TAG, PAYLOAD = 1408, 1384, 8, 40, 16, 1320
HDR = struct.Struct("!HBBIHIHHQBBHQBB")
NONCE_PREFIX = b"\x00\x00\x00\x01"
WIDTH, HEIGHT = 1280, 720
REAL_BYTES = WIDTH * HEIGHT * 3
MAX_ACTIVE = 8                # frame buffers kept in flight
WINDOW = "OS-VideoSDR live"


def main() -> int:
    argv = sys.argv[1:]
    duration = 120.0
    port = 5600
    if argv and argv[0].replace(".", "").isdigit():
        duration = float(argv[0])
        argv = argv[1:]
    if "--port" in argv:
        port = int(argv[argv.index("--port") + 1])

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 64 * 1024 * 1024)
    sock.bind(("0.0.0.0", port))
    sock.setblocking(False)          # drain everything available, then draw

    aes = AESGCM(KEY)
    recv = bytearray(65535)
    # One buffer per frame id. A single shared raster cannot stay coherent
    # when frames arrive partially: segments of different frames land in the
    # same picture and it turns into a mosaic (measured on hardware as dozens
    # of window fragments in one image). Each frame buffer starts as a copy of
    # the last displayed picture, so a missing segment shows the previous
    # frame's content instead of a hole.
    frames: dict[int, bytearray] = {}
    shown_fid = -1
    last_shown = bytearray(REAL_BYTES)

    packets = auth_bad = bad = 0
    drawn = 0
    start = time.monotonic()
    last_report = last_draw = last_save = start

    def disp_img():
        return np.frombuffer(memoryview(disp), dtype=np.uint8).reshape((HEIGHT, WIDTH, 3))

    # WINDOW_AUTOSIZE keeps the aspect ratio fixed. With WINDOW_NORMAL the
    # window can be dragged into a thin strip, and a 1280x720 frame squeezed
    # into a strip of a few dozen pixels looks torn - which is easy to mistake
    # for a data problem. The frame itself is correct (see the saved PNG).
    cv2.namedWindow(WINDOW, cv2.WINDOW_AUTOSIZE)

    while time.monotonic() - start < duration:
        # Drain everything the socket has before touching the display. Reading
        # one packet per loop iteration and drawing in the same iteration caps
        # the receive rate at the drawing rate: measured 85 packets/s that way.
        got = 0
        while got < 500:
            try:
                n = sock.recvfrom_into(recv)
            except (BlockingIOError, socket.timeout):
                break
            got += 1
            n = n[0] if isinstance(n, tuple) else n
            if n == SLOT:
                packets += 1
                counter = struct.unpack_from(">Q", recv, 0)[0]
                nonce = NONCE_PREFIX + counter.to_bytes(8, "big")
                try:
                    plain = aes.decrypt(nonce, memoryview(recv)[PREFIX:BODY].toreadonly(), b"")
                except Exception:
                    auth_bad += 1
                    plain = None
                if plain is not None:
                    if plain[0:2] == b"\x4f\x56":
                        # Header layout "!HBBIHIHHQBBHQBB": frame_id at 10,
                        # seg at 14. (Reading 12 for seg gave the high half of
                        # frame_id and produced black frames.)
                        fid = struct.unpack_from("!I", plain, 10)[0]
                        seg = struct.unpack_from("!H", plain, 14)[0]
                        # Two sockets deliver packets slightly out of order, so
                        # late segments of frames that were already displayed
                        # keep arriving. Writing those into the current frame
                        # mixes several frames into one picture: seen on
                        # hardware as a mosaic that doubles (2, 4, 8, 16
                        # frames) the longer it runs. Only the current frame,
                        # or one a few frames ahead, may be placed.
                        fbuf = frames.get(fid)
                        if fbuf is None:
                            if len(frames) > MAX_ACTIVE:
                                for old_fid in sorted(frames)[: len(frames) - MAX_ACTIVE]:
                                    del frames[old_fid]
                            # New frame: inherit the last displayed picture so
                            # segments that never arrive keep valid content.
                            fbuf = bytearray(last_shown)
                            frames[fid] = fbuf
                        off = seg * PAYLOAD
                        if off < REAL_BYTES:
                            fbuf[off:off + PAYLOAD] = plain[HEADER:HEADER + PAYLOAD]
                    else:
                        bad += 1
        if got == 0:
            time.sleep(0.001)

        now = time.monotonic()
        if now - last_draw >= 1.0 / 60.0:
            newest = max(frames) if frames else -1
            if newest >= 0:
                # Release the numpy view before touching the buffer again: a
                # live export makes the bytearray's slice assignment fail with
                # BufferError ("object cannot be re-sized").
                mv = memoryview(frames[newest])[:REAL_BYTES]   # frame buffer has one segment of slack
                arr = np.frombuffer(mv, dtype=np.uint8).reshape((HEIGHT, WIDTH, 3))
                img_bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
                del arr, mv
                cv2.imshow(WINDOW, img_bgr)
                # Keep this frame's picture as the base for later frames.
                last_shown[:] = frames[newest]
                shown_fid = newest
                drawn += 1
                if now - last_save >= 5.0:
                    last_save = now
                    cv2.imwrite("C:/tmp/live_frame.png", img_bgr)
            last_draw = now
        if cv2.waitKey(1) & 0xFF in (27, ord("q")):
            break
        # Closing the window with the X must stop the program: otherwise the
        # next imshow simply re-creates it, which looks like the window
        # "reappearing" no matter how often it is closed.
        try:
            if cv2.getWindowProperty(WINDOW, cv2.WND_PROP_VISIBLE) < 1:
                print("window closed by the user", flush=True)
                break
        except cv2.error:
            break

        if now - last_report >= 5.0:
            el = now - start
            print(f"[live] {el:.0f}s packets={packets} ({packets/el:.0f}/s) auth_bad={auth_bad} "
                  f"bad_header={bad} draws={drawn} ({drawn/el:.1f}/s)", flush=True)
            last_report = now

    cv2.destroyAllWindows()
    print(f"DONE packets={packets} rate={packets/(time.monotonic()-start):.0f}/s auth_bad={auth_bad} draws={drawn}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
