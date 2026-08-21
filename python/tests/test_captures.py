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
from libkp import midi3
from libkp.nrpn import NrpnHeader
from libkp.protocol import TagStream
from libkp.state import DeviceState

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
        assert entry["kind"] in {"discovery", "midi3_stream"}


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
