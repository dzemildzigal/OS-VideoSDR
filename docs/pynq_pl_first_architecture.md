# PYNQ-Z2 PL-First Architecture Plan

## Goal

Push the streaming data path to PL as much as possible while keeping the onboard PYNQ-Z2 Ethernet port usable.

## Hardware Reality (Critical Constraint)

- On PYNQ-Z2, the onboard 1 Gb Ethernet PHY is connected to the Zynq PS GEM interface.
- Because of this board wiring, direct PL-only packet transmission to the onboard RJ45 is not available as a practical path.
- Result: a true onboard RJ45 PL-only design is not feasible on this board without hardware changes.

This means the best practical design is:

- PL handles media path and crypto path.
- PS is reduced to a thin networking shim and control plane.

## Feasibility Summary

### Path A: Practical on PYNQ-Z2 (Recommended)

- PL-first datapath with minimal PS networking bridge.
- Feasible now.
- Aligns with low-latency objective.

### Path B: True PL-to-wire Ethernet

- Requires external PHY/MAC path connected to PL I/O.
- Not available through default onboard RJ45 routing.
- Feasible only with extra hardware or a different board architecture.

## Target Split: What Runs Where

### PL Responsibilities (Data Plane)

- HDMI capture ingress pipeline.
- Frame slicing and MTU-safe segmenting.
- Packet metadata generation (frame, segment, nonce counter inputs).
- AES-256-GCM encrypt and decrypt engines.
- Reassembly assist for RX path.
- Optional bounded jitter FIFO in hardware.
- DMA streaming to and from DDR buffers.

### PS Responsibilities (Thin Shim + Control)

- Ethernet GEM driver and UDP socket or raw lwIP endpoint.
- Descriptor loop between GEM buffers and DDR rings shared with PL.
- Session setup, key load commands, health telemetry export.
- No Python in production data path.

## Reference Datapaths

### TX (PYNQ HDMI In to Ethernet)

1. HDMI Rx in PL produces pixel stream.
2. PL packetizer builds payload segments and metadata.
3. PL AES-GCM encrypts payload with AAD from header fields.
4. PL writes encrypted segments into DDR ring via AXI DMA.
5. PS networking shim pulls descriptors and pushes UDP packets through GEM.

### RX (Ethernet to PYNQ HDMI Out)

1. PS networking shim receives UDP packets into DDR ring.
2. PL reads encrypted segments from DDR via AXI DMA.
3. PL verifies and decrypts AES-GCM.
4. PL reassembles frames and drives HDMI Tx.

## PL Block Diagram (Logical)

- HDMI_RX_PIPE
- FRAME_PACKETIZER
- NONCE_MANAGER
- AES_GCM_CORE_TXRX
- RX_REASSEMBLER
- STREAM_FIFO_AND_PACER
- AXI_DMA_BRIDGE
- STATS_COUNTERS
- CONTROL_REG_BANK

## Interface Contract

### AXI4-Stream Payload Channel

- tdata: payload bytes
- tvalid, tready, tlast
- tkeep for partial final beat
- tuser carries metadata index or compact flags

### Descriptor Ring in DDR

Each descriptor is fixed-size and owned by either PL or PS.

Suggested fields:

- buffer_addr
- payload_len
- session_id
- stream_id
- frame_id
- segment_id
- segment_count
- nonce_counter
- key_id
- flags (valid, last_segment, drop_hint)
- timestamp_ns

### Control Registers (AXI-Lite)

Suggested top-level map:

- 0x0000 VERSION
- 0x0004 CONTROL
- 0x0008 STATUS
- 0x0010 SESSION_ID
- 0x0014 STREAM_ID
- 0x0018 KEY_ID_TX
- 0x001C KEY_ID_RX
- 0x0020 NONCE_BASE_TX
- 0x0028 NONCE_BASE_RX
- 0x0030 RING_BASE_TX
- 0x0038 RING_BASE_RX
- 0x0040 RING_SIZE
- 0x0044 IRQ_ENABLE
- 0x0048 IRQ_STATUS
- 0x0050 DROPPED_PKT_COUNT
- 0x0058 AUTH_FAIL_COUNT
- 0x0060 LATE_FRAME_DROP_COUNT
- 0x0068 REORDER_EVENT_COUNT

## Performance Notes

- 1080p10 RGB888 is about 62.2 MB/s payload.
- 1080p15 RGB888 is about 93.3 MB/s payload.
- 1 GbE practical payload envelope is lower than raw line rate due to headers and stack overhead.
- AES throughput is not the bottleneck with the existing PL core.
- Main bottlenecks are software networking overhead and memory copy behavior.

## Software Strategy for PS Shim

### Bring-up Stage

- Existing Python entrypoints are valid for protocol and logic verification only.
- Use them as functional reference, not production throughput path.

### Production Stage

- Replace Python data path with C or bare-metal lwIP raw path.
- Keep PS loop minimal: descriptor consume, GEM submit, completion recycle.
- Keep control path separate from data path.

## Migration Plan (From Current State)

1. Freeze packet contract currently in docs and protocol module.
2. Implement PL packetizer and descriptor writer for TX.
3. Implement PS C shim for TX descriptor-to-GEM forwarding.
4. Implement RX GEM-to-descriptor writer on PS side.
5. Implement PL decrypt and reassembly for RX.
6. Add hardware counters and interrupt-driven health reporting.
7. Run U10 and U15 gates with PS C shim.
8. Move to hardware H.264 phase after stable encrypted raw path.

## Risks and Mitigations

- Risk: PS networking still too heavy under Linux.
  - Mitigation: move to bare-metal/lwIP path for wire-speed consistency.
- Risk: DDR copy overhead increases latency.
  - Mitigation: ring buffers, aligned bursts, avoid extra copies.
- Risk: nonce management bugs under reorder.
  - Mitigation: strict monotonic policy and replay window in hardware checks.

## Decision

For PYNQ-Z2 onboard Ethernet, use a PL-first architecture with a minimal PS networking shim. Do not plan for fully onboard PL-only RJ45 transmission on this board as a baseline assumption.
