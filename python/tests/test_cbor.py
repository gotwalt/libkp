"""Unit tests for the CBOR channel codec, the control link and the state-dump
snapshot."""

from __future__ import annotations

import asyncio

from libkp import _generated as gen
from libkp import cbor
from libkp.session import PROTOCOL_CBOR_CONTROL
from libkp.state import Num, Text
from libkp.testing import DEFAULT_DUMP, FakeDevice, wait_for


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


def test_control_items_keep_item_boundaries_and_skip_blobs():
    """The model ends the dump phase on the item whose base is the end
    address, so the walk must keep items apart -- and an opaque ``[5, …]``
    blob, or a pair no implementation can represent, yields nothing."""
    run = cbor.Tag(1, [2, gen.DUMP_END_ADDRESS, 7, 8])
    name = cbor.Tag(1, [4, 1, "Maz 18 Pushed"])
    blob = cbor.Tag(1, [5, 17, b"\x00\x01"])
    flagged = cbor.Tag(1, [-1, 1, gen.MORPH_ADDRESS, 3])
    wide = cbor.param_write(0xFFFF_FFFF + 1, 1)
    items = cbor.control_items([run, name, blob, flagged, wide])
    assert [item.base for item in items] == [gen.DUMP_END_ADDRESS, 1, gen.MORPH_ADDRESS, 4294967296]
    assert items[0].values == ((gen.DUMP_END_ADDRESS, Num(7)), (gen.DUMP_END_ADDRESS + 1, Num(8)))
    assert items[1].values == ((1, Text("Maz 18 Pushed")),)
    assert items[2].values == ((gen.MORPH_ADDRESS, Num(3)),)
    assert items[3].values == ()


def test_session_backlog_survives_a_late_subscriber():
    """The state dump lands as soon as the link opens, which is before a
    caller can subscribe. Those values must not be sent into a void -- the morph
    among them appears nowhere else until something moves them."""
    session = cbor.CborSession(None)
    # Values arriving with nobody subscribed are held...
    session._on_items(cbor.control_items([cbor.param_write(gen.MORPH_ADDRESS, 8192)]))
    assert list(session._backlog) == [(gen.MORPH_ADDRESS, 8192)]
    # ...and replayed to the first subscriber.
    queue = session.updates()
    assert queue.get_nowait() == (gen.MORPH_ADDRESS, 8192)
    assert not session._backlog
    # Once subscribed, values go straight out rather than accumulating.
    session._on_items(cbor.control_items([cbor.param_write(gen.MORPH_ADDRESS, 0)]))
    assert queue.get_nowait() == (gen.MORPH_ADDRESS, 0)
    assert not session._backlog


def test_the_backlog_replay_outgrows_the_broadcast_depth():
    """A full dump buffers thousands of values before anyone can subscribe;
    the first subscriber receives every one of them -- well past the fan-out
    queues' 256-item bound -- rather than crashing into it."""
    session = cbor.CborSession(None)
    count = 600
    session._on_items(
        cbor.control_items([cbor.param_write(gen.MORPH_ADDRESS, v) for v in range(count)])
    )
    queue = session.updates()
    values = [queue.get_nowait()[1] for _ in range(count)]
    assert values == list(range(count))
    assert not session._backlog


# ---------------------------------------------------------------------------
# The link, against the stand-in device
# ---------------------------------------------------------------------------


def test_fetch_state_snapshot_reads_the_dump_over_the_control_link():
    async def scenario():
        async with FakeDevice(offer_cbor=True) as device:
            snapshot = await cbor.fetch_state_snapshot("127.0.0.1", port=device.port, timeout=2.0)
            await wait_for(lambda: all(c.closed.is_set() for c in device.connections))
            return snapshot, device

    snapshot, device = asyncio.run(scenario())
    assert snapshot.is_complete()
    assert (snapshot.current_bank, snapshot.current_rig_slot, snapshot.morph) == (3, 1, 8192)
    assert snapshot.string(gen.STRING_RIG_NAME) == "Dump Rig"
    # One socket, the trigger written exactly once, and nothing left open.
    assert device.connection_count(PROTOCOL_CBOR_CONTROL) == 1
    assert device.control.received_items == [cbor.state_dump_request()]


def test_cbor_session_streams_the_dump_and_later_pushes():
    async def scenario():
        async with FakeDevice(offer_cbor=True) as device:
            async with await cbor.CborSession.connect("127.0.0.1", device.port) as session:
                queue = session.updates()
                seen = []
                while len(seen) < len(cbor.numeric_values(DEFAULT_DUMP)):
                    seen.append(await asyncio.wait_for(queue.get(), 2.0))
                await device.push_items([cbor.param_write(gen.MORPH_ADDRESS, 0)])
                seen.append(await asyncio.wait_for(queue.get(), 2.0))
                return seen

    seen = asyncio.run(scenario())
    # The whole dump, in document order, then the later push.
    assert seen[:-1] == cbor.numeric_values(DEFAULT_DUMP)
    assert (gen.CURRENT_BANK_ADDRESS, 3) in seen
    assert (gen.CURRENT_RIG_SLOT_ADDRESS, 1) in seen
    assert (gen.MORPH_ADDRESS, 8192) in seen
    assert seen[-1] == (gen.MORPH_ADDRESS, 0)
