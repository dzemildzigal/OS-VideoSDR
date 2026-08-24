from __future__ import annotations

import pytest

from pc.runtime.main_rx import strip_transport_padding
from protocol.constants import AUTHENTICATED_BODY_BYTES, TRANSPORT_SLOT_BYTES


def test_b3_padding_is_removed_after_validation() -> None:
    body = bytes(range(256)) * 4 + bytes(range(216))
    body = body[:AUTHENTICATED_BODY_BYTES]
    slot = body + bytes(TRANSPORT_SLOT_BYTES - AUTHENTICATED_BODY_BYTES)

    assert len(body) == AUTHENTICATED_BODY_BYTES
    assert len(slot) == TRANSPORT_SLOT_BYTES
    assert strip_transport_padding(slot) == body


def test_b3_nonzero_padding_is_rejected() -> None:
    slot = bytearray(TRANSPORT_SLOT_BYTES)
    slot[AUTHENTICATED_BODY_BYTES] = 1

    with pytest.raises(ValueError, match="padding is not zero"):
        strip_transport_padding(bytes(slot))


def test_b3_wrong_segment_size_is_rejected() -> None:
    with pytest.raises(ValueError, match="segment size mismatch"):
        strip_transport_padding(bytes(AUTHENTICATED_BODY_BYTES))
