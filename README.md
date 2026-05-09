# OS-VideoSDR

Open Source Software Defined Radio video streaming for encrypted low-latency digital VTX / VRX links.

## 1) Mission

Build a high-throughput, low-latency, encrypted live video link in three milestones:

1. Wired encrypted video link over 1 Gb Ethernet (PYNQ-Z2 <-> PC), both directions.
1. Replace the wire with AntSDR E310 over 2.4 GHz radio while keeping the same packet and crypto contract.
1. Add FHSS after non-hopping radio is stable.

## 2) Locked Requirements

- Platform:
	- PYNQ-Z2 (Zynq-7020) for wired proof-of-concept and FPGA crypto path.
	- AntSDR E310 for full SDR radio stage.
- Encryption:
	- AES-256-GCM end-to-end.
	- Reuse proven FPGA AES-GCM path (measured up to 761 MiB/s datapath in related implementation workspace).
- Transport policy:
	- UDP only.
	- Sequence numbers and deadline-based late packet drop.
	- No retransmission in initial bring-up.
- Latency target:
	- End-to-end p95 < 50 ms in tuned mode.
- Media progression:
	- Start uncompressed.
	- Then add H.264 in Zynq silicon.
- Full HD progression:
	- Uncompressed first for system validation.
	- 1080p60 only after hardware H.264 path is integrated and validated.
- Protocol symmetry:
	- Wired TX and wired RX use the same packet/security contract.
	- AntSDR stage keeps encryption/decryption in SDR/FPGA path from day one.

## 3) 1 GbE Bandwidth Reality and Bring-Up Profiles

1 Gb Ethernet is approximately 125 MB/s raw line rate, with lower practical payload after protocol overhead.

For 1080p uncompressed RGB888:

Rate = width x height x 3 bytes x fps

- 1080p10: 62.2 MB/s (about 475 Mb/s)
- 1080p15: 93.3 MB/s (about 712 Mb/s)

Locked bring-up profiles:

- U10 (required pass): 1080p10 uncompressed
- U15 (target pass): 1080p15 uncompressed
- C60 (post-codec target): 1080p60 after hardware H.264

## 4) End-to-End Architecture

### Wired TX Path (Phase 3)

HDMI input (PYNQ) -> frame segmentation -> AES-GCM encrypt (FPGA path) -> UDP send -> UDP receive (PC) -> verify/decrypt -> frame reassembly -> display.

### Wired RX Path (Phase 4)

Video source (PC) -> frame segmentation -> AES-GCM encrypt -> UDP send -> UDP receive (PYNQ) -> verify/decrypt (FPGA path) -> frame reassembly -> HDMI output.

### SDR Path (Phase 7)

Ingress Ethernet framing -> SDR/FPGA AES-GCM -> modulation -> RF (2.4 GHz) -> demodulation -> SDR/FPGA verify/decrypt -> Ethernet egress.

## 5) Canonical Packet and Security Contract

All phases use one contract to avoid rework.

Packet fields:

- session_id
- stream_id
- frame_id
- segment_id
- segment_count
- source_timestamp_ns
- payload_type (RAW_RGB, RAW_YUV, H264)
- payload_length
- nonce_counter
- auth_tag

Security rules:

- Per-direction keys (TX->RX key separate from RX->TX key).
- Nonce uniqueness per key is strict and never reused.
- AAD includes immutable header fields.
- Replay window enforced at receiver.
- Invalid tag packets are dropped immediately and logged.

Transport behavior:

- MTU-safe segmentation.
- Per-frame reassembly with timeout.
- Late frame deadline drop.
- Continuous counters: loss, reorder depth, auth failures, dropped frames.

## 6) Full Execution Plan

### Phase 0: Requirement and Profile Freeze

Deliverables:

- This README as the source-of-truth plan.
- Frozen profile set: U10 required, U15 target, C60 post-H.264.
- Frozen latency and transport policy.

Exit criteria:

- No unresolved architecture ambiguity before implementation starts.

### Phase 1: Protocol and Security Specification

Deliverables:

- Versioned protocol document.
- Versioned crypto/session policy.
- Error handling policy for drop/reorder/decrypt failure.

