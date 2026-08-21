"""Kemper NRPN-over-SysEx builders and parsers."""

from __future__ import annotations

import pytest

from libkp import nrpn
from libkp.nrpn import (
    DEVICE_OMNI,
    FUNCTION_RENDERED_STRING_REPLY,
    PAGE_STRINGS,
    PRODUCT_PLAYER,
    STRING_RIG_NAME,
    NrpnHeader,
)


def test_init_beacon_bytes():
    # init=1, sysex=1, tuner=1 -> flags 0x23; lease 30 s -> 15 (0x0F); set 0x02.
    assert nrpn.beacon(True, True, 30, 0x02, PRODUCT_PLAYER) == bytes.fromhex(
        "f0002033027f7e004002230ff7"
    )


def test_beacon_flags_bits():
    assert nrpn.beacon_flags(False, False) == 0x02
    assert nrpn.beacon_flags(True, False) == 0x03
    assert nrpn.beacon_flags(False, True) == 0x22
    assert nrpn.beacon_flags(True, True) == 0x23


def test_builds_rig_name_request():
    message = nrpn.request_string(0x00, 0x7F, PAGE_STRINGS, STRING_RIG_NAME)
    assert message == bytes.fromhex("f0002033007f43000001f7")
    header, values = NrpnHeader.parse(message)
    assert (header.function, header.page, header.number) == (0x43, 0x00, 0x01)
    assert values == b""


def test_builds_effect_state_set():
    on = nrpn.set_single(0x00, 0x7F, 0x3D, 0x03, 1)
    assert on == bytes.fromhex("f0002033007f01003d030001f7")
    off = nrpn.set_single(0x00, 0x7F, 0x3D, 0x03, 0)
    assert off[10:12] == bytes([0x00, 0x00])


@pytest.mark.parametrize("value", [0, 1, 6925, 8192, 16383])
def test_u14_round_trip(value):
    msb, lsb = nrpn.u14_split(value)
    assert nrpn.u14(msb, lsb) == value


def test_u14_masks_to_seven_bits():
    assert nrpn.u14(0xFF, 0xFF) == 16383


def test_control_change_bytes():
    assert nrpn.control_change(0, 50, 1) == bytes([0xB0, 50, 1])
    assert nrpn.control_change(15, 47, 0) == bytes([0xBF, 47, 0])


def test_program_change_bytes():
    assert nrpn.program_change(0, 5) == bytes([0xC0, 5])
    assert nrpn.program_change(2, 0) == bytes([0xC2, 0])
    assert nrpn.program_change(0, 200) == bytes([0xC0, 72])


def test_builds_request_multi():
    assert nrpn.request_multi(0x02, 0x7F, 0x34, 0x00) == bytes.fromhex("f0002033027f42003400f7")


def test_builds_request_rendered_string():
    assert nrpn.request_rendered_string(0x02, 0x7F, 0x3C, 53, 8192) == bytes.fromhex(
        "f0002033027f7c003c354000f7"
    )


def test_parses_rendered_string_reply():
    msb, lsb = nrpn.u14_split(8192)
    message = nrpn.sysex(
        PRODUCT_PLAYER,
        DEVICE_OMNI,
        FUNCTION_RENDERED_STRING_REPLY,
        0x3C,
        53,
        bytes([msb, lsb]) + b"<0.0>\x00",
    )
    assert nrpn.parse_rendered_string(message) == (0x3C, 53, 8192, "<0.0>")

    # A too-short reply (no value pair) is rejected.
    short = nrpn.sysex(
        PRODUCT_PLAYER, DEVICE_OMNI, FUNCTION_RENDERED_STRING_REPLY, 0x3C, 53, bytes([0x40])
    )
    assert nrpn.parse_rendered_string(short) is None

    # A wrong function is rejected.
    assert nrpn.parse_rendered_string(nrpn.set_single(0x00, 0x7F, 0x3C, 53, 8192)) is None


def test_multi_values_decodes_consecutive_block():
    values = bytes([*nrpn.u14_split(8192), 0x00, 0x00, *nrpn.u14_split(16383)])
    assert nrpn.multi_values(4, values) == [(4, 8192), (5, 0), (6, 16383)]
    # A trailing odd byte is ignored.
    assert nrpn.multi_values(0, bytes([0x01, 0x02, 0x7F])) == [(0, nrpn.u14(1, 2))]
    assert nrpn.multi_values(0, b"") == []


@pytest.mark.parametrize("value", [0, 1, 6925, 16383, 0x1_2345_6789])
def test_ext_encode_decode_round_trip(value):
    encoded = nrpn.ext_encode(value, 5)
    assert nrpn.ext_decode(encoded) == value
    if value <= 16383:
        msb, lsb = nrpn.u14_split(value)
        assert nrpn.ext_decode(bytes([msb, lsb])) == value


def _ext_string(page: int, number: int, text: bytes) -> bytes:
    address = page * 128 + number
    return (
        bytes([0xF0, 0x00, 0x20, 0x33, 0x00, 0x00, nrpn.FUNCTION_EXT_STRING_PARAM, 0x00])
        + nrpn.ext_encode(address, 5)
        + text
        + b"\x00\xf7"
    )


def test_parse_extended_string_recovers_address_and_text():
    assert nrpn.parse_extended_string(_ext_string(0, 1, b"AC30")) == (1, "AC30")

    address, text = nrpn.parse_extended_string(_ext_string(10, 0, b"JCM800"))
    assert (address // 128, address % 128, text) == (10, 0, "JCM800")

    # A wrong function is rejected.
    assert nrpn.parse_extended_string(nrpn.set_single(0x00, 0x7F, 0x00, 0x01, 1)) is None


def test_parses_a_status_header():
    message = bytes.fromhex("f0002033000002007c4e0000f7")
    header, values = NrpnHeader.parse(message)
    assert (header.function, header.page, header.number) == (0x02, 0x7C, 0x4E)
    assert values == bytes([0x00, 0x00])


def test_header_parse_rejects_non_kemper_messages():
    assert NrpnHeader.parse(b"") is None
    assert NrpnHeader.parse(bytes([0xB0, 0x20, 0x01])) is None
    assert NrpnHeader.parse(bytes.fromhex("f000207700000000000000f7")) is None
    # Too short to carry the fixed header.
    assert NrpnHeader.parse(bytes.fromhex("f0002033000001f7")) is None
