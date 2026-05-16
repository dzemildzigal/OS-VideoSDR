# Next Machine Handoff

This file is the continuation checklist for moving development to another computer.

## Current State

- Project structure, protocol scaffolding, and runtime skeleton are in place.
- Wired TX and RX Python harness entrypoints exist for protocol bring-up.
- Unit and integration tests are present and passing.
- PL-first architecture decision and constraints are documented.
- PYNQ PS protocol bring-up is now validated at synthetic low-to-medium payload rates.
- Full-frame 1080p raw smoke tests on PS software path still show kernel RX buffer drops.
- PS C shim now has runnable TX/RX with selectable `socket` or mmap `ring` transport backend in `pynq/ps_shim`.
- U10 and U15 acceptance gates are still open pending DMA + PL-first data path progress.

Key docs:

- docs/pynq_pl_first_architecture.md
- docs/protocol_spec.md
- docs/crypto_policy.md
- docs/latency_budget.md
- docs/test_plan.md
- pynq/ps_shim/README.md
- docs/pl_ring_uio_spec.md
- docs/templates/ring_uio_template.dtsi
- scripts/check_uio_ring_map.sh

## What To Copy

Copy the entire repository directory.

Include:

- docs/
- config/
- protocol/
- pynq/
- pc/
- scripts/
- tests/
- README.md

Exclude generated artifacts if present:

- __pycache__ directories
- .pytest_cache

## Tooling On New Machine

Required:

- Python 3.10+ (tested with 3.12)
- pytest
- PyYAML

Optional for AES-GCM software path:

- cryptography

Install commands:

- python -m pip install pytest PyYAML
- python -m pip install cryptography

## Quick Validation After Copy

1. Run integration tests.

```bash
python -m pytest tests/integration/ -v
```

2. Verify unified entrypoints load.

```bash
python -m pynq.runtime.main --help
python -m pc.runtime.main_rx --help
```

3. Build PS C shim baseline (optional).

```bash
chmod +x pynq/ps_shim/build.sh
./pynq/ps_shim/build.sh
```

4. Local loopback dry run (requires AES key in OSV_AES_KEY_HEX or --key-hex).

Terminal A (PC RX side):

```bash
export OSV_AES_KEY_HEX="000102030405060708090A0B0C0D0E0F000102030405060708090A0B0C0D0E0F"
python -m pc.runtime.main_rx --config-dir config --max-frames 10
```

Terminal B (PYNQ TX side, or localhost simulation):

```bash
export OSV_AES_KEY_HEX="000102030405060708090A0B0C0D0E0F000102030405060708090A0B0C0D0E0F"
python -m pynq.runtime.main \
  --config-dir config \
  --source synthetic \
  --crypto-mode aesgcm \
  --frames 10 \
  --fps 10
```

## Immediate Work Queue

1. Build and validate `pynq/ps_shim/build/ps_shim` loopback baseline on PYNQ.
2. Implement PL control + ring hardware using `docs/pl_ring_uio_spec.md` and expose one UIO node with map0 (ctrl), map1 (TX ring), map2 (RX ring).
3. Apply DT fragment from `docs/templates/ring_uio_template.dtsi` with board-specific addresses and IRQ.
4. Validate map sizes with `scripts/check_uio_ring_map.sh` for map1 and map2 before running ps_shim.
5. Integrate TX and RX ring ownership protocol between PL and PS with IRQ signaling.
6. Hook hardware counters to AXI-Lite map and verify IRQ status/ack behavior.
7. Run U10/U15 gate attempts on C-shim + PL-first path.

## PS C Shim Quick Start (Copy/Paste)

From PYNQ shell:

- cd /home/xilinx/jupyter_notebooks/OS-VideoSDR
- chmod +x pynq/ps_shim/build.sh
- ./pynq/ps_shim/build.sh

Terminal A (RX):

- ./pynq/ps_shim/build/ps_shim --mode rx --bind-ip 127.0.0.1 --port 5000 --max-runtime-s 20 --frame-bytes 120000 --segment-bytes 1200

