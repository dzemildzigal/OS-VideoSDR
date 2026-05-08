# Latency Budget

## Target

- End-to-end p95 latency under 50 ms in tuned mode.

## Measurement Points

- t0: capture timestamp at sender.
- t1: encryption complete.
- t2: packet receive complete at receiver.
- t3: decryption complete.
- t4: render or HDMI output enqueue.

Derived metrics:

- capture_to_encrypt = t1 - t0
- network_path = t2 - t1
- decrypt_to_output = t4 - t3
- end_to_end = t4 - t0

## Budget Guidance (Initial)

| Stage | Budget (ms) |
|---|---:|
| capture and segment | 8 |
| encrypt and send | 8 |
| network and queue | 12 |
| receive and decrypt | 10 |
| reassembly and output | 12 |
| total | 50 |

## Tuning Priorities

1. Enforce sender pacing and avoid burst queues.
2. Use bounded jitter buffer.
3. Apply strict late frame drop deadline.
4. Keep packet payload near optimal MTU-safe size.
