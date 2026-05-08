"""Lightweight telemetry counters for runtime visibility."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Dict


@dataclass(slots=True)
class TelemetryCounters:
    packets_rx: int = 0
    packets_tx: int = 0
    packets_dropped: int = 0
    decrypt_failures: int = 0
    frames_completed: int = 0
    frames_dropped_late: int = 0
    reorder_events: int = 0

    def snapshot(self) -> Dict[str, int]:
        return asdict(self)
