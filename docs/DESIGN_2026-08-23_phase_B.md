# Phase B Design Spec — B.1 Frame Builder, B.2 Ring Writer (2026-08-23)

Status: DESIGN ONLY. No RTL/BD modified. Build not started.
Fresh video-path evidence (this doc, sec. 1) confirms the transport is the
sole bottleneck; v_vid RTL is NOT to be touched.

## 1. Fresh measurement at verified 100 MHz (gate for B)

Window 5.424 s (locked to 74.25 MHz pixel clock).

```
DE (active px)      55.29 Mpx/s    full 720p60 active rate
v_vid out beats      1.017 Mpx/s   (was 0.533 M at 50 MHz; 2.0x with clock)
v_vid overflows     37.8 /s       (was 35.9/s; unchanged)
v_vid underflow      0
packets sent        ~1,292 pkt/s
writer faults       0
```

Verdict: video front end is healthy and unchanged in behavior. The choke
is downstream. Proceed with B.1+B.2.

## 2. B.1 — Frame Builder (new RTL module, sits behind the AES M_AXIS_CT)

Purpose: emit COMPLETE Ethernet frames so the PS never builds a packet.

Input:  AES GCM ciphertext + tag stream (existing M_AXIS_CT, 128-bit).
Output: PL AXI-stream of full frames:
  [dst MAC 6][src MAC 6][ethertype 0x0800 2]
  [IP hdr 20 (UDP, total len, id, ttl64, proto 17, CHECKSUM)]
  [UDP hdr 8 (src 5601, dst 5600, len, checksum 0)]
  [8B nonce prefix][1216B CT][16B tag]        (existing wire payload)
  Total 1282 B + padding to 60 B minimum not needed (>60 already).

Details:
- IP checksum: incremental/ones-complement over the fixed header; src/dst
  IP from sequencer config registers; MACs from config registers
  (learned once via ARP from the shim, written by MMIO).
- The 40-byte header is generated, not stored per packet; the nonce prefix
  comes from the frame id (already in the stream path).
- No scatter: one TLAST per frame; beats are 128-bit, frame = 1282 B ->
  64.1 beats -> pad to 65 beats with strobe on last (writer already
  handles partial strobes - reuse burst_last_strobe logic).

Register additions (sequencer space):
  0x1A0 MAC_DST_LO/HI, 0x1A4 MAC_SRC_LO/HI, 0x1A8 IP_SRC, 0x1AC IP_DST,
  0x1B0 FRAME_BUILDER_ENABLE.

## 3. B.2 — DDR Ring Writer (replaces AXI_PingPong_Ctrl in this path)

Purpose: decouple PL production from PS consumption; remove the 2-buffer
hand-off that caps batching at 2 and forces per-buffer MMIO acks.

Structure:
- N = 1024 slots x 1280 B (frame + headroom) = 1.25 MiB region.
- Slot k at BASE + k*1280. 1280 = 10 x 128 B = writer-burst aligned.
- Control block (separate 4 KiB page, non-cached on PS side):
    [0x00] PL_produce_idx (u32, written by PL each completed frame)
    [0x04] PS_consume_idx (u32, written by shim after TX_RING owns slot)
    [0x08] overflow_count, [0x0C] underflow_count
- PL: if (PL_produce_idx + 1) mod N != PS_consume_idx -> write frame,
  increment, else drop whole frame and count overflow (frame-atomic drop,
  never partial).
- PS: reads produce_idx, mmap'd slot data, submits to TX_RING, then
  stores consume_idx (single 4-byte release store; PL sees it next cycle
  via its own AXI-Lite/master read port - PL reads the control block
  through an AXI master port each WR_IDLE, 1 transaction per frame).

Why 1024: at 70.56k pkt/s, one slot = 14.2 us of slack; 1024 slots =
14.6 ms of PS-jitter absorption (a full frame period). DDR cost 1.25 MiB.

## 4. B.3 — AF_PACKET TX_RING shim (outline only)

- ETH_P_ALL socket bound to eth0, TPACKET_V3, frame_size 1280 aligned,
  frame_nr 1024 (mirrors ring 1:1 - PL slot k maps to TX_RING block k).
- Per packet: copy 1282 B from PL slot into the ring block, set tp_len,
  status=TX, poll producer; flush batch by write() once per N or on
  timeout; consume_idx update once per batch.
- mmap of the DDR ring region must be O_SYNC non-cached (same as today);
  dcache_invalidate per slot retained (invariant).
- Target: syscall cost per BATCH, not per packet.

## 5. Build plan (STOP POINT - nothing below executed)

Single Vivado rebuild, changes:
  1. New: frame_builder.sv (+ wrapper) - B.1
  2. New: ddr_ring_writer.sv (+ wrapper) - B.2
  3. BD: insert frame_builder between aes_gcm_0/M_AXIS_CT and writer;
     replace AXI_PingPong_Ctrl instance with ddr_ring_writer; add
     control-block master port + address config.
  4. Same build: 720p30 EDID bin (kEdidFileName) + ST_SKIP removal in
     HDMI_Axis_Packetizer.
  5. Keep video_fe/video_beat/packetizer probes untouched.
Sim gates before board: tb_fullchain extended with ring writer + frame
builder; golden = existing wire format (PC oracle must pass unchanged,
byte-for-byte incl. nonce prefix and tag position).

Board bring-up gates:
  G1 pattern flood through ring (no video): >= 75k slots/s sustained
  G2 ARP + MAC/IP config path works (ping wire-capture on PC)
  G3 video attach: bad=0 (auth oracle), 30 fps, v_vid overflow ~0
