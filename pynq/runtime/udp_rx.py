"""UDP receiver utility for low-latency datagram ingestion."""

from __future__ import annotations

import socket
from typing import Optional, Tuple


class UdpRx:
    def __init__(
        self,
        listen_port: int,
        bind_ip: str = "0.0.0.0",
        recv_buffer_bytes: Optional[int] = None,
    ) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind((bind_ip, listen_port))
        if recv_buffer_bytes is not None:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, recv_buffer_bytes)

    def recv(self, max_datagram_bytes: int = 2048) -> Tuple[bytes, tuple]:
        return self._sock.recvfrom(max_datagram_bytes)

    def close(self) -> None:
        self._sock.close()