Exit criteria:

- Both endpoint implementations can be built against one exact packet schema.

### Phase 2: PYNQ Crypto Baseline and Overlay Reproducibility

Deliverables:

- Reproducible bitstream build flow.
- Reproducible overlay load/runtime flow.
- Two overlays with same interface contract (TX-default, RX-default).

Exit criteria:

- Crypto correctness validated.
- Expected throughput envelope confirmed prior to video integration.

### Phase 3: Wired TX (PYNQ HDMI In -> PC Display)

Deliverables:

- PYNQ HDMI capture and segmentation runtime.
- AES-GCM encrypt and UDP sender on PYNQ.
- Receiver, verify/decrypt, reassembly, and display app on PC.

Exit criteria:

- Continuous run >= 30 minutes.
- Zero auth failures and zero nonce reuse.
- U10 stable first, then U15 tuning attempt.

### Phase 4: Wired RX (PC Source -> PYNQ HDMI Out)

Deliverables:

- PC sender with matching packet and security contract.
- PYNQ UDP receiver, verify/decrypt path, and HDMI output path.

Exit criteria:

- Continuous run >= 30 minutes.
- Bidirectional parity with the same protocol behavior and semantics.

### Phase 5: Hardening and Observability

Deliverables:

- Bounded jitter buffer.
- Deadline-based frame dropping policy.
- Sender pacing and queue backpressure policy.
- Structured telemetry and run logs.

Exit criteria:

- No sustained queue growth under target profile.
- p95 latency under 50 ms in tuned mode.
- Stable behavior under packet reorder/drop fault tests.

### Phase 6: Hardware H.264 Integration on Zynq

Deliverables:

- Hardware encode before encryption on TX path.
- Hardware decode after decryption on RX path.
- Payload type negotiation while keeping outer packet/crypto envelope unchanged.

Exit criteria:

- 1080p60 validation achieved only after this phase.
- Latency and quality metrics recorded against uncompressed baseline.

### Phase 7: AntSDR E310 Radio Link Integration

Deliverables:

- TX radio chain with FPGA-side AES-GCM.
- RX radio chain with FPGA-side verify/decrypt.
- Preserved session and packet contract from wired mode.

Exit criteria:

- Stable over-the-air encrypted link.
- Packet loss/resync behavior characterized.

### Phase 8: FHSS Final Milestone

Deliverables:

- Hop scheduler (hopset, dwell, guard intervals).
- Synchronization and re-synchronization behavior.
- Hop telemetry and desync recovery policy.

Exit criteria:

- Stable synchronized hopping.
- Recovery validated under forced desynchronization and interference tests.

## 7) Planned Repository Structure

Target structure to implement next:

OS-VideoSDR/
	README.md
	LICENSE
	docs/
		architecture.md
		protocol_spec.md
		crypto_policy.md
		latency_budget.md
		test_plan.md
	config/
		profiles.yaml
		network.yaml
		crypto.yaml
	protocol/
		packet_schema.py
		constants.py
		validation.py
	pynq/
		overlays/
			tx/
			rx/
		runtime/
			hdmi_capture.py
			hdmi_output.py
			udp_tx.py
			udp_rx.py
			aes_gcm_dma.py
			reassembly.py
			telemetry.py
	pc/
		runtime/
			source_capture.py
			sink_display.py
			udp_tx.py
			udp_rx.py
			aes_gcm_sw.py
			reassembly.py
			telemetry.py
	antsdr/
		tx_chain/
		rx_chain/
		fhss/
		integration/
	scripts/
		run_wired_tx.ps1
		run_wired_rx.ps1
		run_soak_test.ps1
		collect_metrics.ps1
	tests/
		unit/
		integration/
		soak/
	artifacts/
		logs/
		captures/
		metrics/

Purpose by top-level area:

- docs/: versioned source of truth for protocol, security, and testing.
- config/: tunable runtime settings by profile and environment.
- protocol/: shared packet format and validation used by both endpoints.
- pynq/: board-side runtime for capture/output, crypto integration, and transport.
- pc/: host-side runtime for source/sink and interoperability testing.
- antsdr/: SDR chain implementation and FHSS logic.
- scripts/: repeatable run and benchmark entry points.
- tests/: CI and lab verification suites.
- artifacts/: saved evidence from milestone runs.

