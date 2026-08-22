"""Conformance suite — every file in ``spec/vectors`` asserted against libkp.

The vectors are the shared behavioral contract: the same hex inputs and expected
outputs every implementation of the protocol is held to.
"""

from __future__ import annotations

import pytest
from conftest import VECTORS_DIR, vector

from libkp import _generated as gen
from libkp import cbor, control, midi3, nrpn, params, protocol
from libkp.state import DeviceState

# ---------------------------------------------------------------------------
# The vector set itself
# ---------------------------------------------------------------------------


def test_spec_version_matches():
    assert gen.SPEC_VERSION == "0.5.0"


def test_every_vector_file_is_covered():
    """Guard against a new vector file landing without a test for it."""
    present = {p.stem for p in VECTORS_DIR.glob("*.json")}
    covered = {"u14", "discovery", "midi3", "nrpn", "controls", "params", "state", "cbor"}
    assert present == covered, f"uncovered vector files: {sorted(present - covered)}"


def test_no_vector_case_list_is_empty():
    """An emptied case list would make its ``parametrize`` collect nothing and pass."""
    empty: list[str] = []
    for path in sorted(VECTORS_DIR.glob("*.json")):
        document = vector(path.stem)
        for key, value in document.items():
            if isinstance(value, list) and not value:
                empty.append(f"{path.stem}.{key}")
    assert empty == [], f"empty vector case lists: {empty}"


# ---------------------------------------------------------------------------
# u14.json
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", vector("u14")["cases"], ids=lambda c: str(c["value"]))
def test_u14_split_and_join(case):
    assert nrpn.u14_split(case["value"]) == (case["msb"], case["lsb"])
    assert nrpn.u14(case["msb"], case["lsb"]) == case["value"]


# ---------------------------------------------------------------------------
# discovery.json
# ---------------------------------------------------------------------------

_DISCOVERY = vector("discovery")


@pytest.mark.parametrize("case", _DISCOVERY["poll_request"], ids=lambda c: c["mac"])
def test_build_poll_request(case):
    built = protocol.build_poll_request(case["mac"])
    assert built.hex() == case["hex"]
    assert len(built) == _DISCOVERY["poll_request_len"]


def test_poll_request_round_trips_through_the_tagstream_parser():
    packet = protocol.build_poll_request()
    stream = protocol.TagStream.parse(packet)
    assert stream.header == b"DSCV"
    assert stream.fields == [b"MAC#00:00:00:00:00:00", b"POLL:)"]


# ---------------------------------------------------------------------------
# midi3.json
# ---------------------------------------------------------------------------

_MIDI3 = vector("midi3")


@pytest.mark.parametrize("case", _MIDI3["unframe"], ids=lambda c: c["stream"][:16])
def test_unframe_stream(case):
    unframer = midi3.Unframer()
    messages = unframer.push(bytes.fromhex(case["stream"]))
    assert [m.hex() for m in messages] == case["messages"]
    assert unframer.pending() == case["pending"]


@pytest.mark.parametrize("case", _MIDI3["frame"], ids=lambda c: c["message"])
def test_frame_message(case):
    message = bytes.fromhex(case["message"])
    framed = midi3.frame(message)
    assert framed.hex() == case["framed"]
    assert len(framed) % 4 == 0


@pytest.mark.parametrize("case", _MIDI3["frame"], ids=lambda c: c["message"])
def test_frame_unframe_round_trip(case):
    message = bytes.fromhex(case["message"])
    unframer = midi3.Unframer()
    assert unframer.push(midi3.frame(message)) == [message]
    assert unframer.pending() == 0


# ---------------------------------------------------------------------------
# nrpn.json
# ---------------------------------------------------------------------------

_NRPN = vector("nrpn")


def _ids(fields):
    return lambda c: "-".join(str(c[f]) for f in fields)


@pytest.mark.parametrize("case", _NRPN["request_string"], ids=_ids(["page", "number"]))
def test_request_string(case):
    built = nrpn.request_string(case["product"], case["device"], case["page"], case["number"])
    assert built.hex() == case["hex"]


@pytest.mark.parametrize("case", _NRPN["request_single"], ids=_ids(["page", "number"]))
def test_request_single(case):
    built = nrpn.request_single(case["product"], case["device"], case["page"], case["number"])
    assert built.hex() == case["hex"]


