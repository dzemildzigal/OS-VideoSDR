# OS-VideoSDR

Open source encrypted low-latency video transport development for PYNQ-Z2 and AntSDR.

## Mission

Build an end-to-end encrypted live video link in three stages:

1. Wired proof-of-concept over 1 GbE (PYNQ and PC).
2. SDR transport on AntSDR E310 with the same packet and crypto contract.
3. Frequency hopping after the non-hopping radio path is stable.

## Current Status (2026-05-10)

- Protocol and runtime bring-up harnesses are implemented and tested.
- Python runtime scripts are functional for validation and benchmarking:
  - [pynq/runtime/tx_main.py](pynq/runtime/tx_main.py)
  - [pynq/runtime/rx_main.py](pynq/runtime/rx_main.py)
- PS C shim scaffold is available for lower-overhead transport bring-up:
  - [pynq/ps_shim/README.md](pynq/ps_shim/README.md)
- DMA AES adapter is integrated in:
  - [pynq/runtime/aes_gcm_dma.py](pynq/runtime/aes_gcm_dma.py)
- Supported crypto modes:
  - none
  - aesgcm (software)
  - dma (hardware adapter)
- Supported crypto granularities:
  - packet
  - frame
  - chunk

Important architecture note:

- Current Python scripts are bring-up tooling, not the final high-performance production datapath.
- Production direction remains PL-first datapath with PS as thin networking and control shim.
- Full-HD FPS gate runs failed on Python PS runtime (`RX frames=0` with large UDP drop deltas), so active implementation focus has moved to PS C shim + PL ring integration.

## Simple Terms

- Frame: one whole video image payload handled by the app.
- Packet: one UDP datagram carrying a segment of a frame.
- Chunk: how much payload is sent to AES in one crypto call.

Granularity mapping:

- Packet granularity: one AES call per packet.
- Frame granularity: one AES call per frame.
- Chunk granularity: one AES call per medium-size block between packet and frame.

## What Works Now

- Header validation, replay checks, segmentation, and reassembly paths are implemented.
- Synthetic traffic generation and telemetry reporting are stable.
- Runtime safety/operability features are in place:
  - max runtime and max idle exits on RX
  - throughput reporting (average and instant)
  - inter-packet pacing on TX

## Known Limits Right Now

- The currently validated DMA overlay path is encrypt-focused.
- RX DMA mode requires a decrypt-capable overlay and is guarded by explicit runtime checks.
- U10 and U15 acceptance gates are still open for production architecture.

## Latest Benchmark Highlights

### Baseline Mode Matrix (Synthetic)

- none: about 15 Mb/s class
- aesgcm software: about 9 Mb/s class
- dma with packet granularity: about 3 Mb/s class

### Packet vs Frame DMA Granularity (Validated End-to-End)

Test shape:

- frames=180
- fps=15
- frame payload=120000 bytes
- packet payload=1200 bytes
- inter-packet-gap-us=100

Observed results:

- Packet granularity:
  - TX throughput: 2.93 Mb/s
  - TX dma calls: 18000
  - RX: frames=180 packets=18000 drops=0 decrypt_fail=0 reorder=0
  - p95 latency: 353.64 ms
- Frame granularity:
  - TX throughput: 15.07 Mb/s
  - TX dma calls: 180
  - RX: frames=180 packets=18000 drops=0 decrypt_fail=0 reorder=0
  - p95 latency: 53.03 ms

### Chunk Sweep (Validated End-to-End)

Chunk mode was measured with the same test shape.

- chunk 4800:
  - TX throughput: 8.90 Mb/s
  - TX dma calls: 4500
  - p95 latency: 146.11 ms
- chunk 12000:
  - TX throughput: 14.13 Mb/s
  - TX dma calls: 1800
  - p95 latency: 93.34 ms
- chunk 24000:
  - TX throughput: 15.07 Mb/s
  - TX dma calls: 900
  - p95 latency: 71.71 ms
- chunk 48000:
  - TX throughput: 15.07 Mb/s
  - TX dma calls: 540
  - p95 latency: 65.05 ms
