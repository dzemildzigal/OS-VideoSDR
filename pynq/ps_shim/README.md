# PS Shim (C)

This directory hosts the PYNQ PS-side C shim for high-rate transport bring-up.

Current goal:

- replace Python packet loop overhead with a minimal C baseline,
- keep protocol semantics stable while preserving socket A/B comparison,
- establish descriptor-ring transport plumbing for PL integration.

## Current Components

- `src/main.c`: runnable tx/rx baseline with selectable `socket` or `ring` transport backend.
- `include/ring_api.h`: descriptor-ring API contract used for upcoming PL integration.
- `src/ring_backend.c`: mmap-backed userspace ring implementation with descriptor ownership transitions.
- `src/ring_stub.c`: legacy ENOSYS stub retained for reference.

## Build (On PYNQ Linux)

From repository root:

```bash
chmod +x pynq/ps_shim/build.sh
./pynq/ps_shim/build.sh
```

Binary output:

- `pynq/ps_shim/build/ps_shim`

## Quick Loopback Smoke (Socket Backend)

Terminal A:

```bash
./pynq/ps_shim/build/ps_shim --mode rx --bind-ip 127.0.0.1 --port 5000 --max-runtime-s 20 --frame-bytes 120000 --segment-bytes 1200
```

Terminal B:

```bash
./pynq/ps_shim/build/ps_shim --mode tx --target-ip 127.0.0.1 --port 5000 --frames 120 --fps 15 --frame-bytes 120000 --segment-bytes 1200 --inter-packet-gap-us 100
```

## Quick Loopback Smoke (Ring Backend)

Ring backend default path is `/dev/shm/osv_ring.bin` and can be overridden with `--ring-dev-path`.

Terminal A:

```bash
./pynq/ps_shim/build/ps_shim --mode rx --transport-backend ring --ring-dev-path /dev/shm/osv_ring.bin --max-runtime-s 20 --frame-bytes 120000 --segment-bytes 1200 --ring-timeout-ms 500
```

Terminal B:

```bash
./pynq/ps_shim/build/ps_shim --mode tx --transport-backend ring --ring-dev-path /dev/shm/osv_ring.bin --frames 120 --fps 15 --frame-bytes 120000 --segment-bytes 1200 --inter-packet-gap-us 100 --ring-timeout-ms 500
```

## Ring Sizing Knobs

Optional environment overrides:

- `OSV_RING_SLOT_COUNT` (default `2048`)
- `OSV_RING_SLOT_PAYLOAD_BYTES` (default `2048`)
- `OSV_RING_TIMEOUT_MS` (default `250`)
- `OSV_RING_UIO_MAP_INDEX` (default `0`, used when `--ring-dev-path` is `/dev/uioX`)
- `OSV_RING_UIO_RING_OFFSET` (default `0`, byte offset inside mapped UIO map)
- `OSV_RING_DEBUG` (default `0`, set `1` to print ring open diagnostics)

Example:

```bash
export OSV_RING_SLOT_COUNT=4096
export OSV_RING_SLOT_PAYLOAD_BYTES=4096
```

UIO example:

```bash
export OSV_RING_UIO_MAP_INDEX=0
export OSV_RING_UIO_RING_OFFSET=0
./pynq/ps_shim/build/ps_shim --mode rx --transport-backend ring --ring-dev-path /dev/uio1 --max-runtime-s 20 --frame-bytes 120000 --segment-bytes 1200
```

## Interrupt Expectations

- The current mmap ring prototype is memory polling based.
- No ring interrupt activity is expected unless a board-native UIO/device backend with IRQ wiring is integrated.

## Next Integration Step

Replace the mmap ring backend with the board-native `/dev` or UIO backend on target image and wire descriptor ownership transitions to the PL producer/consumer interface.
