#!/usr/bin/env python3
"""Measure the ring writer's publish and drop rates (needs root).

The publish counter is the ground truth for "the PL is receiving video":
70,560/s means the decimated stream is flowing, 0 means it is not.
"""
import mmap
import os
import struct
import time

fd = os.open("/dev/mem", os.O_RDONLY | os.O_SYNC)
w = mmap.mmap(fd, 0x1000, mmap.MAP_SHARED, mmap.PROT_READ, offset=0x40000000)


def r32(off):
    return struct.unpack_from("<I", w, off)[0]


c0, d0 = r32(0x30), r32(0x2C)
time.sleep(5.0)
c1, d1 = r32(0x30), r32(0x2C)

print(f"status=0x{r32(0x08):08X} control={r32(0x04)} fault={r32(0x3C)} coherent={r32(0x4C)}")
print(f"publish {c0} -> {c1} = {(c1 - c0) / 5.0:.0f} slots/s   (need 70560)")
print(f"drops   {d0} -> {d1} = {(d1 - d0) / 5.0:.0f} /s")
