# Protocol Specification

## Version

- Protocol name: OSV
- Version: 0
- Endianness: network byte order (big-endian)

## Datagram Layout

Each UDP datagram carries one segment:

1. Fixed-size header
2. Segment payload
3. Authentication tag

### Fixed Header Fields

| Field | Size (bytes) | Description |
|---|---:|---|
| magic | 2 | Constant marker for packet recognition |
| version | 1 | Protocol version |
| flags | 1 | Future options |
| session_id | 4 | Session scope for replay/nonces |
| stream_id | 2 | Logical stream index |
| frame_id | 4 | Video frame sequence |
| segment_id | 2 | Segment index within frame |
| segment_count | 2 | Number of segments for frame |
| source_timestamp_ns | 8 | Sender capture or emit timestamp |
| payload_type | 1 | RAW_RGB, RAW_YUV, H264 |
| key_id | 1 | Active key selector |
| payload_length | 2 | Bytes in payload |
| nonce_counter | 8 | Monotonic counter per key direction |
| tag_length | 1 | Authentication tag bytes |
| reserved | 1 | Reserved for future use |

Header length is fixed and implementation-defined in protocol module constants.

## Reassembly Rules

- Frame is complete when all segment_id values in [0, segment_count-1] are present.
- Duplicate segments are ignored.
- Out-of-window frames are dropped.
- Reassembly timeout drops incomplete frames.

## Transport Behavior

- MTU-safe payload sizing.
- Deadline-based late frame drop.
- No retransmission in early phases.
- Continuous telemetry for loss and reorder depth.

## Compatibility Rules

- Header version mismatch results in immediate drop.
- Unknown payload_type is dropped and counted.
- Tag verification failure is dropped and counted.