## 8) Acceptance Gates (Summary)

The project moves forward only when each gate is passed:

1. Crypto gate: deterministic correctness and expected throughput envelope.
1. U10 gate: stable 1080p10 uncompressed wired link.
1. U15 gate: stable 1080p15 uncompressed wired link.
1. Bidirectional gate: both wired directions pass with one protocol contract.
1. Latency gate: p95 < 50 ms tuned mode.
1. C60 gate: 1080p60 after hardware H.264 integration.
1. SDR gate: stable non-hopping AntSDR encrypted link.
1. FHSS gate: synchronized hopping with robust resync.

## 9) Immediate Build Order

1. Create docs/, config/, protocol/, pynq/runtime/, pc/runtime/, and scripts/.
1. Write protocol_spec.md and crypto_policy.md before endpoint coding.
1. Implement wired TX path first and pass U10.
1. Implement wired RX path and pass bidirectional parity.
1. Add hardening and telemetry, then push U15.
1. Integrate hardware H.264 and pursue C60.
1. Port middle link to AntSDR and then add FHSS.

## 10) PL-First PYNQ Continuation Package

To switch to a PL-first architecture, start here first:

1. docs/pynq_pl_first_architecture.md
1. docs/next_machine_handoff.md

What these documents contain:

- Board-level feasibility and hardware constraints for PYNQ-Z2 Ethernet routing.
- Exact PL vs PS responsibility split for a PL-first data plane.
- Proposed AXI interfaces, descriptor ring contract, and control register map.
- Migration plan from Python bring-up harnesses to a minimal PS networking shim.
- Copy, setup, validate, and continue checklist for a new development machine.

Current architecture decision:

- Keep the streaming and crypto datapath in PL.
- Use PS as a thin networking and control shim on PYNQ-Z2 onboard Ethernet.

## 11) Current Status of pynq/runtime Code

Important clarification for the current codebase:

- [pynq/runtime/tx_main.py](pynq/runtime/tx_main.py) and [pynq/runtime/rx_main.py](pynq/runtime/rx_main.py) are PS-side bring-up tools.
- [pynq/runtime/tx_main.py](pynq/runtime/tx_main.py) currently generates synthetic frames in software and sends packetized UDP traffic.
- [pynq/runtime/rx_main.py](pynq/runtime/rx_main.py) receives UDP packets in software, validates/reassembles them, and reports telemetry.
- These scripts can run over localhost or over a real network interface, but the processing path is still PS/software.
- They are for protocol and integration validation only.
- They are not the final PL-first high-performance data path.

Planned production direction:

- Move packetization, crypto data path, and frame handling into PL.
- Keep PS as a minimal networking and control shim on PYNQ-Z2.

### Session Update (2026-05-10)

- Protocol bring-up on PYNQ PS was validated for synthetic loopback traffic.
- Runtime bring-up tooling was improved for stable troubleshooting:
	- [pynq/runtime/rx_main.py](pynq/runtime/rx_main.py): added max runtime and max idle exits, plus average and instant throughput metrics.
	- [pynq/runtime/tx_main.py](pynq/runtime/tx_main.py): added inter-packet pacing (`--inter-packet-gap-us`) and config pacing integration.
- Stable AES-GCM software-path run result:
	- 300 frames, `--fps 15`, `--synthetic-frame-bytes 72000`, `packets_tx=18000`, `packets_rx=18000`, `drops=0`, `decrypt_fail=0`.
	- This is valid protocol/contract evidence, not full profile evidence.
- Full-frame 1080p raw smoke test (`6220800` bytes per frame) on PS software path did not pass:
	- Kernel `packet receive errors` and `receive buffer errors` increased during run.
	- RX packet count lagged TX packet count by a large margin.
	- This confirms PS Python runtime is for protocol validation only and is not sufficient for U15 acceptance.
- Current gate interpretation:
	- U10 acceptance gate: not passed yet in production architecture.
	- U15 acceptance gate: not passed.
	- Next step focus: DMA-backed crypto integration, then PS C shim and PL-first data path.
