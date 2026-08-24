"""Replay-capture harness — decode whole recorded streams end to end.

The fixtures in ``spec/captures`` are sanitized recordings of real protocol
traffic gathered through observed experimentation. Where the synthetic vectors
pin individual functions, these prove that a genuine stream — real message
sequences, real framing, the real mix of message types — decodes correctly all
the way through :class:`libkp.state.DeviceState`.
"""

from __future__ import annotations

import pytest
from conftest import CAPTURES_DIR, load_json

from libkp import _generated as gen
from libkp import cbor, midi3
from libkp.nrpn import NrpnHeader
from libkp.protocol import TagStream
from libkp.state import DeviceState, Num, Text

MANIFEST = load_json(CAPTURES_DIR / "manifest.json")
FIXTURES = MANIFEST["fixtures"]


def _load(entry: dict) -> dict:
    return load_json(CAPTURES_DIR / entry["file"])


def _fixtures_of(kind: str) -> list[dict]:
    return [e for e in FIXTURES if e["kind"] == kind]


def _ids(entries: list[dict]):
    return [e["file"] for e in entries]


def test_manifest_lists_existing_fixtures():
    assert FIXTURES, "the capture manifest is empty"
    for entry in FIXTURES:
        assert (CAPTURES_DIR / entry["file"]).is_file(), entry["file"]
        assert entry["kind"] in {"discovery", "midi3_stream", "cbor_stream"}


# ---------------------------------------------------------------------------
# kind: discovery
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("entry", _fixtures_of("discovery"), ids=_ids(_fixtures_of("discovery")))
def test_discovery_reply_decodes(entry):
    fixture = _load(entry)
    stream = TagStream.parse(bytes.fromhex(fixture["raw"]))
    expected = fixture["expected"]

    if "header" in expected:
        assert stream.header is not None
        assert stream.header.decode("ascii") == expected["header"]

    if "key_values" in expected:
        got = [[key, value.decode("utf-8")] for key, value in stream.key_values()]
        assert got == expected["key_values"]


# ---------------------------------------------------------------------------
# kind: midi3_stream
# ---------------------------------------------------------------------------

_STREAMS = _fixtures_of("midi3_stream")


def _unframe(fixture: dict) -> tuple[list[bytes], int]:
    unframer = midi3.Unframer()
    messages = unframer.push(bytes.fromhex(fixture["raw"]))
    return messages, unframer.pending()


def _function_key(message: bytes) -> str:
    parsed = NrpnHeader.parse(message)
    return "none" if parsed is None else str(parsed[0].function)


@pytest.mark.parametrize("entry", _STREAMS, ids=_ids(_STREAMS))
def test_midi3_stream_decodes(entry):
    fixture = _load(entry)
    expected = fixture["expected"]
    messages, pending = _unframe(fixture)

    if "message_count" in expected:
        assert len(messages) == expected["message_count"]
    if "pending" in expected:
        assert pending == expected["pending"]
    if "messages" in expected:
        assert [m.hex() for m in messages] == expected["messages"]


@pytest.mark.parametrize("entry", _STREAMS, ids=_ids(_STREAMS))
def test_midi3_stream_status_frames(entry):
    fixture = _load(entry)
    frames = fixture["expected"].get("status_frames")
    if not frames:
        pytest.skip("fixture declares no status frames")

    messages, _pending = _unframe(fixture)
    for frame in frames:
        state = DeviceState()
        state.apply(messages[frame["index"]])
        assert list(state.status.raw) == frame["raw"]
        assert len(frame["raw"]) == gen.METER_COUNT


@pytest.mark.parametrize("entry", _STREAMS, ids=_ids(_STREAMS))
def test_midi3_stream_function_histogram(entry):
    fixture = _load(entry)
    expected = fixture["expected"].get("function_histogram")
    if not expected:
        pytest.skip("fixture declares no function histogram")

    messages, _pending = _unframe(fixture)
    histogram: dict[str, int] = {}
    for message in messages:
        key = _function_key(message)
        histogram[key] = histogram.get(key, 0) + 1
    assert histogram == expected


@pytest.mark.parametrize("entry", _STREAMS, ids=_ids(_STREAMS))
def test_midi3_stream_state(entry):
    fixture = _load(entry)
    expected = fixture["expected"].get("state")
    if not expected:
        pytest.skip("fixture declares no state expectations")

    messages, _pending = _unframe(fixture)
    state = DeviceState()
    for message in messages:
        state.apply(message)

    if "rig_name" in expected:
        assert state.rig.name == expected["rig_name"]
    if "amp_name" in expected:
        assert state.amp.name == expected["amp_name"]
    if "cab_name" in expected:
        assert state.cabinet.name == expected["cab_name"]


@pytest.mark.parametrize("entry", _STREAMS, ids=_ids(_STREAMS))
def test_midi3_stream_reframes_to_itself(entry):
    """Every decoded message re-frames and re-unframes to the same bytes."""
    fixture = _load(entry)
    messages, _pending = _unframe(fixture)
    reframed = b"".join(midi3.frame(m) for m in messages)
    assert midi3.Unframer().push(reframed) == messages


@pytest.mark.parametrize("entry", _STREAMS, ids=_ids(_STREAMS))
def test_midi3_stream_survives_arbitrary_chunking(entry):
    """Splitting the stream at a non-frame boundary must not change the decode."""
    fixture = _load(entry)
    raw = bytes.fromhex(fixture["raw"])
    whole, _pending = _unframe(fixture)

    unframer = midi3.Unframer()
    chunked: list[bytes] = []
    step = 7  # deliberately coprime with the 4-byte frame size
    for i in range(0, len(raw), step):
        chunked.extend(unframer.push(raw[i : i + step]))
    assert chunked == whole


