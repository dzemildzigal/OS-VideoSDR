#!/usr/bin/env python3
"""Capture the first complete frame from the B.3 stream and save it as a PNG.

Also the strongest end-to-end check available: the reconstructed pixels can be
looked at directly.

Usage: rx_snapshot.py [seconds] [out.png]
"""
from __future__ import annotations

import socket
import struct
import sys
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY = bytes.fromhex("000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f")
SLOT, BODY, PREFIX, HEADER, TAG, PAYLOAD = 1408, 1384, 8, 40, 16, 1320
HDR = struct.Struct("!HBBIHIHHQBBHQBB")
NONCE_PREFIX = b"\x00\x00\x00\x01"
WIDTH, HEIGHT = 1280, 720
REAL_BYTES = WIDTH * HEIGHT * 3
PAD_BYTES = 600            # 2095 segments x 1320 = 2,765,400; frame = 2,764,800
MAX_ACTIVE = 8


def main() -> int:
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
    out = sys.argv[2] if len(sys.argv) > 2 else "frame.png"

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 64 * 1024 * 1024)
    sock.bind(("0.0.0.0", 5600))
    sock.settimeout(1.0)
    aes = AESGCM(KEY)
    buf = bytearray(65535)
    frames: dict[int, list] = {}
    packets = auth_bad = complete = 0
    start = time.monotonic()

    while time.monotonic() - start < duration:
        try:
            n = sock.recvfrom_into(buf)
        except socket.timeout:
            continue
        n = n[0] if isinstance(n, tuple) else n
        if n != SLOT:
            continue
        packets += 1
        counter = struct.unpack_from(">Q", buf, 0)[0]
        nonce = NONCE_PREFIX + counter.to_bytes(8, "big")
        try:
            plain = aes.decrypt(nonce, memoryview(buf)[PREFIX:BODY].toreadonly(), b"")
        except Exception:
            auth_bad += 1
            continue
        magic, ver, _f, sess, stream, fid, seg, seg_count, _ts, _ptype, _kid, plen, hdr_n, tag_len, _r = HDR.unpack_from(plain)
        if magic != 0x4F56 or ver != 0 or sess != 1 or stream != 1 or plen != PAYLOAD or hdr_n != counter or seg >= seg_count:
            continue

        entry = frames.get(fid)
        if entry is None or entry[1] is None:
            entry = [bytearray(seg_count * PAYLOAD), seg_count, 0]
            frames[fid] = entry
            if len(frames) > MAX_ACTIVE:
                for stale in sorted(frames)[: len(frames) - MAX_ACTIVE]:
                    del frames[stale]
        base = seg * PAYLOAD
        entry[0][base:base + PAYLOAD] = memoryview(plain)[HEADER:HEADER + PAYLOAD]
        entry[2] += 1
        if entry[2] == entry[1]:
            complete += 1
            entry[1] = None
            raw = bytes(entry[0])
            if len(raw) - REAL_BYTES != PAD_BYTES:
                print(f"frame {fid}: unexpected size {len(raw)}")
                continue
            import numpy as np
            img = np.frombuffer(raw[:REAL_BYTES], dtype=np.uint8).reshape((HEIGHT, WIDTH, 3))
            import cv2
            cv2.imwrite(out, cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
            print(f"SAVED {out}: frame_id={fid} segments={entry[1] if False else seg_count} "
                  f"packets={packets} auth_bad={auth_bad} complete={complete}")
            print(f"  pixel samples RGB: centre={tuple(int(v) for v in img[HEIGHT//2, WIDTH//2])} "
                  f"corner={tuple(int(v) for v in img[10, 10])} "
                  f"mean={img.mean():.1f}")
            return 0

    print(f"no complete frame in {duration}s (packets={packets} auth_bad={auth_bad} complete={complete})")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
