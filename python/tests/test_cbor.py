"""Unit tests for the CBOR channel codec and the state-dump snapshot."""

from __future__ import annotations

from libkp import _generated as gen
from libkp import cbor


def test_encodes_with_minimal_length_heads():
    # uint16 address, small value.
    assert cbor.to_vec(cbor.param_write(15953, 0)) == bytes.fromhex("c18301193e5100")
    # uint32 address (the extended range).
    assert cbor.to_vec(cbor.param_write(102405, 19)) == bytes.fromhex("c183011a0001900513")


def test_encodes_the_state_dump_request():
    # tag(1)([1, 102528, 1]) — the write that asks for the full state.
    assert cbor.to_vec(cbor.state_dump_request()) == bytes.fromhex("c183011a0001908001")


def test_round_trips_through_the_decoder():
    for item in (cbor.param_write(102528, 1), cbor.param_write(0, 0), cbor.param_write(16383, -1)):
        decoder = cbor.Decoder()
        out = decoder.push(cbor.to_vec(item))
        assert out == [item]
        assert decoder.pending() == 0


def test_decoder_skips_inter_item_filler():
    data = bytes([gen.CBOR_FILLER_BYTE, gen.CBOR_FILLER_BYTE]) + cbor.to_vec(
        cbor.param_write(1412, 8629)
    )
    decoder = cbor.Decoder()
    assert decoder.push(data) == [cbor.param_write(1412, 8629)]
    assert decoder.filler_bytes() == 2


def test_decoder_buffers_a_partial_item_across_chunks():
    full = cbor.to_vec(cbor.param_write(102528, 1))
    decoder = cbor.Decoder()
    assert decoder.push(full[:4]) == []
    assert decoder.pending() == 4
    assert decoder.push(full[4:]) == [cbor.param_write(102528, 1)]
    assert decoder.pending() == 0


def test_extracts_position_from_a_multi_run():
    run = cbor.Tag(1, [2, 100_700, 0, 1, 2])
    snap = cbor.extract_snapshot([run])
    assert snap.current_bank == 1
    assert snap.current_rig_slot == 2
    # The morph has not landed, so the reader must keep going.
    assert not snap.is_complete()
    snap = cbor.extract_snapshot([run, cbor.param_write(119, 8192)])
    assert snap.morph == 8192
    assert snap.is_complete()


def test_extracts_position_from_single_items():
    items = [cbor.param_write(100_701, 3), cbor.param_write(100_702, 4)]
    snap = cbor.extract_snapshot(items)
    assert snap.current_bank == 3
    assert snap.current_rig_slot == 4


def test_collects_strings_and_redacts_secrets():
    name = cbor.Tag(1, [4, 1, "Maz 18 Pushed"])
    secret = cbor.Tag(1, [4, gen.SENSITIVE_ADDRESSES[0], "hunter2"])
    snap = cbor.extract_snapshot([name, secret])
    assert snap.string(1) == "Maz 18 Pushed"
    assert snap.string(gen.SENSITIVE_ADDRESSES[0]) == gen.REDACTED_PLACEHOLDER


def test_snapshot_reads_the_position_by_the_tree_rows():
    """The dump folds through the same routing a live session uses, so a bank
    index wider than the row's 16 bits is dropped rather than wrapped, and one
    past 14 bits is kept: the position rows are ``u16``, the morph is ``u14``."""
    snap = cbor.extract_snapshot(
        [
            cbor.param_write(gen.CURRENT_BANK_ADDRESS, 40_000),
            cbor.param_write(gen.CURRENT_RIG_SLOT_ADDRESS, 70_000),
            cbor.param_write(gen.MORPH_ADDRESS, 16_384),
        ]
    )
    assert snap.current_bank == 40_000
    assert snap.current_rig_slot is None
    assert snap.morph is None


def test_numeric_values_skips_unrepresentable_pairs():
    """A pair every implementation would have to widen or wrap is malformed."""
    good = cbor.param_write(gen.MORPH_ADDRESS, 8192)
    assert cbor.numeric_values([good]) == [(gen.MORPH_ADDRESS, 8192)]
    wide_address = cbor.param_write(0xFFFF_FFFF + 1, 1)
    wide_value = cbor.param_write(gen.MORPH_ADDRESS, 1 << 63)
    negative_address = cbor.param_write(-1, 1)
    assert cbor.numeric_values([wide_address, wide_value, negative_address]) == []
    # The bounds themselves are representable.
    assert cbor.numeric_values([cbor.param_write(0xFFFF_FFFF, -(1 << 63))]) == [
        (0xFFFF_FFFF, -(1 << 63))
    ]


def test_empty_snapshot_is_incomplete():
    snap = cbor.extract_snapshot([])
    assert snap.current_bank is None
    assert snap.current_rig_slot is None
    assert not snap.is_complete()


def test_session_backlog_survives_a_late_subscriber():
    """The state dump lands as soon as the session opens, which is before a
    caller can subscribe. Those values must not be sent into a void -- the morph
    among them appears nowhere else until something moves it."""
    session = cbor.CborSession(session=None)
    # Values arriving with nobody subscribed are held...
    session._emit([cbor.param_write(gen.MORPH_ADDRESS, 8192)])
    assert list(session._backlog) == [(gen.MORPH_ADDRESS, 8192)]
    # ...and replayed to the first subscriber.
    queue = session.updates()
    assert queue.get_nowait() == (gen.MORPH_ADDRESS, 8192)
    assert not session._backlog
    # Once subscribed, values go straight out rather than accumulating.
    session._emit([cbor.param_write(gen.MORPH_ADDRESS, 0)])
    assert queue.get_nowait() == (gen.MORPH_ADDRESS, 0)
    assert not session._backlog
