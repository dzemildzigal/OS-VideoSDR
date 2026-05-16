# OS-VideoSDR

Open source encrypted low-latency video transport development for PYNQ-Z2 and AntSDR.

## Reset Notice (May 2026)

This repository is now the single integration home for the video pipeline.

V1 scope:
- PYNQ: HDMI in -> AES encrypt in PL -> DDR DMA -> PS Ethernet sender
- PC: UDP server -> AES decrypt in software -> OpenCV display

Not in V1:
- PYNQ HDMI out path (deferred until V1 gates pass)

## Mission

Build an end-to-end encrypted live video link in three stages:

1. Wired proof-of-concept over 1 GbE (PYNQ and PC).
2. SDR transport on AntSDR E310 with the same packet and crypto contract.
3. Frequency hopping after the non-hopping radio path is stable.

## Current Status

- Phase C-D implementation: unified runtime spine with config loader, nonce enforcement, and display integration.
- Protocol contract is implemented and validated.
- Unified runtime entrypoints ready for bring-up:
  - **PYNQ TX:** [pynq/runtime/main.py](pynq/runtime/main.py) - synthesizes or captures HDMI, encrypts, sends UDP
  - **PC RX:** [pc/runtime/main_rx.py](pc/runtime/main_rx.py) - receives UDP, decrypts, displays (OpenCV or headless)
  - **Config:** [config_loader.py](config_loader.py) - unified YAML-based config for both sides (network.yaml, crypto.yaml)
- Crypto modes supported:
  - `none` (plaintext)
  - `aesgcm` (software, always available on PC)
  - `dma` (hardware adapter on PYNQ, requires bitstream)
- Integration tests present:
  - Roundtrip TX encrypt → RX decrypt validation
  - Nonce monotonicity and replay window enforcement
  - Frame reassembly integrity (in-order, out-of-order, multi-frame)
- PS C shim scaffold available for lower-overhead transport backend:
  - [pynq/ps_shim/README.md](pynq/ps_shim/README.md)
  - Selectable `socket` and `ring` transport backends
- Hardware implementation references:
  - [docs/pl_ring_uio_spec.md](docs/pl_ring_uio_spec.md)
  - [docs/templates/ring_uio_template.dtsi](docs/templates/ring_uio_template.dtsi)
  - [scripts/check_uio_ring_map.sh](scripts/check_uio_ring_map.sh)

Important architecture note:

- This repo owns integration orchestration end-to-end; AES core hardware ownership remains in AES-256-SystemVerilog.
- Production direction remains PL-first datapath with PS as thin networking and control shim.
- Legacy split entrypoints are being removed in favor of one orchestrator per side.

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

### PS C Shim Transport A/B (2026-05-11)

Identical bounded run (`frames=120`, `fps=15`, `frame_bytes=120000`, `segment_bytes=1200`):

- socket TX: `14.40 Mb/s`
- ring TX (mmap prototype): `14.40 Mb/s`
- interpretation: both are frame-rate limited at this profile.

Stress run (`max-runtime-s=20`, `fps=500`, no inter-packet gap):

- socket TX: `251.78 Mb/s`, socket RX final: `225.39 Mb/s`
- ring TX (mmap prototype): `480.00 Mb/s`, ring RX final: `433.29 Mb/s`
- interpretation: ring prototype is about `1.7x` faster than socket transport for this stress shape.

UIO discovery on board:

- `/dev/uio0`: `audio-codec-ctrl`
- `/dev/uio1`: `fabric`
- no dedicated ring-named device was present in `/dev`.

UIO fail-fast validation (latest run):

- `/dev/uio1` map0 size: `0x00010000` (64 KiB).
- Requested ring layout (`slot_count=8192`, `slot_payload=4096`) requires `34078752` bytes.
- Backend correctly returns `ENOSPC` with explicit message that UIO map is too small.
- Conclusion: `/dev/uio1` is not a viable ring data-memory target for current profile.
- Operational guidance: keep performance benchmarking on `/dev/shm/osv_ring.bin` until a dedicated ring-memory UIO mapping is provided by hardware/DT.

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
- [pynq/runtime](pynq/runtime): board-side orchestrator, ingest, crypto adapter, transport
- [pynq/ps_shim](pynq/ps_shim): PS-side C transport shim scaffold for PL-first migration
- [pc](pc): host-side runtime tooling (decrypt and display)
- [tests](tests): unit and integration validation

## Next Engineering Steps

1. Complete V1 pipeline: HDMI in on PYNQ to AES encrypt in PL to PS UDP sender.
2. Complete PC receiver path: receive, decrypt, and display with OpenCV.
3. Keep protocol and nonce policy unchanged while replacing legacy runtime entrypoints.
4. Use PS C shim as optional performance backend after Python orchestrator parity is achieved.
5. Defer HDMI out until V1 network pipeline gates are fully green.

## Source of Truth for Handover

For machine migration, session evidence, and exact run commands use:

- [docs/next_machine_handoff.md](docs/next_machine_handoff.md)
