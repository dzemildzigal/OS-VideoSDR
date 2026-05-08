# Crypto Policy

## Baseline

- Algorithm: AES-256-GCM
- Tag length: 16 bytes default
- Directional keys: TX->RX and RX->TX are different keys

## Nonce and Replay

- Nonce counter is strictly monotonic per direction and key.
- Nonce reuse is forbidden.
- Receiver enforces replay window and rejects stale counters.
- Session rollover resets replay state and derives fresh key context.

## Associated Data (AAD)

AAD includes immutable routing and framing metadata:

- magic
- version
- session_id
- stream_id
- frame_id
- segment_id
- segment_count
- payload_type
- key_id
- payload_length
- nonce_counter

## Key Handling

- Key material is never logged.
- Key identifiers may be logged.
- Rekey interval is configurable.
- Decryption attempts with unknown key_id are dropped.

## Failure Policy

- Invalid tag: drop packet, increment counter.
- Replay violation: drop packet, increment counter.
- Nonce gap beyond configured tolerance: drop packet and log event.
