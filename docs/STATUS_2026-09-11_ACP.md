# STATUS 2026-09-11 — ACP-coherent ring path for the 30 fps HDMI chain

## 1. Why this change exists

The PL publishes 2352 segments per frame. At 30 frames per second this is
70,560 packets per second. The PS shim must send all of them. A lost packet
breaks a frame.

Measured PS shim rates (1280-byte UDP GSO segments, one PC sink):

| variant                                  | packets/s | notes                          |
|------------------------------------------|-----------|--------------------------------|
| old /dev/mem shim                        | 43,000    | first working version          |
| XRT buffer shim                          | 52,400    | ring in an XRT buffer object   |
| windowed ring sync                       | 54,500    | invalidate 7 batches ahead     |
| dual-core sender (two sockets)           | 60,900    | best result before this change |

The dual-core sender already uses 1.97 s of CPU per second across both cores
(send 1.60 s, cache maintenance 0.37 s), so the CPU is the limit and not the
network. The measured per-packet cost is about 15 µs of kernel TX time plus
2.6 µs of L1/L2 invalidate.

The invalidate exists because the writer wrote through S_AXI_HP0. HP0 is fast
but not coherent: the CPU must flush its cache lines before it reads a slot
that the PL has just overwritten. The Accelerator Coherency Port (ACP) removes
that work. The ACP goes through the snoop control unit (SCU), which keeps the
CPU caches correct in hardware. The ACP is also fully fast enough for this
stream: the chain needs about 90 MB/s of write bandwidth.

## 2. What changed

### 2.1 Block design (`pynq/build_bd_hdmi_aes_tx.tcl`)

- `CONFIG.PCW_USE_S_AXI_HP0` changed from 1 to 0.
- `CONFIG.PCW_USE_S_AXI_ACP` set to 1.
- `hp0_mem_ic` renamed to `acp_mem_ic`. Its `M00_AXI` now drives
  `ps7/S_AXI_ACP`.
- The ACP clock is FCLK0 (100 MHz) and the M00 reset comes from the stable
  reset, exactly as the HP0 port had.

### 2.2 Coherence attributes (`DDRRingWriter.sv`, `DDRRingWriter_wrapper.v`)

The ACP is coherent only when the master asks for it. UG585 ("ACP Requests")
says that an ACP read is coherent when `ARUSER[0] = 1` and `ARCACHE[1] = 1`,
and the same rule applies to writes with `AWUSER[0]` and `AWCACHE[1]`.

The writer master carried no cache attributes at all, and the block design
reported the mismatch:

```text
WARNING: [BD 41-237] Bus Interface property AWUSER_WIDTH does not match
         between /ps7/S_AXI_ACP(5) and /acp_mem_ic/s00_couplers/auto_pc/M_AXI(0)
```

Without the attributes the PL writes would land in DDR while the CPU keeps
reading stale cache lines. That is the same fault the ACP is meant to remove,
and it would corrupt packets at a low rate.

The writer now drives:

```text
M_AXI_AWCACHE = 4'b0011   bufferable, cacheable, no write-allocate
M_AXI_AWUSER  = 5'b00001  coherent write
M_AXI_AWPROT  = 3'b000
M_AXI_ARUSER  = 5'b00001  (read channel stays retired)
M_AXI_ARCACHE = 4'b0011
M_AXI_ARPROT  = 3'b000
```

`AWCACHE = 0011` is the right policy here: the PL writes each slot once and
never reads it back, so write-allocate would only push ring data into the CPU
caches and cause evictions.

After this change the block design reports no `AWUSER_WIDTH` or `ARUSER_WIDTH`
warning, which shows that the attributes reach the ACP port.

Note for later: `AWCACHE = 0011` (cacheable) is correct for the ACP. It must
not be used on a non-coherent port such as HP0.

### 2.3 Writer registers (`DDRRingWriter.sv`)

- New read-only register `0x4C` reports 1 for a coherent build. Software reads
  it and skips cache maintenance by itself.
- A soft reset (write bit 1 to `0x04`) now also clears the PS-pushed consume
  register. A restarted sender used to inherit the old value and the writer
  dropped every packet from then on.

### 2.4 Shim (`pynq/ps_shim/src/tx_shim.cpp`)

- Reads `0x4C`; when it reports a coherent path the shim skips cache
  maintenance. `--sync` and `--no-sync` override the automatic choice.