# ---------------------------------------------------------------------------
# kind: cbor_stream
# ---------------------------------------------------------------------------

_CBOR_STREAMS = _fixtures_of("cbor_stream")


def _decode(fixture: dict) -> tuple[list, cbor.Decoder]:
    decoder = cbor.Decoder()
    items = decoder.push(bytes.fromhex(fixture["raw"]))
    return items, decoder


def _item_head(item: object) -> tuple[int, int] | None:
    """The ``(selector, address)`` an item names, a leading source flag skipped;
    ``None`` for anything that is not one of the channel's array shapes."""
    fields = cbor._as_array(item)
    if not fields:
        return None
    first = cbor._as_int(fields[0])
    rest = fields[1:] if first is not None and first < 0 else fields
    selector = cbor._as_int(rest[0]) if rest else None
    address = cbor._as_int(rest[1]) if len(rest) > 1 else None
    if selector is None or address is None:
        return None
    return selector, address


@pytest.mark.parametrize("entry", _CBOR_STREAMS, ids=_ids(_CBOR_STREAMS))
def test_cbor_stream_decodes(entry):
    fixture = _load(entry)
    expected = fixture["expected"]
    items, decoder = _decode(fixture)

    if "item_count" in expected:
        assert len(items) == expected["item_count"]
    if "pending" in expected:
        assert decoder.pending() == expected["pending"]
    if "filler_bytes" in expected:
        assert decoder.filler_bytes() == expected["filler_bytes"]
    if "numeric_count" in expected:
        assert len(cbor.numeric_values(items)) == expected["numeric_count"]
    if "strings" in expected:
        # As a reader surfaces them: a sensitive address is redacted, the
        # same view the Rust and Swift harnesses assert off their snapshots.
        got = [
            [
                address,
                gen.REDACTED_PLACEHOLDER if address in gen.SENSITIVE_ADDRESSES else decoded.text,
            ]
            for item in cbor.control_items(items)
            for address, decoded in item.values
            if isinstance(decoded, Text)
        ]
        assert got == expected["strings"]


@pytest.mark.parametrize("entry", _CBOR_STREAMS, ids=_ids(_CBOR_STREAMS))
def test_cbor_stream_item_shapes(entry):
    """The blobs, the live singles and the run that closes the dump, by index."""
    fixture = _load(entry)
    expected = fixture["expected"]
    items, _decoder = _decode(fixture)
    heads = [_item_head(item) for item in items]

    if "blob_count" in expected:
        blobs = sum(1 for head in heads if head is not None and head[0] == 5)
        assert blobs == expected["blob_count"]
        # A blob is opaque to the walk: every other item is one of the three
        # value-bearing shapes, so the walk yields exactly the rest.
        assert len(cbor.control_items(items)) == len(items) - blobs
    if "live_items" in expected:
        for address, count in expected["live_items"].items():
            got = sum(1 for head in heads if head == (gen.CBOR_SELECTOR_SINGLE, int(address)))
            assert got == count, f"live items at {address}"
    if "dump_end_index" in expected:
        ends = [
            i
            for i, head in enumerate(heads)
            if head == (gen.CBOR_SELECTOR_MULTI, gen.DUMP_END_ADDRESS)
        ]
        assert ends, "no run based at DUMP_END_ADDRESS"
        assert ends[-1] == expected["dump_end_index"]


@pytest.mark.parametrize("entry", _CBOR_STREAMS, ids=_ids(_CBOR_STREAMS))
def test_cbor_stream_state(entry):
    fixture = _load(entry)
    expected = fixture["expected"].get("state")
    if not expected:
        pytest.skip("fixture declares no state expectations")

    items, _decoder = _decode(fixture)
    state = DeviceState()
    for item in cbor.control_items(items):
        for address, decoded in item.values:
            if isinstance(decoded, Num):
                state.apply_cbor(address, decoded.value)
            else:
                state.apply_cbor_text(address, decoded.text)

    if "rig_name" in expected:
        assert state.rig.name == expected["rig_name"]
    if "amp_name" in expected:
        assert state.amp.name == expected["amp_name"]
    if "cab_name" in expected:
        assert state.cabinet.name == expected["cab_name"]
    if "current_bank" in expected:
        assert state.current_bank == expected["current_bank"]
    if "current_rig_slot" in expected:
        assert state.current_rig_slot == expected["current_rig_slot"]
    if "morph" in expected:
        assert state.morph == expected["morph"]
    if "bank" in expected:
        got = [
            {"rig_name": s.rig_name, "amp_name": s.amp_name, "cab_name": s.cabinet_name}
            for s in state.bank.slots
        ]
        assert got == expected["bank"]
    if "status_raw" in expected:
        assert list(state.status.raw) == expected["status_raw"]
        assert len(expected["status_raw"]) == gen.METER_COUNT


@pytest.mark.parametrize("entry", _CBOR_STREAMS, ids=_ids(_CBOR_STREAMS))
def test_cbor_stream_survives_arbitrary_chunking(entry):
    """Splitting the stream at a non-item boundary must not change the decode."""
    fixture = _load(entry)
    raw = bytes.fromhex(fixture["raw"])
    whole, _decoder = _decode(fixture)

    decoder = cbor.Decoder()
    chunked: list = []
    step = 7  # small enough to split every multi-byte head and run
    for i in range(0, len(raw), step):
        chunked.extend(decoder.push(raw[i : i + step]))
    assert chunked == whole
    assert decoder.pending() == 0
