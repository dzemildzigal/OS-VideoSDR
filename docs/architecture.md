# Architecture

## Scope

OS-VideoSDR is built in three milestones:

1. Wired encrypted video over 1 Gb Ethernet in both directions.
2. Replace Ethernet middle link with AntSDR E310 radio link.
3. Add FHSS after non-hopping radio is stable.

## Design Principles

- One packet contract across all phases.
- One crypto contract across all phases.
- UDP transport with deterministic drop policy.
- Latency-first behavior with bounded buffering.
- Validate each gate before scaling complexity.

## Data Paths

### Wired TX Path

PYNQ HDMI input -> frame segmentation -> AES-256-GCM encrypt -> UDP send -> PC receive -> verify/decrypt -> reassembly -> display.

### Wired RX Path

PC video source -> frame segmentation -> AES-256-GCM encrypt -> UDP send -> PYNQ receive -> verify/decrypt -> reassembly -> HDMI output.

### SDR Path

Ethernet ingress -> SDR/FPGA AES-256-GCM -> modulation -> RF (2.4 GHz) -> demodulation -> SDR/FPGA verify/decrypt -> Ethernet egress.

## Profile Strategy

- U10: 1080p10 uncompressed (required first pass).
- U15: 1080p15 uncompressed (next target).
- C60: 1080p60 after hardware H.264 path is stable.

## Acceptance Sequence

1. Crypto correctness and throughput envelope.
2. U10 stable in wired TX and wired RX.
3. U15 stable in wired TX and wired RX.
4. p95 latency under 50 ms in tuned mode.
5. H.264 integration and C60 validation.
6. AntSDR encrypted non-hopping link.
7. FHSS synchronized and resilient.
