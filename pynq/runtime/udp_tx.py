"""UDP sender utility for low-latency datagram transmission."""

from __future__ import annotations

import socket
from typing import Optional


class UdpTx:
    def __init__(
        self,
        target_ip: str,
        target_port: int,
        bind_ip: str = "0.0.0.0",
        send_buffer_bytes: Optional[int] = None,
    ) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind((bind_ip, 0))
        if send_buffer_bytes is not None:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, send_buffer_bytes)
        self._target = (target_ip, target_port)

    def send(self, datagram: bytes) -> int:
        return self._sock.sendto(datagram, self._target)

    def close(self) -> None:
        self._sock.close()
