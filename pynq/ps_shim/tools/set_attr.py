#!/usr/bin/env python3
"""Set the ring writer's AXI cache attributes at run time (needs root).

  set_attr.py <axcache> <axuser>

The writer applies the new value to the following AXI transactions, so the
stream does not have to stop. This is how the coherence setting is swept
without rebuilding the bitstream.
"""
import mmap
import os
import struct
import sys

BASE = 0x40000000
REG_AX_ATTR = 0x50

if len(sys.argv) != 3:
    raise SystemExit("usage: set_attr.py <axcache 0..15> <axuser 0..31>")

cache = int(sys.argv[1], 0) & 0xF
user = int(sys.argv[2], 0) & 0x1F

fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
m = mmap.mmap(fd, 0x1000, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=BASE)

value = (user << 8) | cache
struct.pack_into("<I", m, REG_AX_ATTR, value)
back = struct.unpack_from("<I", m, REG_AX_ATTR)[0]
print(f"REG_AX_ATTR <- 0x{value:08X} (AXCACHE=0x{cache:X} AXUSER=0x{user:X}), readback=0x{back:08X}")
