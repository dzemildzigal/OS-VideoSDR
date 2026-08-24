# Phase B — Concrete Implementation Plan (2026-08-24)

Author intent: PROPER fix, no hotfixes. Every architectural decision below is
backed by a measurement made on this board + this PC + this switch (evidence
table in section 0). Where a decision could not be made from evidence, it is
marked OPEN DECISION.

---

## 0. Evidence table (all measured 2026-08-23/24, unless noted)

| # | Fact | Number | Method |
|---|---|---|---|
| E1 | Video input rate (720p60 active) | 55.29 Mpx/s | DE_COUNT / pixel-clock-locked window |
| E2 | Video surviving v_vid_in at 100 MHz | 1.017 Mpx/s, 37.8 overflow/s | board probes, verified clock |
| E3 | PL writer AXI cost | 5.2 us/pkt, 0 faults | flood counters 0x8C-0xA0 |
| E4 | Writer idle waiting for PS | 94% of wall | flood counter 0xA4 |
| E5 | Current shim cost | ~68 us/pkt (send=38-41, cache=6-13, copy=2-11, wait=14, ack=2) | shim ns timers |
| E6 | Requirement at 1176 B payload | 70,560 pkt/s (14.2 us/pkt) | 2352 seg x 30 fps |
| E7 | UDP sendto ceiling | 41.6k pps (1 core), 53k (2 cores) | pps probe, delivered at sink |
| E8 | UDP sendmmsg ceiling | 44.6k pps (1 core) | pps probe, delivered |
| E9 | **UDP GSO (UDP_SEGMENT) ceiling** | **73.1k pps sustained 30 s (1 core), 82k (2 cores), 100% delivered** | gso probe + sink |
| E10 | AF_PACKET TX_RING | BROKEN on this kernel: 20 B of zeros (sizeof(sockaddr_ll)) prepended to every frame; kernel reports success; frames never delivered. Tap + GEM counters prove it | txring probe, board tap dump |
| E11 | AF_PACKET QDISC_BYPASS | frames silently dropped (not even on local tap); GEM HW counters still increment | txring probe A/B |
| E12 | AF_PACKET SOCK_RAW sendmmsg | 33k pps 1 core, 42k 2 cores, 100% delivered | pktpps probe |
| E13 | Jumbo frames | DEAD: board macb caps MTU 1500 (built-in kernel), switch drops DF pings >1500 (100% loss at 2000-8972 B) | Gate 0 report |
| E14 | CPU clock | 650 MHz dual A9 (BogoMIPS 325) | /proc/cpuinfo |
| E15 | Wire budget at target | 70.56k x 1302 B = 735 Mbit/s (73% of GbE) | arithmetic |
| E16 | PC receiver | Realtek PCIe GBE, MTU 1500, receives 82k pps sustained, zero drop | sink + pktmon |

Conclusions forced by evidence:
- The PS CAN carry the load (E9 > E6) but ONLY via UDP GSO. Every
  per-packet-syscall path measured fails by 1.4-2x (E7, E8, E12).
- TX_RING (the original B.3 idea) is disqualified by a kernel bug (E10).
- PL frame building with ETH/IP/UDP headers is UNNECESSARY: the kernel
  builds correct headers at GSO speed (E9). The PL only needs to place the
  1240 B UDP payloads contiguously in DDR.

---

## B.1 — Nonce-Prefix Injector (RTL)

### Description
A small AXI-Stream module between `aes_gcm_0/M_AXIS_CT` and the ring writer
that prepends the 8-byte cleartext nonce to each encrypted packet, so one
contiguous 1240 B UDP-payload unit (8 nonce + 1216 CT + 16 tag) lands in DDR.
This REPLACES the planned full ETH/IP/UDP frame builder — the kernel does all
headers via UDP GSO (evidence E9/E10).

### What to do
- New file `AES_VERILOG.srcs/sources_1/new/NoncePrefixInject.sv` (+ wrapper).
- Input: `M_AXIS_CT` (128-bit, TKEEP, TLAST per packet = 1232 B = 77 beats).
- Output: same stream with an 8 B prefix beat inserted per packet.
- Nonce source: the sequencer's `cfg_nonce_counter`/nonce state. The injector
  latches the nonce at each packet's first beat (the sequencer increments per
  packet; the first packet of a session uses nonce_seed). Design decision:
  take the nonce from a new sequencer output `pkt_nonce[63:0]` that the
  sequencer already tracks (it programs the AES core per packet), NOT
  recomputed locally — single source of truth.

