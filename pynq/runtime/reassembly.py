"""Frame reassembly helper for segmented datagrams."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from protocol.packet_schema import PacketHeader
from protocol.validation import is_frame_complete


@dataclass(slots=True)
class _FrameState:
    segment_count: int
    segments: Dict[int, bytes] = field(default_factory=dict)


class FrameReassembler:
    def __init__(self, max_active_frames: int = 8) -> None:
        self.max_active_frames = max_active_frames
        self._frames: Dict[Tuple[int, int, int], _FrameState] = {}

    def push(self, header: PacketHeader, payload: bytes) -> Optional[bytes]:
        key = (header.session_id, header.stream_id, header.frame_id)
        state = self._frames.get(key)

        if state is None or state.segment_count != header.segment_count:
            state = _FrameState(segment_count=header.segment_count)
            self._frames[key] = state

        if header.segment_id not in state.segments:
            state.segments[header.segment_id] = payload

        if is_frame_complete(state.segments.keys(), state.segment_count):
            out = b"".join(state.segments[idx] for idx in range(state.segment_count))
            del self._frames[key]
            self._evict_if_needed()
            return out

        self._evict_if_needed()
        return None

    def drop_frame(self, session_id: int, stream_id: int, frame_id: int) -> None:
        self._frames.pop((session_id, stream_id, frame_id), None)

    def _evict_if_needed(self) -> None:
        while len(self._frames) > self.max_active_frames:
            oldest_key = next(iter(self._frames))
            del self._frames[oldest_key]
