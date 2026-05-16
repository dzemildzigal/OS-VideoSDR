# PL Ring UIO Hardware Specification (Kickoff)

## Purpose

Define the first hardware cut for board-native descriptor rings on PYNQ with:

- dedicated PL-managed DDR apertures,
- UIO exposure to userspace,
- interrupt signaling,
- no ABI changes to current PS ring software.

This spec is aligned with the current PS shim ring backend in:

- [pynq/ps_shim/src/ring_backend.c](pynq/ps_shim/src/ring_backend.c)
- [pynq/ps_shim/include/ring_api.h](pynq/ps_shim/include/ring_api.h)

## Software Ring Contract (Must Match)

Ring memory bytes are computed as:

required_bytes = sizeof(RingHeader) + slot_count * (sizeof(RingSlot) + slot_payload_bytes)

Current software layout implies:

- sizeof(RingHeader) = 32 bytes
- sizeof(RingSlot) = 64 bytes

Therefore:

required_bytes = 32 + slot_count * (64 + slot_payload_bytes)

For the active target profile:

- slot_count = 8192
- slot_payload_bytes = 4096
- required_bytes = 34078752 bytes (~32.5 MiB)

## First-Cut Hardware Topology

Because directionality is bidirectional in first cut, expose separate TX and RX ring maps.

Recommended UIO map layout:

1. map0: control registers (64 KiB)
2. map1: TX ring memory aperture (64 MiB, DDR-backed)
3. map2: RX ring memory aperture (64 MiB, DDR-backed)

Why this split:

- one 64 MiB ring map comfortably fits the 34078752-byte profile with headroom,
- bidirectional operation needs two independent rings,
- control aperture remains isolated from data aperture.

If platform tooling requires only one data map, use one 128 MiB data map and assign fixed offsets per direction.

## Ownership and State Machine

Slot state values are fixed and must remain:

- SLOT_EMPTY = 0
- SLOT_FULL = 1

PS -> PL (TX ring, map1):

1. PS writes payload
2. PS writes descriptor metadata
3. PS sets slot state FULL
4. PS advances write_index
5. PL consumes and returns slot to EMPTY
6. PL advances read_index

PL -> PS (RX ring, map2):

1. PL writes payload
2. PL writes descriptor metadata
3. PL sets slot state FULL
4. PL advances write_index
5. PS consumes and returns slot to EMPTY
6. PS advances read_index

Do not change descriptor field order or size in first cut.

## AXI-Lite Register Map (map0)

All offsets are from control map base.

| Offset | Name | Access | Description |
|---|---|---|---|
| 0x0000 | VERSION | RO | IP version (major.minor encoded) |
| 0x0004 | CAPABILITIES | RO | Bitfield for IRQ, bidirectional, map count |
| 0x0008 | CONTROL | RW | bit0 enable, bit1 soft_reset |
| 0x000C | STATUS | RO | bit0 tx_active, bit1 rx_active, bit2 fault |
| 0x0010 | IRQ_ENABLE | RW | bit mask for enabled events |
| 0x0014 | IRQ_STATUS | RW1C | pending interrupt bits |
| 0x0018 | TX_DOORBELL | WO | PS notify: new TX descriptors available |
| 0x001C | RX_DOORBELL | WO | PS notify/ack for RX flow control |
| 0x0020 | TX_CONSUMED_INDEX | RO | latest PL-consumed index |
| 0x0024 | RX_PRODUCED_INDEX | RO | latest PL-produced index |
| 0x0028 | TX_ERROR_COUNT | RO | TX path errors |
| 0x002C | RX_ERROR_COUNT | RO | RX path errors |
| 0x0030 | IRQ_COUNT | RO | aggregate IRQ count |
| 0x0034 | LAST_FAULT_CODE | RO | implementation-defined fault code |

IRQ_STATUS bit suggestion:

- bit0: TX consumed update
- bit1: RX produced update
- bit2: TX fault
- bit3: RX fault

## Interrupt Requirements

- Wire one PL interrupt to PS GIC for ring events.
- Implement IRQ status + clear (RW1C).
- Support event coalescing to reduce IRQ storm under high FPS.

Userspace UIO behavior expectation:

1. wait on /dev/uioX read/poll
2. read IRQ_STATUS from map0
3. clear handled bits via RW1C write
4. re-enable UIO interrupt by writing 1 to /dev/uioX if required by kernel driver

## Device Tree / UIO Exposure

Expose one UIO node with three maps (ctrl + tx ring + rx ring) and one interrupt.

Template is provided in:

- [docs/templates/ring_uio_template.dtsi](docs/templates/ring_uio_template.dtsi)

## Bring-Up Command Pattern

Assume /dev/uioX is the new ring device.

Terminal A (RX app consumes PL->PS ring from map2):

- export OSV_RING_UIO_MAP_INDEX=2
- export OSV_RING_UIO_RING_OFFSET=0
- export OSV_RING_SLOT_COUNT=8192
- export OSV_RING_SLOT_PAYLOAD_BYTES=4096
- ./pynq/ps_shim/build/ps_shim --mode rx --transport-backend ring --ring-dev-path /dev/uioX --max-runtime-s 22 --frame-bytes 120000 --segment-bytes 1200 --ring-timeout-ms 1000

Terminal B (TX app produces PS->PL ring to map1):

- export OSV_RING_UIO_MAP_INDEX=1
- export OSV_RING_UIO_RING_OFFSET=0
- export OSV_RING_SLOT_COUNT=8192
- export OSV_RING_SLOT_PAYLOAD_BYTES=4096
- ./pynq/ps_shim/build/ps_shim --mode tx --transport-backend ring --ring-dev-path /dev/uioX --max-runtime-s 20 --fps 500 --frame-bytes 120000 --segment-bytes 1200 --inter-packet-gap-us 0 --ring-timeout-ms 1000

First initialization note:

- only on dedicated ring memory, set OSV_RING_UIO_ALLOW_RESET=1 once to initialize header when needed,
- then unset and run with default safety guards.

## Hardware Deliverables

1. Bitstream and hardware handoff data with fixed base addresses for map0/map1/map2.
2. Device tree fragment (or overlay) binding the node to UIO.
3. Address map summary including interrupt line and trigger type.
4. Validation logs showing map sizes and successful ring_open on map1/map2.

## Acceptance Checklist

1. /sys/class/uio/uioX/maps/map0/size is 0x00010000 (or agreed control size).
2. /sys/class/uio/uioX/maps/map1/size >= 34078752.
3. /sys/class/uio/uioX/maps/map2/size >= 34078752.
4. ring_open succeeds with OSV_RING_UIO_ALLOW_CLAMP unset.
5. ring_open succeeds with OSV_RING_UIO_ALLOW_RESET unset after initialization.
6. TX and RX stress run completes with stable counters and no descriptor corruption.