@pytest.mark.parametrize("case", _NRPN["request_multi"], ids=_ids(["page", "number"]))
def test_request_multi(case):
    built = nrpn.request_multi(case["product"], case["device"], case["page"], case["number"])
    assert built.hex() == case["hex"]


@pytest.mark.parametrize("case", _NRPN["set_single"], ids=_ids(["page", "number", "value"]))
def test_set_single(case):
    built = nrpn.set_single(
        case["product"], case["device"], case["page"], case["number"], case["value"]
    )
    assert built.hex() == case["hex"]


@pytest.mark.parametrize(
    "case", _NRPN["request_rendered_string"], ids=_ids(["page", "number", "value"])
)
def test_request_rendered_string(case):
    built = nrpn.request_rendered_string(
        case["product"], case["device"], case["page"], case["number"], case["value"]
    )
    assert built.hex() == case["hex"]


@pytest.mark.parametrize("case", _NRPN["beacon"], ids=_ids(["init", "tuner", "lease_secs"]))
def test_beacon(case):
    built = nrpn.beacon(
        case["init"], case["tuner"], case["lease_secs"], case["param_set"], case["product"]
    )
    assert built.hex() == case["hex"]


@pytest.mark.parametrize(
    "case", _NRPN["control_change"], ids=_ids(["channel", "controller", "value"])
)
def test_control_change(case):
    built = nrpn.control_change(case["channel"], case["controller"], case["value"])
    assert built.hex() == case["hex"]


@pytest.mark.parametrize("case", _NRPN["header_parse"], ids=lambda c: c["hex"][:20])
def test_header_parse(case):
    parsed = nrpn.NrpnHeader.parse(bytes.fromhex(case["hex"]))
    assert parsed is not None
    header, values = parsed
    assert header.product == case["product"]
    assert header.device == case["device"]
    assert header.function == case["function"]
    assert header.instance == case["instance"]
    assert header.page == case["page"]
    assert header.number == case["number"]
    assert values.hex() == case["values"]


@pytest.mark.parametrize("case", _NRPN["multi_values"], ids=lambda c: c["values"])
def test_multi_values(case):
    pairs = nrpn.multi_values(case["number"], bytes.fromhex(case["values"]))
    assert [list(p) for p in pairs] == case["pairs"]


@pytest.mark.parametrize("case", _NRPN["ext_decode"], ids=lambda c: c["bytes"])
def test_ext_decode(case):
    data = bytes.fromhex(case["bytes"])
    assert nrpn.ext_decode(data) == case["value"]
    assert nrpn.ext_encode(case["value"], len(data)) == data


@pytest.mark.parametrize("case", _NRPN["parse_extended_string"], ids=lambda c: c["hex"][:24])
def test_parse_extended_string(case):
    got = nrpn.parse_extended_string(bytes.fromhex(case["hex"]))
    expected = case["expected"]
    if expected is None:
        assert got is None
    else:
        assert got == (expected["address"], expected["text"])


@pytest.mark.parametrize("case", _NRPN["parse_rendered_string"], ids=lambda c: c["hex"][:24])
def test_parse_rendered_string(case):
    got = nrpn.parse_rendered_string(bytes.fromhex(case["hex"]))
    expected = case["expected"]
    if expected is None:
        assert got is None
    else:
        assert got == (
            expected["page"],
            expected["number"],
            expected["value"],
            expected["text"],
        )


# ---------------------------------------------------------------------------
# controls.json
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case",
    vector("controls")["cases"],
    ids=lambda c: f"{c['op']}-{'-'.join(str(v) for v in c['params'].values())}-ch{c['channel']}",
)
def test_control_op_bytes(case):
    built = control.control_from_op(case["op"], **case["params"])
    assert built.message(case["channel"]).hex() == case["hex"]


def test_every_control_op_is_exercised_by_the_vectors():
    """The vectors need not cover every op, but every op they name must exist."""
    ops = {case["op"] for case in vector("controls")["cases"]}
    assert ops <= set(control.CONTROL_OPS)


# ---------------------------------------------------------------------------
# params.json
# ---------------------------------------------------------------------------

_PARAMS = vector("params")


@pytest.mark.parametrize("case", _PARAMS["param_name"], ids=_ids(["page", "number"]))
def test_param_name(case):
    assert params.param_name(case["page"], case["number"]) == case["name"]


@pytest.mark.parametrize("case", _PARAMS["effect_type_name"], ids=_ids(["value"]))
def test_effect_type_name(case):
    assert params.effect_type_name(case["value"]) == case["name"]


