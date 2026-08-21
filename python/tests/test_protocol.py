"""TagStream encoding and the discovery poll packet."""

from __future__ import annotations

import pytest

from libkp.errors import FieldOverrunError, TooShortError
from libkp.protocol import PORT, TagStream, build_poll_request, push_field


def test_poll_request_is_exactly_34_bytes():
    built = build_poll_request("00:00:00:00:00:00")
    assert built == (b"DSCV\x16MAC#00:00:00:00:00:00\x07POLL:)\x00")
    assert len(built) == 34


def test_poll_request_carries_the_supplied_mac():
    built = build_poll_request("D4:4F:80:00:9E:52")
    assert b"MAC#D4:4F:80:00:9E:52" in built
    assert len(built) == 34


def test_round_trip_parse_of_our_own_poll():
    stream = TagStream.parse(build_poll_request())
    assert stream.header == b"DSCV"
    assert stream.fields == [b"MAC#00:00:00:00:00:00", b"POLL:)"]
    assert stream.get("POLL") == b":)"


def test_key_values_skips_unprintable_fields():
    payload = bytearray(b"DSCV")
    push_field(payload, b"NAMEProfiler")
    push_field(payload, b"\x00\x01\x02\x03rest")
    push_field(payload, b"SER#12345")
    payload.append(0x00)
    stream = TagStream.parse(bytes(payload))
    assert stream.key_values() == [("NAME", b"Profiler"), ("SER#", b"12345")]
    assert stream.get("MISS") is None


def test_headerless_payload_parses_from_offset_zero():
    payload = bytearray()
    push_field(payload, b"NAMEx")
    payload.append(0x00)
    stream = TagStream.parse(bytes(payload))
    assert stream.header is None
    assert stream.fields == [b"NAMEx"]


def test_empty_payload_is_rejected():
    with pytest.raises(TooShortError):
        TagStream.parse(b"")


def test_truncated_field_is_rejected():
    with pytest.raises(FieldOverrunError):
        TagStream.parse(b"\x40abc")


def test_port_is_the_shared_udp_tcp_port():
    assert PORT == 5727
