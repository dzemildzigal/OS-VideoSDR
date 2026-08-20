#!/usr/bin/env python3
"""rd_dbg.py - dump the sequencer mirrors + the new tag-path debug probes
for the most recently completed packet (nonce-1).

Reads (via /dev/mem, no overlay reload):
  sequencer @ 0x40001000:
    0x40/0x44 nonce      0x60-0x6C tag mirror      0x74-0x80 ghash mirror
    0x84-0x90 REG_DBG_PUSH_0..3   (what the stream FIFO push wrote at the tag beat)
    0x94-0xA0 REG_DBG_MAXIS_0..3  (what M_AXIS emitted as the last beat)
Prints every value as big-endian hex words for direct PC-side comparison.
"""
import mmap, struct, sys, os

SEQ_BASE = 0x40001000
try:
    SEQ_BASE = int(sys.argv[1], 0)
except IndexError:
    pass

fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
mp = mmap.mmap(fd, 0x1000, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=SEQ_BASE)

def rd(off):
    return struct.unpack("<I", mp[off:off + 4])[0]

n_hi, n_lo = rd(0x40), rd(0x44)
nonce = (n_hi << 32) | n_lo

def words(offsets):
    return [rd(o) for o in offsets]

tag = words([0x60, 0x64, 0x68, 0x6C])
gh  = words([0x74, 0x78, 0x7C, 0x80])
push = words([0x84, 0x88, 0x8C, 0x90])
mxs  = words([0x94, 0x98, 0x9C, 0xA0])
tagv = rd(0x70) & 1

print("NONCE %d" % nonce)
print("TAGV %d" % tagv)
print("TAG " + " ".join("%08x" % w for w in tag))
print("GH  " + " ".join("%08x" % w for w in gh))
print("PUSH " + " ".join("%08x" % w for w in push))
print("MXIS " + " ".join("%08x" % w for w in mxs))
mp.close()
os.close(fd)