Terminal B (TX):

- ./pynq/ps_shim/build/ps_shim --mode tx --target-ip 127.0.0.1 --port 5000 --frames 120 --fps 15 --frame-bytes 120000 --segment-bytes 1200 --inter-packet-gap-us 100

Expected outcome:

- RX prints non-zero packet counters and throughput.
- TX completes requested frame count with throughput summary.
- This baseline does not use ring descriptors yet; it verifies C transport loop viability before ring integration.

## Terminology Cheat Sheet (Plain Language)

- Frame: one full video image payload handled by the sender loop (example: `120000` bytes in synthetic tests).
- Packet: one UDP datagram on the wire, carrying one segment of a frame (example: `1200` bytes payload).
- Chunk: how much data is passed to AES in one crypto call.
	- Packet granularity: one AES call per packet.
	- Frame granularity: one AES call per frame.
	- Chunk granularity (next): one AES call per medium block, between packet and frame.

Why this matters:

- Too many tiny AES calls can dominate runtime even when AES hardware is fast.
- Very large crypto units reduce call overhead, but can add buffering delay.
- Practical low-latency target is usually a middle chunk size after measurement.

## Session Evidence (2026-05-10)

- Stable software-path protocol run (AES-GCM):
	- `--fps 15 --synthetic-frame-bytes 72000`
	- TX and RX packet/frame parity reached (`18000` packets each, `300` frames complete).
	- `drops=0`, `decrypt_fail=0`, `reorder=0`.
- Full-frame 1080p raw smoke run (`6220800` bytes/frame) on PS path:
	- RX packet count significantly lower than TX packet count.
	- `netstat -su` counters (`packet receive errors`, `receive buffer errors`) increased during the run.
	- Result: PS Python path is not sufficient evidence for U15.

## Decision Update (2026-05-10, Full-HD Gate)

- Full-HD FPS sweep runs returned `FAIL_RX_FRAMES` with large kernel UDP receive-buffer error deltas for all tested FPS values.
- Decision: pivot active throughput-path implementation from Python runtime to PS C shim + PL descriptor-ring integration.
- Python runtime remains the protocol and diagnostics harness.

## Session Evidence (2026-05-11, C-Shim A/B and UIO Discovery)

Profile A (identical bounded run, `frames=120`, `fps=15`, `frame_bytes=120000`, `segment_bytes=1200`):

- Socket TX: `throughput_mbps=14.40`, RX completed `12000` packets.
- Ring TX (mmap prototype): `throughput_mbps=14.40`, RX completed `12000` packets.
- Interpretation: this run is frame-rate limited by configuration; both backends match expected payload rate.

Profile B (stress run, `max-runtime-s=20`, `fps=500`, `frame_bytes=120000`, `segment_bytes=1200`, no inter-packet gap):

- Socket TX: `throughput_mbps=251.78`, RX observed `throughput_mbps=225.39` at run end.
- Ring TX (mmap prototype): `throughput_mbps=480.00`, RX observed `throughput_mbps=433.29` at run end.
- Interpretation: mmap ring prototype is about `1.7x` faster than socket path for this stress profile.

UIO discovery on board:

- `/dev/uio0`: `audio-codec-ctrl` (`audio-codec-ctrl@43c00000`).
- `/dev/uio1`: `fabric` (`fabric@40000000`).
- No dedicated ring-named device found under `/dev`.
- During current prototype runs, no ring-specific interrupt activity was observed.

Follow-up validation (latest run):

- `/dev/uio1` map0 size is `0x00010000` (64 KiB).
- Requested ring layout (`slot_count=8192`, `slot_payload=4096`) needs `34078752` bytes.
- Backend now fails fast with `ENOSPC` and message: UIO map too small for requested ring layout.
- Conclusion: `/dev/uio1` is not a suitable data-plane ring memory target for current settings.
- Action: use `/dev/shm/osv_ring.bin` for software-path benchmarking until a dedicated ring memory region is exposed via DT/UIO.