@pytest.mark.parametrize("case", _PARAMS["effect_category_name"], ids=_ids(["value"]))
def test_effect_category_name(case):
    assert params.effect_category_name(case["value"]) == case["name"]


@pytest.mark.parametrize("case", _PARAMS["page_name"], ids=_ids(["page"]))
def test_page_name(case):
    assert params.page_name(case["page"]) == case["name"]


@pytest.mark.parametrize("case", _PARAMS["string_tag_name"], ids=_ids(["number"]))
def test_string_tag_name(case):
    assert params.string_tag_name(case["number"]) == case["name"]


@pytest.mark.parametrize("case", _PARAMS["describe"], ids=_ids(["page", "number"]))
def test_describe(case):
    assert params.describe(case["page"], case["number"]) == case["text"]


# ---------------------------------------------------------------------------
# state.json
# ---------------------------------------------------------------------------


def _assert_state_expectations(state: DeviceState, expect: dict) -> None:
    if "rig_name" in expect:
        assert state.rig.name == expect["rig_name"]
    if "tempo_bpm" in expect:
        assert state.rig.tempo_bpm == expect["tempo_bpm"]
    if "rig_volume" in expect:
        assert state.rig.volume == expect["rig_volume"]
    if "amp_on" in expect:
        assert state.amp.on == expect["amp_on"]
    if "amp_gain" in expect:
        assert state.amp.gain == expect["amp_gain"]
    if "morph" in expect:
        assert state.morph == expect["morph"]
    if "tuner_note" in expect:
        assert state.tuner.note == expect["tuner_note"]
    if "current_bank" in expect:
        assert state.current_bank == expect["current_bank"]
    if "current_rig_slot" in expect:
        assert state.current_rig_slot == expect["current_rig_slot"]
    if "current_rig_index" in expect:
        assert state.current_rig_index == expect["current_rig_index"]
    if "main_volume" in expect:
        assert state.output.main_volume == expect["main_volume"]
    if "headphone_volume" in expect:
        assert state.output.headphone_volume == expect["headphone_volume"]
    if "monitor_volume" in expect:
        assert state.output.monitor_volume == expect["monitor_volume"]
    if "master_volume" in expect:
        assert state.output.master_volume == expect["master_volume"]
    if "bank" in expect:
        for entry in expect["bank"]:
            slot = state.bank.slots[entry["slot"]]
            if "rig_name" in entry:
                assert slot.rig_name == entry["rig_name"]
            if "amp_name" in entry:
                assert slot.amp_name == entry["amp_name"]
            if "cabinet_name" in entry:
                assert slot.cabinet_name == entry["cabinet_name"]
    if "status_raw" in expect:
        assert list(state.status.raw) == expect["status_raw"]
    if "effect" in expect:
        want = expect["effect"]
        effect = state.effect(want["slot"])
        assert effect is not None
        if "kind" in want:
            assert effect.kind == want["kind"]
        if "on" in want:
            assert effect.on == want["on"]
        if "type_name" in want:
            assert effect.type_name == want["type_name"]


@pytest.mark.parametrize("case", vector("state")["cases"], ids=lambda c: c["name"])
def test_state_apply(case):
    state = DeviceState()
    for hex_message in case["messages"]:
        state.apply(bytes.fromhex(hex_message))
    _assert_state_expectations(state, case["expect"])


# ---------------------------------------------------------------------------
# cbor.json
# ---------------------------------------------------------------------------

_CBOR = vector("cbor")


@pytest.mark.parametrize("case", _CBOR["param_write"], ids=lambda c: str(c["addr"]))
def test_cbor_param_write(case):
    assert cbor.to_vec(cbor.param_write(case["addr"], case["value"])).hex() == case["hex"]


def test_cbor_state_dump_request():
    assert cbor.to_vec(cbor.state_dump_request()).hex() == _CBOR["state_dump_request"]["hex"]


@pytest.mark.parametrize("case", _CBOR["extract_snapshot"], ids=lambda c: c["name"])
def test_cbor_extract_snapshot(case):
    decoder = cbor.Decoder()
    items = decoder.push(bytes.fromhex(case["stream_hex"]))
    snap = cbor.extract_snapshot(items)
    expect = case["expect"]
    assert snap.current_bank == expect["current_bank"]
    assert snap.current_rig_slot == expect["current_rig_slot"]
    if "strings" in expect:
        want = [(s["addr"], s["text"]) for s in expect["strings"]]
        assert snap.strings == want
