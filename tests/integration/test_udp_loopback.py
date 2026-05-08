from __future__ import annotations

import socket
import time

from pc.runtime.reassembly import FrameReassembler
from pc.runtime.udp_rx import UdpRx
from pc.runtime.udp_tx import UdpTx
from protocol.constants import DEFAULT_TAG_LENGTH, PAYLOAD_TYPE_RAW_RGB
from protocol.packet_schema import PacketHeader, build_datagram, split_datagram
from protocol.validation import validate_header


def _free_udp_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _segment(data: bytes, chunk_size: int) -> list[bytes]:
    return [data[idx : idx + chunk_size] for idx in range(0, len(data), chunk_size)]


def test_udp_loopback_reassembly_and_validation() -> None:
    listen_port = _free_udp_port()
    receiver = UdpRx(listen_port=listen_port, bind_ip="127.0.0.1", timeout_s=1.0)
    sender = UdpTx(target_ip="127.0.0.1", target_port=listen_port, bind_ip="127.0.0.1")

    frame = bytes([idx % 256 for idx in range(3600)])
    segments = _segment(frame, 1200)

    send_order = [1, 0, 2]
    if len(segments) > 3:
        send_order.extend(range(3, len(segments)))

    session_id = 99
    stream_id = 1
    frame_id = 123

    try:
        for segment_id in send_order:
            payload = segments[segment_id]
            header = PacketHeader(
                session_id=session_id,
                stream_id=stream_id,
                frame_id=frame_id,
                segment_id=segment_id,
                segment_count=len(segments),
                source_timestamp_ns=time.time_ns(),
                payload_type=PAYLOAD_TYPE_RAW_RGB,
                key_id=1,
                payload_length=len(payload),
                nonce_counter=1000 + segment_id,
                tag_length=DEFAULT_TAG_LENGTH,
            )
            datagram = build_datagram(header, payload, b"\x00" * DEFAULT_TAG_LENGTH)
            sender.send(datagram)

        reassembler = FrameReassembler(max_active_frames=4)
        rebuilt = None

        for _ in range(len(segments)):
            datagram, _peer = receiver.recv(max_datagram_bytes=2048)
            header, payload, tag = split_datagram(datagram)

            assert tag == b"\x00" * DEFAULT_TAG_LENGTH
            assert validate_header(header) == []

            maybe_frame = reassembler.push(header, payload)
            if maybe_frame is not None:
                rebuilt = maybe_frame

        assert rebuilt == frame
    finally:
        sender.close()
        receiver.close()