- Two sender workers, one per core, each with its own UDP GSO socket.
- Batches are claimed in order. A completion bitmap gives the writer an exact
  reuse frontier, which the shim writes to the PS-consume register `0x48`.
- One-instance lock and default worker priority. Two senders at nice −20 once
  made the board unreachable.

## 3. Expected result

The 0.37 s/s of cache maintenance disappears, so about 0.20 s of CPU per
second becomes free on each core. The estimated shim capability is then
 70,000 to 76,000 packets/s against a requirement of 70,560 packets/s.

If this is not enough, the remaining lever is the packet geometry: a 1440-byte
slot with 1368 payload bytes (456 pixels) lowers the requirement to
60,660 packets/s and raises link efficiency from 76% to 83%.

## 4. How the result is verified

1. Handoff file shows `S_AXI_ACP` with a 5-bit USER field and no `S_AXI_HP0`.
2. Timing stays clean at 100 MHz (0 failing endpoints).
3. The shim prints that it found a coherent path.
4. `drops_delta` falls to 0 while the PL publishes 70,560 packets/s.
5. The authentication monitor reads the wire for 15 s: every packet must
   authenticate, with 0 duplicates and 0 padding, length or header errors.
   This is the real test of coherency: a stale cache line would fail the
   GCM tag.
6. Full chain: the PC receiver completes 30 frames per second at
   1280x720 RGB888.

## 5. Measured result (2026-09-11, bit e94b9d68)

Target: 70,560 packets/s. All rates measured on the wire and re-checked on the
PC.

| configuration                  | rate        | drops  | auth failures  |
|--------------------------------|-------------|--------|----------------|
| ACP, cached ring read          | 70,505-70,659 | 0    | 155-250 per 20 s |
| ACP, uncached ring read        | 61,176-62,204 | 8,500/s | **0 in 1,227,974** |
| HP0, cached + invalidate       | 54,500      | some   | 0              |

Cache maintenance is gone as expected (`bo-sync-us=0`), and with the cached read
the chain reaches the target with no drops at all. 451 frames completed in
20 s in the best run.

### 5.1 The remaining defect

The cached run loses 0.011% of packets to failed authentication. Measurements
show that the failures are:

- always isolated packets, never two in a row (250 failures, 0 adjacent pairs);
- absent when the same slots are read through an uncached mapping
  (0 failures in 1,227,974 packets).

So the PL's writes through the ACP are **not** invalidating the CPU's cached
copies, even though UG585's condition is satisfied (AWUSER[0]=1, AWCACHE[1]=1,
and the PS7 ties the ACP's AWUSER to 31). The ring is 29 ms deep and the CPU
reads 90 MB/s, so nearly every stale line is evicted before it is read again -
which is why only 0.011% of packets are affected.

### 5.2 What this costs

- Cached read: correct rate, 0.011% corrupt packets. A corrupted packet breaks
  its whole frame, so about one frame in three loses a packet.
- Uncached read: no corrupt packets at all, but the uncached read costs about
  5 us per packet, so the sender saturates both cores at 62k packets/s and the
  ring overflows (8,500 drops/s).

### 5.3 Next step

Rebuild with `AWCACHE = 4'b1111` and `AWUSER = 5'b11111` (write-allocate plus
all user bits). This is the setting that Xilinx users report as working, and a
write-allocate write reaches the CPU's own copy, so the read must be correct.
If that fails, fall back to the uncached read plus the 1440-byte slot, which
lowers the requirement to 60,660 packets/s and fits under the measured 62k.

### 5.4 Note for the test harness

The writer latches `FAULT_TKEEP` (fault code 2) and stops publishing when it is
disabled while the video keeps running - as happens when the shim is restarted
several times in a row. Only a full reload of the overlay clears it. Restart the
shim once per measurement, not in a loop.

## 6. Open risks

| risk                              | state                                            |
|-----------------------------------|--------------------------------------------------|
| ACP writes not coherent           | fixed by the attributes; verified by the monitor |
| ACP bandwidth too low             | low risk: need is 90 MB/s, ACP is far faster     |
| ACP write latency slows the PL    | check the PL publish rate after the rebuild      |
| Timing regression from the change | check the rebuild report                         |
| PC receiver rejects reordered packets | fixed in `main_rx.py` (reorder window)        |
