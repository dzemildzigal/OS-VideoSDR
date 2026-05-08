# Test Plan

## Gate Order

1. Crypto baseline gate.
2. U10 wired TX gate.
3. U10 wired RX gate.
4. U15 wired TX and RX gates.
5. Latency gate (p95 < 50 ms).
6. C60 gate after H.264.
7. AntSDR non-hopping gate.
8. FHSS gate.

## Core Test Suites

- Unit tests: packet parsing, validation, nonce handling.
- Integration tests: wired TX and wired RX interoperability.
- Soak tests: 30-minute and multi-hour continuity.
- Fault tests: packet drop, reorder, jitter stress.

## Required Evidence per Gate

- Run configuration snapshot.
- Metrics summary (loss, auth failures, latency percentiles).
- Short pass or fail report.
- Artifacts stored under artifacts/metrics and artifacts/logs.

## Failure Triage Order

1. Crypto correctness.
2. Packet continuity and reassembly.
3. Latency and queue behavior.
4. Visual quality and frame stability.
