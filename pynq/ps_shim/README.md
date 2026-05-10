# PS Shim (C)

This directory hosts the PYNQ PS-side C shim for high-rate transport bring-up.

Current goal:

- replace Python packet loop overhead with a minimal C UDP baseline,
- keep protocol semantics stable,
- prepare clean handoff into PL descriptor-ring integration.

## Current Components

- `src/main.c`: runnable UDP tx/rx baseline with pacing and throughput telemetry.
- `include/ring_api.h`: descriptor-ring API contract used for upcoming PL integration.
- `src/ring_stub.c`: ENOSYS stubs so the interface is compiled and ready for replacement.

## Build (On PYNQ Linux)

From repository root:

```bash
chmod +x pynq/ps_shim/build.sh
./pynq/ps_shim/build.sh
```

Binary output:

- `pynq/ps_shim/build/ps_shim`

## Quick Loopback Smoke

Terminal A:

```bash
./pynq/ps_shim/build/ps_shim --mode rx --bind-ip 127.0.0.1 --port 5000 --max-runtime-s 20 --frame-bytes 120000 --segment-bytes 1200
```

Terminal B:

```bash
./pynq/ps_shim/build/ps_shim --mode tx --target-ip 127.0.0.1 --port 5000 --frames 120 --fps 15 --frame-bytes 120000 --segment-bytes 1200 --inter-packet-gap-us 100
```

## Next Integration Step

Swap `ring_stub.c` with a real `/dev` or UIO-backed ring implementation and route TX/RX data movement through descriptor ownership transitions instead of direct socket-to-buffer loops.
