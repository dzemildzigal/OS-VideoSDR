# Status: Writer/Shim Service Rate — 2026-08-23

## Question under test

Is the throughput clog in the PL (writer) or the PS (shim)? Do the flag probes
prove it?

## Setup

- Board: PYNQ-Z2, fresh push of bitstream (Aug 23, md5 726ce434…), tx_shim,
  runtime scripts to `/home/xilinx/jupyter_notebooks/OS-VideoSDR/`.
- Daemon: `tx_daemon.py --configure-only --aes-freq 100`, started with
  `sudo bash -lc` (login shell needed for `XILINX_XRT=/usr`).
- Shim: `./tx_shim 192.168.0.37 5600` (send mode), root.
- Measurement: writer switched to deterministic pattern flood for 3 s
  (`SRC_SEL=0`), counters at 0x7C..0xB8 read before/after, source restored.

## Result 1 — service rate

```
Flood rate:      8585 pkt/s   (25756 completions / 3.0 s)
720p30 target:   70,560 pkt/s
Deficit:         ~8.2x too slow
```

## Result 2 — per-packet cycle budget (flag probes)

| counter        | cyc/pkt | µs @50 MHz | share |
|----------------|---------|------------|-------|
| idle_blocked   | 5440    | 108.8      | 93%   |
| W-channel      | 154     | 3.1        |       |
| B-channel      | 209     | 4.2        |       |
| AW-channel     | 10      | 0.2        |       |
| total counted  | 5813    | 116.3      |       |
| wall measured  |         | 116.5      | ✓     |

The budget closes to 0.2 %. Nothing is hidden.

Meaning:

- `idle_blocked` = writer in WR_IDLE with a READY buffer the shim has not
  consumed. This is 93 % of every packet.
- PL AXI work (10 bursts × 128 B) is 7.5 µs. The writer hardware is not the
  limit. The single-beat AXI fault from earlier is confirmed fixed.

**Verdict: the clog is the PS/shim handoff. The flag probes confirm it.**

## Result 3 — shim self-timing (its own log)

```
t[pkt] wait=15.4us cache=6.2us copy=8.3us send=36.0us ack=2.3us  (~68 us total)
```

- `send` (sendto, 1240 B UDP) = 36 µs — dominant cost.
- `cache` (dcache_invalidate) = 6.2 µs — REQUIRED for correctness
  (removing it gave ok=0 bad=32644 earlier). Keep, do not delete.
- `copy` = 8.3 µs.

Note: shim self-reports 68 µs/pkt, but effective service period is 116 µs.
The gap is the ping-pong round trip: shim ack -> writer WR_IDLE -> fill next
buffer. Both sides must shrink.

## Result 4 — clock domain actually 50 MHz, not 100

Cycle budget closes only at 50 MHz. Direct register proof (read-only probe):

```
axi_gpio_clkctrl @ 0x41200000:
  GPIO_DATA = 0x00000001   sel[2:1]=00 -> 50 MHz input selected
  GPIO_TRI  = 0xffffffff   channel is ALL INPUT -> DATA writes drive nothing
```

The daemon never clears GPIO_TRI, so the mux select pins float. Every
`--aes-freq` switch (50/75/100) has been a silent no-op since the mux was
added. The design runs at 50 MHz always.

Impact:

- AES/stream domain halved. Not the current flood bottleneck (writer waits on
  PS), but it caps any future PL-side rate gains.
- Fix: daemon must write `GPIO_TRI = 0x00000000` (channel 1 all outputs)
  before driving DATA, and verify sel reads back. One-line fix in
  `tx_daemon.py`.

## Result 5 — EDID status

**No.** The board still advertises 720p60-only EDID. The packetizer still
does 2:1 frame skip (`ST_SKIP`) in the PL. Native 30 Hz EDID is still open.

Consequences of current setup:

- Capture burst = 141k pkt/s for one frame, then idle — hard for 2 buffers.
- Native 30 Hz source would give a flat 70.56k pkt/s, no skip state, no burst.

Recommended order stays: fix shim service rate first (it is 8x short even of
the flat rate), then native 30 Hz EDID, then remove ST_SKIP.

## Incident log

- Test script created a second `Overlay()` under the running daemon and
  switched clocks -> PL reprogrammed mid-run, HPD dropped, PS hung. Board
  power-cycled, all restored. Rule now: only the daemon touches the PL; no
  second Overlay; no clock writes from probes.

## Action list (ranked)

1. Daemon: clear clkctrl `GPIO_TRI` to outputs; verify sel readback; then
   re-run flood at 50 vs 100 MHz to confirm the switch really happens.
2. Shim: attack `send=36us` — batch multiple datagrams per syscall
   (sendmmsg), raise SO_SNDBUF, or one connected socket. Target < 10 µs/pkt.
3. Shim/writer: shrink round trip (wait+ack ~18 µs) — e.g. check both
   buffers per poll, or N-buffer ring instead of ping-pong.
4. Only after 1–3: native 30 Hz EDID, remove ST_SKIP, re-measure video path.