Current backend status after this evidence:

- `pynq/ps_shim/src/ring_backend.c` now supports mmap file path and `/dev/uioX` mapping path with env-selectable map index and ring offset.
- It is still a polling transport prototype; board-native PL ownership and IRQ-backed flow remain open integration work.
- Safety guardrails are active:
	- char-device mappings are not auto-reset unless `OSV_RING_UIO_ALLOW_RESET=1` is set.
	- undersized UIO maps fail fast unless `OSV_RING_UIO_ALLOW_CLAMP=1` is set for debug-only runs.

## Session Evidence (2026-05-10, DMA Granularity Benchmark)

Test shape:

- `frames=180`, `fps=15`, synthetic frame bytes `120000`, segment bytes `1200`, inter-packet gap `100 us`.
- Current overlay posture: TX uses DMA encrypt path, RX uses software AES-GCM verify/decrypt.

Observed end-to-end results (validated):

- Packet granularity:
	- `TX done ... throughput_mbps=2.93`
	- `TX dma done: calls=18000 avg_encrypt_ms=2.858 avg_dma_ms=1.490 avg_control_ms=1.368 avg_tag_wait_ms=0.203`
	- `RX done: frames=180 packets=18000 drops=0 decrypt_fail=0 reorder=0 latency_p95_ms=353.64`
- Frame granularity:
	- `TX done ... throughput_mbps=15.07`
	- `TX dma done: calls=180 avg_encrypt_ms=4.095 avg_dma_ms=2.838 avg_control_ms=1.257 avg_tag_wait_ms=0.240`
	- `RX done: frames=180 packets=18000 drops=0 decrypt_fail=0 reorder=0 latency_p95_ms=53.03`

Chunk sweep results (same profile):

- `chunk=4800`: `throughput=8.90`, `calls=4500`, `latency_p95_ms=146.11`
- `chunk=12000`: `throughput=14.13`, `calls=1800`, `latency_p95_ms=93.34`
- `chunk=24000`: `throughput=15.07`, `calls=900`, `latency_p95_ms=71.71`
- `chunk=48000`: `throughput=15.07`, `calls=540`, `latency_p95_ms=65.05`
- `chunk=96000`: `throughput=15.07`, `calls=360`, `latency_p95_ms=61.21`

Validation status from this sweep:

- Packet parity reached in all cases (`frames=180`, `packets=18000`).
- `drops=0`, `decrypt_fail=0`, `reorder=0` in all cases.
- `netstat -su` receive buffer error counters remained unchanged.

Current decision for this profile:

- Default runtime granularity: `frame` (best observed latency at max observed throughput).
- If chunk boundaries are required, prefer larger chunk sizes (`48000` to `96000`) for near-frame throughput.

## Session Evidence (2026-05-10, Breakpoint Sweep)

Test shape:

- `frames=90`, `fps=15`, `segment_bytes=1200`, `inter-packet-gap-us=100`.
- Compared `frame` versus `chunk` (`crypto-chunk-bytes=96000`) at larger frame payloads.

Observed results:

- `frame_bytes=240000`:
	- frame mode: `TX throughput=23.70`, `RX done=90/90`, `latency_p95_ms=108.54`, UDP counter delta `0`.
	- chunk 96000: `TX throughput=22.03`, `RX done=90/90`, `latency_p95_ms=125.64`, UDP counter delta `0`.
- `frame_bytes=480000`:
	- frame mode: `TX throughput=24.89`, `RX done=73/90`, UDP receive buffer error delta `+174`.
	- chunk 96000: `TX throughput=21.63`, `RX done=90/90`, UDP counter delta `0`.
- `frame_bytes=960000`:
	- frame mode: `TX throughput=24.45`, `RX done=45/90`, UDP receive buffer error delta `+6126`.
	- chunk 96000: `TX throughput=21.59`, `RX done=45/90`, UDP receive buffer error delta `+5640`.

Decision from breakpoint sweep:

