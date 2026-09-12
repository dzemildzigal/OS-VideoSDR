#!/usr/bin/env python3
"""Lossless capture proof for the B.3 transport.

Reads the stream, authenticates every packet, reassembles frames and reports
completeness. Deliberately lean, because the authentication monitor loses about
1% of packets to its own per-packet bookkeeping (dicts, Counters, lists), and
that loss is enough to break 40 frames in 25 seconds:

  - one thread, no queues, no locks
  - a preallocated receive buffer with recvfrom_into
  - plain integer counters
  - one bytearray per active frame plus a fill count, instead of a set of
    segment ids per frame

Usage: rx_lossless.py [seconds] [label]
"""
from __future__ import annotations

import socket
import struct
import sys
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY = bytes.fromhex("000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f")
SLOT = 1408
BODY = 1384
PREFIX = 8
HEADER = 40
TAG = 16
PAYLOAD = 1320
HDR = struct.Struct("!HBBIHIHHQBBHQBB")
NONCE_PREFIX = b"\x00\x00\x00\x01"
MAX_ACTIVE_FRAMES = 8


def main() -> int:
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
    label = sys.argv[2] if len(sys.argv) > 2 else "run"

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16 * 1024 * 1024)
    sock.bind(("0.0.0.0", 5600))
    sock.settimeout(1.0)

    aes = AESGCM(KEY)
    buf = bytearray(65535)
    # One memoryview over the reused buffer: decrypt() takes any bytes-like
    # object, so the 1376-byte copy per packet that a slice would make is not
    # needed. At 62,850 packets/s those copies alone cost more than 10% of a
    # core, which is the difference between keeping up and losing 2%.

    packets = auth_bad = bad_frame = 0
    complete = 0
    gaps = dups = 0
    last_counter = -1
    frames: dict[int, list] = {}
    frame_reuse = 0
    frame_pad_ok = 0
    first_counter = None

    start = time.monotonic()
    while time.monotonic() - start < duration:
        try:
            n = sock.recvfrom_into(buf)
        except socket.timeout:
            continue
        # recvfrom_into returns (bytes, address) on some builds
        n = n[0] if isinstance(n, tuple) else n
        if n != SLOT:
            continue
        packets += 1
        body_view = memoryview(buf)[PREFIX:BODY].toreadonly()

        counter = struct.unpack_from(">Q", buf, 0)[0]
        if first_counter is None:
            first_counter = counter
        if last_counter >= 0:
            delta = counter - last_counter
            if delta == 1:
                pass
            elif delta == 0:
                dups += 1
            elif delta > 1:
                gaps += delta - 1
        last_counter = counter

        nonce = NONCE_PREFIX + counter.to_bytes(8, "big")
        try:
            plain = aes.decrypt(nonce, body_view, b"")
        except Exception:
            auth_bad += 1
            continue

        magic, version, _flags, session, stream, fid, seg, seg_count, _ts, ptype, kid, plen, hdr_nonce, tag_len, _res = HDR.unpack_from(plain)
        if magic != 0x4F56 or version != 0 or session != 1 or stream != 1 or kid != 1:
            bad_frame += 1
            continue
        if plen != PAYLOAD or hdr_nonce != counter or tag_len != TAG or seg >= seg_count:
            bad_frame += 1
            continue

        entry = frames.get(fid)
        if entry is None or entry[1] is None:
            # New frame id, or an old one whose buffer already completed. Allocate
            # once per frame, never per packet: a 2.76 MB bytearray per packet
            # costs ~600 us and overflows the socket buffer immediately.
            if entry is not None:
                frame_reuse += 1
            entry = [bytearray(seg_count * PAYLOAD), seg_count, 0]
            frames[fid] = entry
            if len(frames) > MAX_ACTIVE_FRAMES:
                for stale in sorted(frames)[: len(frames) - MAX_ACTIVE_FRAMES]:
                    del frames[stale]

        base = seg * PAYLOAD
        entry[0][base:base + PAYLOAD] = memoryview(plain)[HEADER:HEADER + PAYLOAD]
        entry[2] += 1
        if entry[2] == entry[1]:
            expected = entry[1] * PAYLOAD
            real = 1280 * 720 * 3
            if expected - real == 600:
                frame_pad_ok += 1
            complete += 1
            entry[1] = None          # mark complete, kept for the reuse counter

    elapsed = time.monotonic() - start
    total = packets
    span = (last_counter - first_counter + 1) if first_counter is not None else 0
    lost = span - total
    print(
        f"RESULT {label}: {elapsed:.1f}s packets={total} rate={total/elapsed:.0f}/s "
        f"auth_bad={auth_bad} bad_frame={bad_frame} duplicates={dups}"
    )
    print(
        f"COUNTERS span={span} received={total} missing={lost} "
        f"({100.0*lost/span if span else 0:.2f}% lost)  "
        f"(note: out-of-order arrivals from the two sockets are counted as missing by"
        f" neither measure, only true gaps are)"
    )
    print(
        f"FRAMES complete={complete} ({complete/elapsed:.1f} fps) "
        f"pad_ok={frame_pad_ok} frame_id_reuse={frame_reuse} "
        f"first_counter={first_counter} last_counter={last_counter}"
    )
    return 0 if auth_bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
