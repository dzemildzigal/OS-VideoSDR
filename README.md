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
- DMA AES adapter is integrated in:
  - [pynq/runtime/aes_gcm_dma.py](pynq/runtime/aes_gcm_dma.py)
- Supported crypto modes:
  - none
  - aesgcm (software)
  - dma (hardware adapter)
- Supported crypto granularities:
  - packet
  - frame

Important architecture note:

- Current Python scripts are bring-up tooling, not the final high-performance production datapath.
- Production direction remains PL-first datapath with PS as thin networking and control shim.

## Simple Terms

- Frame: one whole video image payload handled by the app.
- Packet: one UDP datagram carrying a segment of a frame.
- Chunk: how much payload is sent to AES in one crypto call.

Granularity mapping:

- Packet granularity: one AES call per packet.
- Frame granularity: one AES call per frame.
- Chunk granularity (next step): one AES call per medium-size block between packet and frame.

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

### Packet vs Frame DMA Granularity (TX Side)

Test shape:

- frames=300
- fps=15
- frame payload=120000 bytes
- packet payload=1200 bytes
- inter-packet-gap-us=100

Observed TX results:

- Packet granularity:
  - throughput about 3.02 Mb/s
  - dma calls=30000
- Frame granularity:
  - throughput about 15.07 Mb/s
  - dma calls=300

Interpretation:

- The large gain came from reducing per-call orchestration overhead, not from changing AES core logic.
- Fewer, larger crypto calls are currently much more efficient than many tiny calls.

Important caveat for that exact run:

- RX logs showed zero packets and zero frames in both modes.
- That run is valid as TX-side evidence only.
- Re-run with corrected RX timing before using as end-to-end proof.

## Reproducible Commands

Use the maintained copy-paste benchmark recipe in:

- [docs/next_machine_handoff.md](docs/next_machine_handoff.md)

Start at the section:

- Corrected Packet vs Frame Benchmark (Copy/Paste)

## Repository Guide

- [docs](docs): architecture decisions, protocol policy, handoff notes
- [config](config): runtime settings
- [protocol](protocol): shared packet schema and validation
- [pynq/runtime](pynq/runtime): board-side bring-up runtime and DMA adapter
- [pc](pc): host-side runtime tooling
- [tests](tests): unit and integration validation

## Next Engineering Steps

1. Re-run packet vs frame benchmark with corrected RX timing and collect valid RX parity counters.
2. Add chunk-level crypto mode and sweep chunk sizes for latency versus throughput tradeoff.
3. Validate a decrypt-capable DMA overlay for true RX hardware path.
4. Continue PS C shim and PL-first datapath migration for production throughput targets.

## Source of Truth for Handover

For machine migration, session evidence, and exact run commands use:

- [docs/next_machine_handoff.md](docs/next_machine_handoff.md)
