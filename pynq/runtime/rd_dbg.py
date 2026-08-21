#!/usr/bin/env python3
"""rd_dbg.py - dump the sequencer mirrors + the new tag-path debug probes
for the most recently completed packet (nonce-1).

Reads (via /dev/mem, no overlay reload):
  writer @ 0x40000000:
    0x54 status, 0x58 error count, 0x64-0x78 first-fault diagnostics
  sequencer @ 0x40001000:
    0x40/0x44 nonce      0x60-0x6C tag mirror      0x74-0x80 ghash mirror
    0x84-0x90 REG_DBG_PUSH_0..3   (what the stream FIFO push wrote at the tag beat)
    0x94-0xA0 REG_DBG_MAXIS_0..3  (what M_AXIS emitted as the last beat)
    0xA4 raw AES status, including CT FIFO overflow bit 18
    0xCC/0xD0 AES tag-complete counter high/low
Prints every value as big-endian hex words for direct PC-side comparison.
"""
import mmap, struct, sys, os

WRITER_BASE = 0x40000000
SEQ_BASE = 0x40001000
try:
    SEQ_BASE = int(sys.argv[1], 0)
except IndexError:
    pass

fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
fw = mmap.mmap(fd, 0x1000, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=WRITER_BASE)
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

def wrd(off):
    return struct.unpack("<I", fw[off:off + 4])[0]

print("WRITER_STATUS 0x%08x" % wrd(0x54))
writer_complete = wrd(0x7C) | (wrd(0x80) << 32)
seq_tag_complete = rd(0xD0) | (rd(0xCC) << 32)

print("WRITER_ERROR  0x%08x" % wrd(0x58))
print("WRITER_COMPLETE %d" % writer_complete)
print("AES_TAG_COMPLETE %d" % seq_tag_complete)
print("FAULT_CAUSE   %d" % wrd(0x64))
print("FAULT_STATE   0x%08x" % wrd(0x68))
print("FAULT_KEEP    0x%04x" % (wrd(0x6C) & 0xFFFF))
print("FAULT_LEFT    %d" % wrd(0x70))
print("FAULT_BURST   %d" % wrd(0x74))
print("FAULT_BRESP   0x%x" % (wrd(0x78) & 0x3))
print("AES_STATUS    0x%08x" % rd(0xA4))
print("CT_BEATS      %d" % rd(0xA8))
print("TAG_PUSHES    %d" % rd(0xAC))
print("TAG_FIFO_CNT  %d" % rd(0xB0))
print("TAG_PT_INFL   %d" % rd(0xB4))
print("LAST_CT_BEATS %d" % rd(0xB8))
print("LAST_FIFO_PUSH %d" % rd(0xBC))
print("LAST_AXIS_POP  %d" % rd(0xC0))
print("LAST_TAG_ATTEM %d" % rd(0xC4))
print("LAST_FIFO_CNT  %d" % rd(0xC8))
print("NONCE %d" % nonce)
print("TAGV %d" % tagv)
print("TAG " + " ".join("%08x" % w for w in tag))
print("GH  " + " ".join("%08x" % w for w in gh))
print("PUSH " + " ".join("%08x" % w for w in push))
print("MXIS " + " ".join("%08x" % w for w in mxs))
fw.close()
mp.close()
os.close(fd)