### Where
- RTL: new module; BD: insert between `aes_gcm_0` and `ddr_ring_writer`
  (replaces the direct `M_AXIS_CT -> S_AXIS_SRC` connection,
  build_bd_hdmi_aes_tx.tcl line ~604).
- Sim: extend `tb_fullchain.sv`.

### How
- Beat 0 of each packet: emit a 128-bit beat with TKEEP=0x00FF containing
  the 8 nonce bytes (big-endian, matching today's shim prefix byte order),
  then stream the packet's 77 beats unchanged.
- Backpressure: pure valid/ready pass-through; 1 extra beat per packet.

### Why
- The PC receiver protocol requires the cleartext 8 B nonce prefix as the
  first 8 bytes of each UDP payload (it selects the GCM nonce). Today the SHIM
  builds this prefix in software per packet (cost + non-contiguity). With GSO
  the PS must send 32 packets as ONE contiguous buffer — the prefix has to be
  in DDR, hence in PL.
- Cost: ~1 beat per packet (0.01% rate overhead). The alternative (PS memcpy
  to stitch prefixes) would re-add ~10 us/pkt software cost — exactly the
  disease we are curing.

### Success criteria
- tb_fullchain: byte-for-byte identity of the DDR image vs the old
  (prefix || CT || tag) format for a 100-packet run.
- Wire capture on PC: first 8 bytes of every UDP payload equal the expected
  nonce sequence (nonce_seed + k).

### Gotchas
- Nonce byte order: today's shim writes big-endian (`nonce >> (56-8b)`).
  The injector must match EXACTLY or every tag fails.
- The sequencer increments the nonce when the AES core consumes the packet,
  not when it emits CT. Latch at packet START from a stable register — if
  the sequencer's counter already advanced, use a per-packet shadow
  (`nonce_at_pkt_start`) exported for this purpose.
- TKEEP of the injected beat: 0x00FF (8 valid bytes), NOT 0xFFFF.
- Keep the module free of any header/MAC/IP logic — that path is dead (E9).

---

## B.2 — DDR Ring Writer (RTL, replaces AXI_PingPong_Ctrl in this path)

### Description
A packet-ring DMA writer: consumes the injector's 1240-byte authenticated
stream and writes each packet into an N-slot DDR ring with a 1280-byte slot.
Bytes 1240..1279 are explicit zero excess transport bytes. Frame-atomic: a
packet plus its padding is either fully written or dropped whole. PS consumes
via a control block.

### What to do
- New file `AES_VERILOG.srcs/sources_1/new/DDRRingWriter.sv` (+ wrapper).
  Reuse `AXI_PingPong_Ctrl.sv` burst machinery (16-beat 128 B bursts,
  burst_last_strobe, BRESP checks — all proven, E3) as the inner write engine.
- Ring: 2048 slots x 1280 B = 2.62 MiB. Each slot contains 1240 bytes of
  authenticated data and 40 explicit zero excess bytes. 2048 slots gives
  about 29 ms of jitter absorption at full rate (a full frame period).
- Control block (4 KiB, separate page):
  - `0x00 produce_idx (u32)` — PL writes after each completed packet.
  - `0x04 consume_idx (u32)` — PS writes after a GSO batch is sent.
  - `0x08 dropped_packets (u32)`, `0x0C fault_code (u32)`.
- PL reads consume_idx from the control block via one AXI-Lite-style master
  read each time it is about to wrap (only near the ring end — cheap).
- Drop policy: if the ring is full (produce+1 == consume mod N), drop the
  WHOLE packet, increment dropped_packets, continue. Never partial writes.

### Where
- BD: replaces `frame_writer_0` (`AXI_PingPong_Ctrl_wrapper`) on the AES CT
  path. Old ping-pong stays in the design unused OR is removed — plan:
  REMOVE (one clean path; the pattern-mode flood test moves to the ring's
  own built-in pattern generator for G1 testing).
- Registers: sequencer space 0x1A0+ (RING_BASE_LO/HI, CTRL_BASE_LO/HI,
  RING_ENABLE, RING_SLOTS, plus the readback counters).

### How
- Same AXI write state machine as today (WR_PREP/AW/W/B), with 160 64-bit
  words and 10 full 128-byte bursts per 1280-byte slot. The input capture
  remains 155 body words plus one 8-byte prefix beat; five final words are
  generated as zero padding.
  - slot address = RING_BASE + slot*1280
  - after the last padding burst: produce_idx++ (write to control block)
  - underflow-safe wrap check each packet start
- PS-facing invariants: buffers written via HP0 (as today), cache coherency
  handled by PS invalidate (unchanged invariant).

### Why
- E4/E5: the 2-buffer ping-pong forces one MMIO ack + one refill wait per
  packet; the shim measured 14 us wait + 2 us ack per packet — 100% of the
  PS budget at target rate. A ring amortizes: 1 ack per 32-packet batch
  (0.09 us/pkt) and decouples PL burst from PS jitter.
- GSO requires contiguous memory: 32 packets x 1280 B must be one linear
  region — the padded ring slots provide that region.

### Success criteria
- Sim (tb_fullchain + ring model): 10k packets, zero reordering, zero
  partial writes, produce/consume indices exact, drop counter only
  increments under forced-full conditions.
- Board G1: pattern flood >= 75k slots/s sustained 30 s, dropped_packets
  == 0 with a GSO shim draining at 70.56k (short-term ring absorption).
- Board G2: after flood, DDR contents byte-compare vs golden: bytes
  0..1239 equal prefix+CT+tag; bytes 1240..1279 equal zero.

### Gotchas
- Control block MUST be on a separate page from the ring; PS maps it
  uncached (O_SYNC) like today's registers.
- produce_idx write ordering: write packet data FIRST, index LAST (release
  semantics), or the PS can read a slot before the DMA finished. Same for
  PS: send() returns AFTER the kernel copied the skb — consume_idx update
  may happen immediately after send() returns (safe point).
- dcache_invalidate range in B.3 is the 32 x 1280 B slot range (40,960 B),
  ONE call per batch — not per packet. The 40-byte slot tail is excess
  transport data and is not authenticated.
- Do NOT touch v_vid_in / video front end (frozen per 2026-08-23 design doc).
- 2048 slots at 1280 B: verify no overlap with the HP0 address region the
  daemon allocates today (0x1584A000 area); the daemon must allocate the
  ring via pynq memory API instead of hardcoded addresses.

---

## B.3 — UDP GSO Shim (PS software, replaces tx_shim send loop)

### Description
A C shim that drains the padded ring in 32-slot batches and sends each batch
with ONE UDP `send()` on a `UDP_SEGMENT` (GSO) socket. The kernel segments the
batch into 32 wire packets with correct headers and checksums at measured
73k pps (E9). Each UDP payload is 1280 bytes: the first 1240 bytes are the
existing OSV body and the final 40 bytes are excess zero transport bytes.

### What to do
- Rewrite `pynq/ps_shim/src/tx_shim.c` (keep process structure, MMIO helpers,
  argv interface; replace the drain loop):
  1. UDP socket, `setsockopt(IPPROTO_UDP, UDP_SEGMENT, 1280)`, SNDBUF 4 MiB,
     connect() to PC.
  2. Pin to CPU1, RT priority (E14: 650 MHz — leave CPU0 for IRQ + kernel).
  3. Loop: read produce_idx (1 MMIO read). For each batch of up to 32
     packets, locate source slot `ring_base + index*1280`. Invalidate the
     contiguous 1280-byte slot range, then make ONE `send(sock,
     ring_slot_start, count*1280, 0)` call with UDP_SEGMENT=1280. The 40
     zero bytes at slot bytes 1240..1279 are sent as excess transport bytes;
     they are not authenticated data. Write consume_idx (1 MMIO write) only
     after send() returns. If fewer than 32 packets wait until the oldest
     packet is 2 ms old, then send a short whole-slot batch. At ring wrap,
     split the request into two contiguous GSO sends.
  4. Keep the 1 s stats print: batches/s, pkts/s, syscalls/s,
     cache-invalidate us, slot bytes sent, drops, and short-batch count.
- Keep `--dst-host/--dst-port` argv. Mode flags `nosend`/`nocopy` stay.

### Where
- `OS-VideoSDR/pynq/ps_shim/src/tx_shim.c` (board path:
  `/home/xilinx/jupyter_notebooks/OS-VideoSDR/pynq/ps_shim/src/`).
- Daemon (`tx_daemon.py`) unchanged except: allocate + program ring/ctrl
  addresses, keep sequencer-disabled-until-shim handshake.

### How
- UDP GSO: the contiguous 1280-byte-stride ring region is sent as one UDP
  GSO request; the kernel emits N packets. Each packet contains 1240 bytes
  of authenticated OSV data followed by 40 excess zero bytes. The PC drops
  those final 40 bytes before parsing/authentication.
- Batch size 32: 32*1280 = 40,960 B < 65,535 UDP limit. Batch 64 =
  81,920 B > the UDP limit and produced EMSGSIZE in the 1280-byte probe.

### Why
- E9: GSO is the only measured PS path above the 70.56k requirement
  (73k sustained single-core, 100% delivered, 30 s) for a contiguous
  userspace buffer.
- E7/E8/E12: all per-packet-syscall paths cap at 33-53k — structurally
  insufficient; no amount of tuning closes 1.4-2x.
- E10/E11: TX_RING and QDISC_BYPASS are disqualified by kernel bugs.
- E9 directly measured 1280-byte UDP GSO at 73,209 pkt/s for 2 s, with
  146,432 packets submitted, 146,432 packets received at the PC application,
  and 146,432 NIC packets with zero PktMon drops. The B.2 ring now has the
  same 1280-byte slot shape, so B.3 does not add a gather copy. The GCM tag
  covers only bytes 0..1239; bytes 1240..1279 are excess transport bytes.

### Success criteria
- G3a: pattern flood end-to-end (ring + GSO shim + PC sink):
  >= 70,560 pkt/s sustained 60 s, zero drops, sink count == ring
  produce count.
- G3b: video attach: bad=0 on the PC auth oracle, 30 fps displayed,
  v_vid overflow/s ~ 0 (re-measure; E2 baseline 37.8/s must drop).
- G3c: 30-min soak: no drift in produce/consume delta, no tag failures.

### Gotchas
- UDP_SEGMENT requires kernel >= 4.18 (board: 6.6.10 ✓, verified by probe).
- The DDR ring is passed directly to send(); its slot stride is exactly the
  1280-byte GSO segment size. Invalidate every PL-written slot before
  reading it — the dcache_invalidate invariant that broke auth last time
  stays sacred.
- Batch boundary = slot boundary. NEVER send a partial slot. The 40-byte
  tail is deliberately sent and deliberately discarded by the PC receiver.
- Do not authenticate, parse, or use the 40-byte tail.
- PC sink fragility: ANY Windows NIC property change (jumbo toggle, adapter
  reset) silently kills the receiver — killed our measurements twice. The
  receiver must be verified ALIVE (line count grows) before any rate claim.
- EMSGSIZE at batch>=52 (=52*1280 > 65,535): clamp batch to 32.
- E9's 73k result used a contiguous buffer and has only 3.5% margin over
  70.56k. The separated-slot copy can reduce that margin. G3a must measure
  the complete ring-copy + GSO path. If it is too tight, use a second GSO
  sender thread on CPU0 (82k aggregate was measured), increase the batch
  size through multiple GSO requests, or select the larger payload option;
  do NOT fall back to per-packet syscalls.

---

## Build & integration order (ONE Vivado rebuild window)

1. RTL: `NoncePrefixInject.sv`, `DDRRingWriter.sv` (+ wrappers), sim in
   tb_fullchain. STOP POINT for review — nothing below starts until B.1/B.2
   sim criteria pass.
2. Same rebuild carries (already planned, independent):
   - 720p30 EDID bin (replaces 720p60-only) — source-side fix.
   - Remove ST_SKIP 2:1 frame decimation from HDMI_Axis_Packetizer
     (EDID 30 Hz makes it wrong; would produce 15 fps).
   - Keep ALL probes (video_fe, beat counter, status) untouched.
3. BD: swap writer, insert injector, wire ring/ctrl address registers.
4. PS: GSO shim + daemon ring allocation.
5. Gates G1 -> G2 -> G3a -> G3b -> G3c (defined above).

## Open decisions (none blocking the build start)

1. Payload size: keep 1176 B (70.56k pps, 3.5% single-core margin, zero
   protocol change) vs 1392 B (59.6k pps, ~23% margin, packetizer geometry +
   PC reassembly changes). Recommendation: build with 1176 B first
   (cfg_payload_bytes is runtime-configurable in the sequencer), measure G3a
   margin, escalate only if the 30-min soak shows drops.
2. Ring slot count 2048 (29 ms absorption): confirm against real GEM
   jitter in G1; doubling is a register write away if slots are a parameter.

## What was NOT decided by opinion

Every fork in this plan (TX_RING vs GSO, frame builder vs kernel headers,
payload size, batch size, core count) was settled by a probe on this exact
hardware. The grill-me session was not needed: the board did the grilling.
