# Next Machine Handoff

This file is the continuation checklist for moving development to another computer.

## Current State

- Project structure, protocol scaffolding, and runtime skeleton are in place.
- Wired TX and RX Python harness entrypoints exist for protocol bring-up.
- Unit and integration tests are present and passing.
- PL-first architecture decision and constraints are documented.
- PYNQ PS protocol bring-up is now validated at synthetic low-to-medium payload rates.
- Full-frame 1080p raw smoke tests on PS software path still show kernel RX buffer drops.
- U10 and U15 acceptance gates are still open pending DMA + PL-first data path progress.

Key docs:

- docs/pynq_pl_first_architecture.md
- docs/protocol_spec.md
- docs/crypto_policy.md
- docs/latency_budget.md
- docs/test_plan.md

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

1. Run tests.

- python -m pytest -q

2. Verify entrypoints load.

- python pynq/runtime/tx_main.py --help
- python pynq/runtime/rx_main.py --help

3. Optional local loopback dry run.

Terminal A:

- python pynq/runtime/rx_main.py --bind-ip 127.0.0.1 --listen-port 5000 --max-frames 30

Terminal B:

- python pynq/runtime/tx_main.py --target-ip 127.0.0.1 --target-port 5000 --frames 30 --fps 10

## Immediate Work Queue

1. Implement PS C shim for minimal GEM descriptor loop.
2. Implement PL descriptor producer for TX path.
3. Integrate TX ring ownership protocol between PL and PS.
4. Implement RX ring ingestion from PS to PL.
5. Hook hardware counters to AXI-Lite map.
6. Run U10 gate with PS C shim replacing Python data path.

## Session Evidence (2026-05-10)

- Stable software-path protocol run (AES-GCM):
	- `--fps 15 --synthetic-frame-bytes 72000`
	- TX and RX packet/frame parity reached (`18000` packets each, `300` frames complete).
	- `drops=0`, `decrypt_fail=0`, `reorder=0`.
- Full-frame 1080p raw smoke run (`6220800` bytes/frame) on PS path:
	- RX packet count significantly lower than TX packet count.
	- `netstat -su` counters (`packet receive errors`, `receive buffer errors`) increased during the run.
	- Result: PS Python path is not sufficient evidence for U15.

## DMA Next Session Checklist

1. Confirm hardware assets and interface contract.
	- Verify `.bit/.hwh` paths, IP instance names, DMA instance names, and channel directions.
	- Confirm nonce and AAD field contract is unchanged from protocol docs.
2. Implement board DMA adapter in `pynq/runtime/aes_gcm_dma.py`.
	- Implement `load()` to bind overlay + DMA resources.
	- Implement `encrypt()` and `decrypt()` with timeout/error mapping.
	- Keep method signatures compatible with current TX/RX cipher call pattern.
3. Wire DMA mode into `pynq/runtime/tx_main.py` and `pynq/runtime/rx_main.py`.
	- Add runtime mode selection for DMA-backed crypto.
	- Keep current software modes for fallback diagnostics.
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
- python pynq/runtime/tx_main.py --help > "$RUN_DIR/03_tx_help.txt"
- python pynq/runtime/rx_main.py --help > "$RUN_DIR/04_rx_help.txt"

4. UDP drop counters before test.

- netstat -su | grep -E "packet receive errors|receive buffer errors" | tee "$RUN_DIR/05_udp_before.txt"

5. Quick known-good software-path sanity (stable synthetic load).

- timeout 30s python pynq/runtime/rx_main.py --bind-ip 127.0.0.1 --listen-port 5000 --max-frames 120 --max-packets 12000 --max-idle-s 2 --max-runtime-s 30 --crypto-mode aesgcm --key-hex "$KEY_HEX" --recv-buffer-bytes 33554432 > "$RUN_DIR/06_rx_sw_sanity.txt" 2>&1 &
- sleep 1
- python pynq/runtime/tx_main.py --target-ip 127.0.0.1 --target-port 5000 --frames 120 --fps 15 --synthetic-frame-bytes 72000 --inter-packet-gap-us 100 --crypto-mode aesgcm --key-hex "$KEY_HEX" --send-buffer-bytes 33554432 > "$RUN_DIR/07_tx_sw_sanity.txt" 2>&1

6. UDP drop counters after sanity.

- netstat -su | grep -E "packet receive errors|receive buffer errors" | tee "$RUN_DIR/08_udp_after_sw_sanity.txt"

7. DMA smoke placeholders (run after DMA mode is wired into TX and RX).

- timeout 40s python pynq/runtime/rx_main.py --bind-ip 127.0.0.1 --listen-port 5000 --max-frames 180 --max-packets 18000 --max-idle-s 2 --max-runtime-s 40 --crypto-mode dma --key-hex "$KEY_HEX" --recv-buffer-bytes 67108864 > "$RUN_DIR/09_rx_dma_smoke.txt" 2>&1 &
- sleep 1
- python pynq/runtime/tx_main.py --target-ip 127.0.0.1 --target-port 5000 --frames 180 --fps 15 --synthetic-frame-bytes 120000 --inter-packet-gap-us 100 --crypto-mode dma --key-hex "$KEY_HEX" --send-buffer-bytes 67108864 > "$RUN_DIR/10_tx_dma_smoke.txt" 2>&1
- netstat -su | grep -E "packet receive errors|receive buffer errors" | tee "$RUN_DIR/11_udp_after_dma_smoke.txt"

8. What to check in logs before moving on.

- Packet parity (TX packets == RX packets).
- RX counters: `drops=0`, `decrypt_fail=0`, `reorder=0`.
- No growth in UDP kernel receive buffer errors for the selected test profile.

## Definition of Done for Next Step

The next major milestone is complete when:

- Python is no longer on the PYNQ data path for wired TX/RX throughput runs.
- PL performs packetization and AES-GCM operations.
- PS is limited to networking shim and control.
- U10 profile passes 30-minute stability gate.

## Notes

- Existing Python entrypoints are bring-up tools, not final performance architecture.
- Onboard PYNQ-Z2 Ethernet is PS-connected, so full onboard PL-only RJ45 data path is not a baseline target.
