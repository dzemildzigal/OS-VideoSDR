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
print("WR_FETCH_CYCLES %d" % (wrd(0x84) | (wrd(0x88) << 32)))
print("WR_FETCH_WAIT   %d" % (wrd(0xAC) | (wrd(0xB0) << 32)))
print("WR_FETCH_PACK   %d" % (wrd(0xB4) | (wrd(0xB8) << 32)))
print("WR_AW_CYCLES    %d" % (wrd(0x8C) | (wrd(0x90) << 32)))
print("WR_W_CYCLES     %d" % (wrd(0x94) | (wrd(0x98) << 32)))
print("WR_B_CYCLES     %d" % (wrd(0x9C) | (wrd(0xA0) << 32)))
print("WR_IDLE_BLOCKED %d" % (wrd(0xA4) | (wrd(0xA8) << 32)))
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
print("SEQ_TOTAL      %d" % rd(0xD4))
print("SEQ_SETUP      %d" % rd(0xD8))
print("SEQ_WAIT_EMPTY %d" % rd(0xDC))
print("SEQ_POLL_SESS  %d" % rd(0xE0))
print("SEQ_PASS       %d" % rd(0xE4))
print("SEQ_WAIT_TAG   %d" % rd(0xE8))
print("SEQ_TAG_READ   %d" % rd(0xEC))
print("SEQ_GHASH_READ %d" % rd(0xF0))
_st = rd(0x100)
print("AES_STALL 0x%08x" % _st)
print("  tvalid=%d pass=%d tready=%d pt_ready=%d slot=%d sess=%d ghct=%d key=%d" % (
    (_st >> 31) & 1, (_st >> 30) & 1, (_st >> 29) & 1, (_st >> 28) & 1,
    (_st >> 27) & 1, (_st >> 26) & 1, (_st >> 25) & 1, (_st >> 24) & 1))
print("  gh_fifo=%d ct_fifo=%d pt_infl=%d ct_valid=%d empty=%d" % (
    (_st >> 16) & 0xFF, (_st >> 9) & 0x7F, (_st >> 2) & 0x7F,
    (_st >> 1) & 1, _st & 1))
print("FIFO_FULL_CYCLES  %d" % (rd(0x104) | (rd(0x108) << 32)))
print("EMPTY_NO_CT_CYCLES %d" % (rd(0x10C) | (rd(0x110) << 32)))
print("PT_BLOCKED_CYCLES %d" % (rd(0x114) | (rd(0x118) << 32)))
print("NO_OFFER_CYCLES   %d" % (rd(0x11C) | (rd(0x120) << 32)))
print("GH_NOT_READY_CYCLES %d" % (rd(0x124) | (rd(0x128) << 32)))
print("SLOT_BLOCKED_CYCLES %d" % (rd(0x12C) | (rd(0x130) << 32)))
print("GCM_BUSY_CYCLES   %d" % (rd(0x134) | (rd(0x138) << 32)))
print("LAST_FIFO_FULL   %d" % rd(0x13C))
print("LAST_EMPTY_NO_CT %d" % rd(0x140))
print("LAST_PT_BLOCKED  %d" % rd(0x144))
print("LAST_NO_OFFER    %d" % rd(0x148))
print("LAST_GH_NOT_READY %d" % rd(0x14C))
print("LAST_SLOT_BLOCKED %d" % rd(0x150))
_pst = rd(0x154)
print("PKT_STATUS 0x%02x" % _pst)
print("  vtv=%d vtr=%d tuser=%d state=%d ptv=%d ptr=%d" % (
    (_pst >> 6) & 1, (_pst >> 5) & 1, (_pst >> 4) & 1,
    (_pst >> 2) & 3, (_pst >> 1) & 1, _pst & 1))
print("PREFIFO_BEATS %d" % ((rd(0x58) << 32) | rd(0x5C)))
print("PIXELCLK_COUNT %d" % ((rd(0x158) << 32) | rd(0x15C)))
print("DE_COUNT %d" % ((rd(0x160) << 32) | rd(0x164)))
print("VID_OF_COUNT %d" % ((rd(0x168) << 32) | rd(0x16C)))
print("VID_UF_COUNT %d" % ((rd(0x170) << 32) | rd(0x174)))
print("VID_RESET_PULSE %d" % ((rd(0x178) << 32) | rd(0x17C)))
print("VID_RESET_LEVEL %d" % rd(0x180))
print("PKT_FIFO_WR_COUNT %d" % rd(0x184))
print("PKT_FIFO_RD_COUNT %d" % rd(0x188))
print("PREFIFO_VALID_CYCLES %d" % rd(0x190))
print("PREFIFO_READY_CYCLES %d" % rd(0x194))
print("CDCOUT_BEATS %d" % ((rd(0x198) << 32) | rd(0x19C)))
print("DE_AT_OVERFLOW %d (line %d)" % (rd(0x1A0), rd(0x1A0) // 1280))
_pfs = rd(0x18C)
print("PKT_FIFO_STATUS 0x%08x" % _pfs)
print("  m_ready=%d m_valid=%d" % (_pfs & 1, (_pfs >> 1) & 1))
print("NONCE %d" % nonce)
print("TAGV %d" % tagv)
print("TAG " + " ".join("%08x" % w for w in tag))
print("GH  " + " ".join("%08x" % w for w in gh))
print("PUSH " + " ".join("%08x" % w for w in push))
print("MXIS " + " ".join("%08x" % w for w in mxs))
fw.close()
mp.close()
os.close(fd)
