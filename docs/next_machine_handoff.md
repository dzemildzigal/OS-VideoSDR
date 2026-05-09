# Next Machine Handoff

This file is the continuation checklist for moving development to another computer.

## Current State

- Project structure, protocol scaffolding, and runtime skeleton are in place.
- Wired TX and RX Python harness entrypoints exist for protocol bring-up.
- Unit and integration tests are present and passing.
- PL-first architecture decision and constraints are documented.

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

## Definition of Done for Next Step

The next major milestone is complete when:

- Python is no longer on the PYNQ data path for wired TX/RX throughput runs.
- PL performs packetization and AES-GCM operations.
- PS is limited to networking shim and control.
- U10 profile passes 30-minute stability gate.

## Notes

- Existing Python entrypoints are bring-up tools, not final performance architecture.
- Onboard PYNQ-Z2 Ethernet is PS-connected, so full onboard PL-only RJ45 data path is not a baseline target.
