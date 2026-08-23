# Transport Redesign — Report and Plan (2026-08-23, night)

Status: PLAN ONLY. No RTL, BD, or code changed for this plan yet.
Baseline measurements in docs/STATUS_2026-08-23_writer_service_rate.md
and this session.

---

## 1. Measured baseline (all verified on board)

| Item | Value | Source |
|---|---|---|
| Design clock | 100 MHz, readback verified (was silently 50 MHz before) | clkctrl gpio fix |
| PL writer AXI cost | 5.2 us/pkt, zero faults, 10 bursts | flood counters |
| Writer idle waiting for PS | 94% of wall time | flood counters |
| PS shim total cost | ~68 us/pkt + hidden sleep, ~95 us/pkt effective | shim timers |
| Flood service rate | 10,431 pkt/s | flood counters |
| 720p30 requirement | 70,560 pkt/s (1176 B payload) | protocol spec |
| Deficit | ~6.8x | |
| Live video stream | ~1,285 pkt/s (video path starved separately) | sink |
| Auth status of last full run | ok=64798 bad=0 (before tonight's changes) | PC oracle |

## 2. Hard constraints (no software can remove these)

1. **The Ethernet PHY is wired to the PS, not the PL.** PYNQ-Z2 routes the
   RTL8211E PHY to PS GEM via MIO. A PL MAC is physically impossible on this
   board. Every packet crosses the PS, whatever we build.
2. **GbE line rate.** 70,560 pkt/s x 1286 B on-wire (frame+IFG) is ~727 Mbit/s,
   73% of the line. Feasible. No headroom for 2x.
3. **PS per-packet budget.** 70,560 pkt/s = 14.2 us/packet (derived:
   2352 seg x 30 fps, packetizer constants). Measured send cost alone on
   this board is 39-41 us/pkt; batching is hard-capped at 2 by the
   ping-pong design, so even a perfect batch leaves 19.5 us send +
   measured 12-25 us wait+cache+ack. **The per-packet-syscall architecture
   cannot reach 720p30 on measured numbers alone.** No generic syscall
   estimates needed.
4. **PL pipeline capacity is fine.** At 100 MHz the AES path moves 16 B/cycle;
   the writer moves 5.2 us/pkt. PL is not the limit.

## 3. Root-cause statement

The architecture routes every packet through: PL writer -> DDR ping-pong ->
PS mmap read + dcache invalidate + UDP stack + syscall + macb driver -> GEM.
That chain costs ~95 us/packet. The requirement is 14.2 us/packet. The gap
cannot be closed by optimizing the existing chain (sendmmsg, pinning, spinning
are all still per-packet syscalls). The architecture must change.

## 4. Proper fix options

### Option A — Jumbo frames (kill the packet count)

Raise MTU end-to-end (PYNQ GEM supports jumbo; PC NIC likely supports;
switch must support — MUST be verified first).

- Payload 1176 B -> 8552 B (header 40 + payload = 8592 = 537x16, keeps the
  AES 16-byte beat rule).
- Packet rate drops 70,560 -> **9,696 pkt/s**. The CURRENT shim already does
  10,431 pkt/s. The problem dissolves with margin.
- Changes: MTU on board + PC + switch; payload-bytes config; PL packetizer
  segment size params; PC rx reassembly window; EDID stays.
- Wire format and auth logic unchanged.
- Risks: switch/NIC jumbo support; a lost 9 KB packet costs 77 us of pixels
  (frame auth still catches it); NIC rx buffering.
- Verification gate: ethtool MTU both ends; ping -M do -s 8972; auth run.

### Option B — PL-built Ethernet frames + AF_PACKET TX_RING (zero-copy)

PL writes complete Ethernet frames (ETH/IP/UDP headers + payload + tag)
directly into a kernel TX_RING slot ring; the shim's per-packet work becomes
one 4-byte status store; one syscall per batch.

- Changes: new PL frame-builder + IP checksum in the sequencer/packetizer;
  DDR ring writer (N slots, e.g. 512); shim rewrite; PC rx accepts same wire
  format as now (it already sees UDP); kernel macb must sustain ~75k pps in
  softirq — plausible on a dedicated core but unproven on this board.
- No dependency on switch/NIC jumbo support.
- Risks: macb softirq ceiling; TX_RING slot alignment with PL DMA addresses;
  largest engineering effort (RTL + kernel-adjacent userspace).

### Option C — Userspace GEM driver (bypass the kernel fully)

Unbind macb after link-up; drive GEM descriptors from a userspace ring.

- Highest ceiling, highest risk (PHY state machine handoff, coexistence with
  network stack), longest schedule. Only worth it if B measures below target.

### Common to all options (PL-side, independent work)

1. **DDR ring writer**: replace the 2-buffer ping-pong with an N-slot ring
   (AXI_PingPong_Ctrl -> ring writer; PS writes back a consumed pointer).
   Decouples PL burst from PS service jitter. Needed for A (small N) and B.
2. **720p30 EDID + remove PL ST_SKIP**: stops the 60->30 capture/skip burst;
   makes the packet rate flat 70.5k (or 9.7k with A). Independent of transport.

## 5. Recommended path (SUPERSEDED by Gate 0: jumbo FAILED both board kernel and network path - Option B is primary; see GATE0_2026-08-23_jumbo_feasibility.md)

**A first, B as fallback.**

- A is a protocol/configuration change, not a hotfix: it selects the correct
  packet size for a 1 Gbit/s PS-attached PHY, which is the honest design for
  this board. It is reversible and does not touch RTL. Gate 1: verify jumbo
  path (NIC + switch) — if the link is direct PC-to-board, this is near-free.
- If jumbo is impossible on the path, commit to B (RTL ring + frame builder).
  C only if B measures below 70k with jumbo unavailable.

## 6. Plan (build stage boundary — stop here tonight)

Gate 0 — evidence, no hardware:
  0.1 Document PC NIC jumbo capability (ethtool), switch model, link topology
      (direct cable vs switch). Owner: me, with Dzemo confirming topology.

Phase A — jumbo bring-up (if Gate 0 passes):
  A.1 Set MTU 9000+ on board (ip link) and PC; test 8972 B pings both ways.
  A.2 Payload-bytes 8552 end-to-end dry run (pattern flood, no video): writer,
      shim, sink rate >= 10,500 pkt/s sustained, WR_IDLE_BLOCKED near zero.
  A.3 PL packetizer segment/pad params for 8552 B (RTL change, one rebuild).
  A.4 720p30 EDID file + ST_SKIP disable (RTL change, same rebuild).
  A.5 Full-frame authenticated run: bad=0, full 30 fps, PC display.
  A.6 Ring writer (N slots) only if A.5 shows buffer-run stalls.

Phase B — fallback (only if Gate 0 fails):
  B.1 RTL: frame-builder (headers + IP checksum) behind the sequencer.
  B.2 RTL: DDR ring writer, 512 slots, control block, PS consumed pointer.
  B.3 PS: AF_PACKET TX_RING shim, batch flush, CPU1 pinned.
  B.4 Measure: target >= 75k pps pattern flood before video attach.
  B.5 Same video/auth gates as A.5.

## 7. Protected invariants (all options)

- dcache invalidation before reading PL-written buffers (bad=0 depends on it).
- PC-side authentication oracle stays the acceptance test.
- No color-space conversion; raw RGB888 end to end.
- One daemon, one shim, one receiver per valid run.

## 8. Evidence validity

- All flood/shim/clock numbers: measured after the verified 100 MHz fix
  (readback 0b10), unless marked "before".
- Video-path evidence (532 kpx/s, 35.86 overflow/s) predates the clock fix
  and is STALE: v_vid drain ceiling moved 50 -> 100 Mpx/s. No RTL redesign
  decision on the video path may be based on it until re-measured at the
  verified clock.
- Jumbo feasibility (Option A): zero measurements. Gate 0 exists for this.
- AES stream path at full pixel rate: never measured (flood used pattern
  mode; fetch counters were trivially zero). Must be measured before
  declaring the PL pipeline clear for video.
- sendmmsg shim build: compiled, never run. No numbers exist for it.

## 9. Open questions for morning

1. Is the PC connected to the board directly or through the router/switch?
2. PC NIC model — jumbo capable?
3. Do you accept 8.5 KB payload as the protocol's new fixed size, or must
   1176 B stay (then B is mandatory)?