- Up to `240000` bytes/frame, keep `frame` mode.
- At `480000` bytes/frame, prefer `chunk` with `crypto-chunk-bytes=96000` for robustness.
- At `960000` bytes/frame, neither mode is stable under current pacing and software RX verify path.

## DMA Next Session Checklist

Current implementation status:

- `pynq/runtime/aes_gcm_dma.py` now contains overlay load and encrypt-path DMA plumbing.
- `pynq/runtime/main.py` now provides the unified PYNQ TX orchestrator.
- `pc/runtime/main_rx.py` now provides the unified PC RX decrypt/display path.
- RX DMA mode currently enforces decrypt-capable-overlay guard via `--dma-decrypt-supported`.
- For this project stage, run TX DMA on PYNQ and decrypt on PC software path first.
- DMA runtime validation is hardware-only and must be executed on a PYNQ target, not a host PC.

1. Confirm hardware assets and interface contract.
	- Verify `.bit/.hwh` paths, IP instance names, DMA instance names, and channel directions.
	- Confirm nonce and AAD field contract is unchanged from protocol docs.
2. Implement board DMA adapter in `pynq/runtime/aes_gcm_dma.py`.
	- Implement `load()` to bind overlay + DMA resources.
	- Implement `encrypt()` and `decrypt()` with timeout/error mapping.
	- Keep method signatures compatible with current TX/RX cipher call pattern.
3. Wire DMA mode into `pynq/runtime/main.py` and keep PC decrypt in `pc/runtime/main_rx.py`.
	- Add runtime mode selection for DMA-backed crypto.
	- Keep current software modes for fallback diagnostics.
	- Use RX DMA mode only with a decrypt-capable bitstream; otherwise use software decrypt on RX for verification.
4. Run DMA smoke ladder (same key/session policy).
	- Step A: `72000` bytes/frame at `15` fps.
	- Step B: `120000` bytes/frame at `15` fps.
	- Step C: `6220800` bytes/frame short run first, then longer run.
5. Capture evidence at each step.
	- TX packets vs RX packets.
	- RX `drops`, `decrypt_fail`, `reorder`.
	- `netstat -su` drop counters before/after each run.
6. Exit criteria before calling U10/U15 gate attempts.
	- No growth in kernel receive buffer error counters on chosen profile.
	- Stable run without stalls or nonce/auth regressions.
	- Throughput envelope moves beyond PS software baseline.
7. If full-frame drops persist, continue with PS C shim work queue above.
	- Keep Python runtime as protocol/debug harness only.

## First 30 Minutes Tomorrow (Copy/Paste)

Run these commands on the PYNQ shell to restart quickly with logs and clear pass or fail evidence.

1. Session setup and log folder.

- cd /home/xilinx/jupyter_notebooks/OS-VideoSDR
- export KEY_HEX=000102030405060708090A0B0C0D0E0F000102030405060708090A0B0C0D0E0F
- export RUN_TS=$(date +%Y%m%d_%H%M%S)
- export RUN_DIR=artifacts/logs/$RUN_TS
- mkdir -p "$RUN_DIR"

2. Environment snapshot.

- uname -a | tee "$RUN_DIR/00_env.txt"
- python --version | tee -a "$RUN_DIR/00_env.txt"
- ip -br addr | tee -a "$RUN_DIR/00_env.txt"
- python -m pytest -q | tee "$RUN_DIR/01_pytest.txt"

3. Overlay and runtime asset check.

- find pynq/overlays -maxdepth 4 -type f \( -name "*.bit" -o -name "*.hwh" \) | tee "$RUN_DIR/02_overlays.txt"
- python pynq/runtime/main.py --help > "$RUN_DIR/03_tx_help.txt"
- python pc/runtime/main_rx.py --help > "$RUN_DIR/04_rx_help.txt"

4. UDP drop counters before test.

- netstat -su | grep -E "packet receive errors|receive buffer errors" | tee "$RUN_DIR/05_udp_before.txt"

5. Quick known-good software-path sanity (stable synthetic load).

