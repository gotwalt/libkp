"""MIDI3 stream framing."""

from __future__ import annotations

import pytest

from libkp.midi3 import TAG_CONT, Unframer, frame, is_kemper_sysex


def test_unframes_a_19_byte_sysex():
    raw = bytes.fromhex("14f00020143302001406000014000620140500001400001015f70000")
    unframer = Unframer()
    messages = unframer.push(raw)
    assert messages == [bytes.fromhex("f00020330200060000000620050000000010f7")]
    assert is_kemper_sysex(messages[0])
    assert unframer.pending() == 0


@pytest.mark.parametrize(
    "message",
    [
        bytes.fromhex("f0002033027f7e004002230ff7"),  # 13 bytes
        bytes.fromhex("f0002033f7"),  # 5 bytes
        bytes.fromhex("f000203301f7"),  # 6 bytes (a multiple of 3)
        bytes([0xF0] + [0x00] * 40 + [0xF7]),
    ],
)
def test_frame_unframe_round_trip(message):
    framed = frame(message)
    assert len(framed) % 4 == 0
    assert Unframer().push(framed) == [message]


def test_reassembles_across_chunk_boundaries():
    raw = bytes.fromhex("14f0002014330200170102f7")
    unframer = Unframer()
    assert unframer.push(raw[:5]) == []
    assert unframer.push(raw[5:]) == [bytes.fromhex("f000203302000102f7")]
    assert unframer.pending() == 0


def test_pending_counts_partial_frames():
    unframer = Unframer()
    assert unframer.push(bytes.fromhex("14f0002017f7000014aa")) == [bytes.fromhex("f00020f70000")]
    assert unframer.pending() == 2


def test_unknown_tag_resyncs_and_drops_the_partial_message():
    unframer = Unframer()
    # A good opening group, then garbage, then a clean complete message.
    stream = bytes([TAG_CONT, 0xF0, 0x00, 0x20]) + bytes([0xAA, 0xAA, 0xAA, 0xAA])
    stream += frame(bytes.fromhex("f000203301f7"))
    messages = unframer.push(stream)
    assert messages == [bytes.fromhex("f000203301f7")]


def test_empty_message_frames_to_nothing():
    assert frame(b"") == b""


def test_reset_drops_buffered_bytes():
    unframer = Unframer()
    unframer.push(bytes([TAG_CONT, 1, 2, 3, 0x99]))
    assert unframer.pending() == 4
    unframer.reset()
    assert unframer.pending() == 0


def test_is_kemper_sysex_rejects_other_traffic():
    assert not is_kemper_sysex(b"")
    assert not is_kemper_sysex(bytes([0xB0, 0x20, 0x01]))
    assert not is_kemper_sysex(bytes.fromhex("f000207701f7"))
    assert not is_kemper_sysex(bytes.fromhex("f000203301"))
