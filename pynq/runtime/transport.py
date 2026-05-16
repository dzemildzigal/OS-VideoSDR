"""Minimal transport abstraction for the unified runtime entrypoint."""

from __future__ import annotations

import socket
from typing import Tuple


class UdpTransport:
    def __init__(self, bind_ip: str, bind_port: int, send_ip: str, send_port: int) -> None:
        self._bind = (bind_ip, bind_port)
        self._send = (send_ip, send_port)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(self._bind)

    def send(self, payload: bytes) -> int:
        return self._sock.sendto(payload, self._send)

    def recv(self, max_bytes: int = 65535) -> Tuple[bytes, Tuple[str, int]]:
        return self._sock.recvfrom(max_bytes)

    def close(self) -> None:
        self._sock.close()