- timeout 30s python pc/runtime/main_rx.py --bind-ip 127.0.0.1 --port 5000 --max-frames 120 --key-hex "$KEY_HEX" > "$RUN_DIR/06_rx_sw_sanity.txt" 2>&1 &
- sleep 1
- python pynq/runtime/main.py --mode tx --target-ip 127.0.0.1 --target-port 5000 --frames 120 --fps 15 --frame-bytes 72000 --segment-bytes 1200 --crypto-mode aesgcm --key-hex "$KEY_HEX" > "$RUN_DIR/07_tx_sw_sanity.txt" 2>&1

6. UDP drop counters after sanity.

- netstat -su | grep -E "packet receive errors|receive buffer errors" | tee "$RUN_DIR/08_udp_after_sw_sanity.txt"

7. DMA TX smoke with software RX verify (current encrypt-only overlay).

- timeout 40s python pc/runtime/main_rx.py --bind-ip 127.0.0.1 --port 5000 --max-frames 180 --key-hex "$KEY_HEX" > "$RUN_DIR/09_rx_sw_verify_for_dma_tx.txt" 2>&1 &
- sleep 1
- python pynq/runtime/main.py --mode tx --target-ip 127.0.0.1 --target-port 5000 --frames 180 --fps 15 --frame-bytes 120000 --segment-bytes 1200 --crypto-mode dma --key-hex "$KEY_HEX" > "$RUN_DIR/10_tx_dma_smoke.txt" 2>&1
- netstat -su | grep -E "packet receive errors|receive buffer errors" | tee "$RUN_DIR/11_udp_after_dma_tx_smoke.txt"

8. What to check in logs before moving on.

- Packet parity (TX packets == RX packets).
- RX counters: `drops=0`, `decrypt_fail=0`, `reorder=0`.
- No growth in UDP kernel receive buffer errors for the selected test profile.

## Full-HD Capacity Gate (Why This Is Not Just Testing)

Purpose:

- Determine the sustainable full-HD raw frame-rate envelope for the current runtime stack.
- Convert measurements into a concrete go/no-go decision for PL-first integration work.

Automation script:

- `scripts/run_fullhd_fps_sweep.sh`

How to run on PYNQ:

- `cd /home/xilinx/jupyter_notebooks/OS-VideoSDR`
- `chmod +x scripts/run_fullhd_fps_sweep.sh`
- `./scripts/run_fullhd_fps_sweep.sh`

Optional overrides:

- `FPS_LIST="1 2 3 5 8" FRAME_BYTES=6220800 INTER_PACKET_GAP_US=0 ./scripts/run_fullhd_fps_sweep.sh`
- `CRYPTO_GRANULARITY=chunk CRYPTO_CHUNK_BYTES=96000 ./scripts/run_fullhd_fps_sweep.sh`

What comes out of this gate:

- A per-FPS pass/fail decision with reasons (`FAIL_RX_FRAMES`, `FAIL_UDP_DROPS`, etc.).
- A clear ceiling for current stack capability at full-HD payload.
- Evidence to decide the next engineering move:
	- If stable at target FPS: proceed to longer soak and then HDMI path integration.
	- If unstable at target FPS: prioritize PS C shim + PL-first datapath migration before further feature work.

Output location:

- `artifacts/logs/<timestamp>_fullhd_fps_sweep/summary.txt`

## Benchmark Note

Legacy packet-vs-frame benchmark commands in this document previously used deleted split entrypoints.
Use the unified commands above (`pynq/runtime/main.py` and `pc/runtime/main_rx.py`) for current validation runs.

## Definition of Done for Next Step

The next major milestone is complete when:

- Python is no longer on the PYNQ data path for wired TX/RX throughput runs.
- PL performs packetization and AES-GCM operations.
- PS is limited to networking shim and control.
- U10 profile passes 30-minute stability gate.

## Notes

- Existing Python entrypoints are bring-up tools, not final performance architecture.
- Onboard PYNQ-Z2 Ethernet is PS-connected, so full onboard PL-only RJ45 data path is not a baseline target.