- chunk 96000:
  - TX throughput: 15.07 Mb/s
  - TX dma calls: 360
  - p95 latency: 61.21 ms

All chunk runs completed with RX parity (frames=180, packets=18000), no drops/decrypt failures/reorder, and zero UDP receive buffer error growth.

Interpretation:

- The primary gain comes from reducing per-call orchestration overhead, not changing AES core behavior.
- Fewer, larger crypto calls are much more efficient than many tiny calls in this runtime architecture.
- For this 120000-byte frame profile, frame mode is currently the best latency choice at max observed throughput.

Practical operating choice for current profile:

- Default: `frame` crypto granularity.
- Optional: large `chunk` values (`48000` to `96000`) when integration constraints require chunk boundaries while keeping near-frame throughput.

### Breakpoint Sweep (Larger Frame Payloads)

Additional end-to-end runs were executed at `frames=90`, `fps=15`, `segment_bytes=1200`, `inter-packet-gap-us=100`:

- `frame_bytes=240000`:
  - frame mode: stable, `throughput=23.70`, `latency_p95_ms=108.54`, no UDP error growth.
  - chunk 96000: stable, `throughput=22.03`, `latency_p95_ms=125.64`, no UDP error growth.
- `frame_bytes=480000`:
  - frame mode: unstable (`frames_completed=73/90`), UDP receive buffer errors increased by `174`.
  - chunk 96000: stable (`90/90`), no additional UDP error growth.
- `frame_bytes=960000`:
  - frame mode: unstable (`45/90`), UDP receive buffer errors increased by `6126`.
  - chunk 96000: unstable (`45/90`), UDP receive buffer errors increased by `5640`.

Interpretation from breakpoint sweep:

- Up to `240000` bytes/frame, frame mode remains best overall.
- Around `480000` bytes/frame, chunk mode (`96000`) is more robust than frame mode.
- At `960000` bytes/frame with current pacing and software RX verify path, both modes exceed the stable envelope.

## Reproducible Commands

Use the maintained copy-paste benchmark recipe in:

- [docs/next_machine_handoff.md](docs/next_machine_handoff.md)

Start at the section:

- Corrected Packet vs Frame Benchmark (Copy/Paste)

## PS C Shim Quick Start

Build on PYNQ Linux:

- `chmod +x pynq/ps_shim/build.sh`
- `./pynq/ps_shim/build.sh`

Loopback smoke test:

- RX: `./pynq/ps_shim/build/ps_shim --mode rx --bind-ip 127.0.0.1 --port 5000 --max-runtime-s 20 --frame-bytes 120000 --segment-bytes 1200`
- TX: `./pynq/ps_shim/build/ps_shim --mode tx --target-ip 127.0.0.1 --port 5000 --frames 120 --fps 15 --frame-bytes 120000 --segment-bytes 1200 --inter-packet-gap-us 100`

More details:

- [pynq/ps_shim/README.md](pynq/ps_shim/README.md)

## Repository Guide

- [docs](docs): architecture decisions, protocol policy, handoff notes
- [config](config): runtime settings
- [protocol](protocol): shared packet schema and validation
- [pynq/runtime](pynq/runtime): board-side bring-up runtime and DMA adapter
- [pynq/ps_shim](pynq/ps_shim): PS-side C transport shim scaffold for PL-first migration
- [pc](pc): host-side runtime tooling
- [tests](tests): unit and integration validation

## Next Engineering Steps

1. Use the PS C shim as the primary transport baseline and re-run controlled FPS sweeps to quantify improvement over Python runtime.
2. Replace `ring_stub.c` with a real descriptor-ring backend and move TX/RX buffers to ring-owned memory.
3. Integrate PS ring ownership transitions with PL producer/consumer logic and expose hardware counters.
4. Validate a decrypt-capable DMA overlay for true RX hardware DMA path.
5. Re-run U10/U15 acceptance gates on the C-shim + PL-first path.

## Source of Truth for Handover

For machine migration, session evidence, and exact run commands use:

- [docs/next_machine_handoff.md](docs/next_machine_handoff.md)
