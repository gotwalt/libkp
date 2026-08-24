"""The device-state tree and its pure decode routing."""

from __future__ import annotations

import pytest

from libkp import _generated as gen
from libkp import _routes, nrpn
from libkp.nrpn import DEVICE_OMNI, PAGE_STRINGS, set_single, sysex, u14, u14_split
from libkp.state import (
    ApplyOutcome,
    BankPreview,
    BeatPulse,
    Block,
    Channel,
    Connection,
    CurrentPosition,
    DeviceState,
    EffectChanged,
    MorphButton,
    MorphChanged,
    Num,
    ParamChanged,
    Phase,
    RealtimeStatus,
    RenderedString,
    RigChanged,
    Status,
    StringTag,
    TempoBpm,
    Text,
    TunerDeviance,
    TunerNote,
    Update,
    _decode,
)

FN_STRING = nrpn.FUNCTION_STRING_PARAM
FN_MULTI = nrpn.FUNCTION_MULTI_PARAM
FN_RENDERED = nrpn.FUNCTION_RENDERED_STRING_REPLY


def meter_block(values: list[int]) -> bytes:
    out = bytearray()
    for v in values:
        out.extend(u14_split(v))
    return bytes(out)


def ext_string(page: int, number: int, text: bytes) -> bytes:
    address = page * 128 + number
    return (
        bytes([0xF0, 0x00, 0x20, 0x33, 0x00, 0x00, nrpn.FUNCTION_EXT_STRING_PARAM, 0x00])
        + nrpn.ext_encode(address, 5)
        + text
        + b"\x00\xf7"
    )


# ---------------------------------------------------------------------------
# The empty tree
# ---------------------------------------------------------------------------


def test_new_state_seeds_eight_slots_in_order():
    state = DeviceState()
    assert state.connection is Connection.DISCONNECTED
    assert len(state.effects) == 8
    assert (state.effects[0].slot, state.effects[0].page) == ("A", 0x32)
    assert (state.effects[7].slot, state.effects[7].page) == ("REV", 0x3D)
    assert all(e.kind is None and e.on is None and e.mix is None for e in state.effects)
    assert state.status.raw == (0,) * gen.METER_COUNT


def test_effect_lookup_is_case_insensitive():
    state = DeviceState()
    assert state.effect("rev").slot == "REV"
    assert state.effect("A").page == 0x32
    assert state.effect("nope") is None


def test_effect_type_name_and_is_empty():
    effect = DeviceState().effects[7]
    assert effect.type_name is None
    effect.kind = 0
    assert effect.is_empty
    effect.kind = 179
    assert not effect.is_empty
    assert effect.type_name == "Easy Reverb"
    assert effect.category_name == "Reverb"


@pytest.mark.parametrize(
    "deviance,expected",
    [(None, None), (8192, True), (8192 + 350, True), (8192 + 351, False), (0, False)],
)
def test_tuner_in_tune_window(deviance, expected):
    state = DeviceState()
    state.tuner.deviance = deviance
    assert state.tuner.in_tune is expected


def test_snapshot_is_independent():
    state = DeviceState()
    snap = state.snapshot()
    state.rig.name = "changed"
    state.effects[0].on = True
    assert snap.rig.name is None
    assert snap.effects[0].on is None


# ---------------------------------------------------------------------------
# Strings
# ---------------------------------------------------------------------------


def test_rig_name_string_updates_and_signals_a_rig_change():
    state = DeviceState()
    out = state.apply(sysex(0x00, 0x7F, FN_STRING, PAGE_STRINGS, 1, b"AC30\x00"))
    assert state.rig.name == "AC30"
    assert out.slow_changed
    assert out.events == [StringTag(1), RigChanged()]

    # A non-name tag does not signal a rig change but is still slow.
    out = state.apply(sysex(0x00, 0x7F, FN_STRING, PAGE_STRINGS, 2, b"Author"))
    assert state.rig.author == "Author"
    assert out.slow_changed
    assert out.events == [StringTag(2)]

    state.apply(sysex(0x00, 0x7F, FN_STRING, PAGE_STRINGS, gen.STRING_AMP_NAME, b"JCM"))
    state.apply(sysex(0x00, 0x7F, FN_STRING, PAGE_STRINGS, 32, b"412"))
    assert state.amp.name == "JCM"
    assert state.cabinet.name == "412"

    # An untracked string tag has no row, so it is silent: nothing stored, no
    # event, no snapshot.
    out = state.apply(sysex(0x00, 0x7F, FN_STRING, PAGE_STRINGS, 99, b"x"))
    assert out == ApplyOutcome.empty()


def test_ext_string_recovers_amp_name():
    state = DeviceState()
    out = state.apply(ext_string(PAGE_STRINGS, gen.STRING_AMP_NAME, b"JCM800"))
    assert state.amp.name == "JCM800"
    assert out.slow_changed
    assert out.events == [StringTag(gen.STRING_AMP_NAME)]


def test_ext_string_rig_name_signals_a_rig_change():
    state = DeviceState()
    out = state.apply(ext_string(PAGE_STRINGS, nrpn.STRING_RIG_NAME, b"AC30"))
    assert state.rig.name == "AC30"
    assert out.events == [StringTag(1), RigChanged()]


def test_ext_string_off_the_string_page_is_ignored():
    state = DeviceState()
    assert state.apply(ext_string(0x0A, 0, b"nope")) == ApplyOutcome.empty()


# ---------------------------------------------------------------------------
# Numeric routing
# ---------------------------------------------------------------------------


def test_effect_type_state_and_mix_fold_into_the_slot():
    state = DeviceState()
    out = state.apply(set_single(0x00, 0x7F, 0x3D, 0, 179))
    assert out.events == [EffectChanged(7)]
    assert out.slow_changed
    assert state.effects[7].kind == 179
    assert state.effects[7].type_name == "Easy Reverb"

    out = state.apply(set_single(0x00, 0x7F, 0x3D, 3, 1))
    assert out.events == [EffectChanged(7)]
    assert state.effects[7].on is True

    out = state.apply(set_single(0x00, 0x7F, 0x3D, 4, 8192))
    assert out.events == [EffectChanged(7)]
    assert out.slow_changed
    assert state.effect("rev").mix == 8192


def test_meter_block_fills_status_and_is_fast():
    state = DeviceState()
    values = [0] * gen.METER_COUNT
    values[3] = 4096
    values[4] = 9000
    values[6] = 12000
    values[9] = 3000
    out = state.apply(
        sysex(0x00, 0x00, FN_MULTI, gen.PAGE_REALTIME, gen.METER_BLOCK_NUMBER, meter_block(values))
    )
    assert out.events == [Status(state.status)]
    assert not out.slow_changed
    assert state.status.strobe_phase == 4096
    assert state.status.stack_level == 9000
    assert state.status.rig_out_level == 12000
    assert state.status.loudness == 3000
    assert list(state.status.raw) == values


def test_meter_block_tolerates_a_short_value_block():
    status = RealtimeStatus.from_values(meter_block([1, 2, 3]))
    assert status.raw == (1, 2, 3, 0, 0, 0, 0, 0, 0, 0, 0)
    assert status.field("strobe_seg_low") == 1
    with pytest.raises(KeyError):
        status.field("nope")


def test_strobe_active_tracks_the_first_four_fields():
    assert not RealtimeStatus().strobe_active
    assert RealtimeStatus.from_values(meter_block([0, 0, 0, 5])).strobe_active
    assert not RealtimeStatus.from_values(meter_block([0, 0, 0, 0, 9000])).strobe_active


def test_beat_pulse_is_fast_and_touches_nothing():
    state = DeviceState()
    before = state.snapshot()
    out = state.apply(set_single(0x00, 0x00, gen.PAGE_REALTIME, gen.BEAT_PULSE_NUMBER, 16383))
    assert out.events == [BeatPulse(True)]
    assert not out.slow_changed
    assert state == before

    out = state.apply(set_single(0x00, 0x00, gen.PAGE_REALTIME, gen.BEAT_PULSE_NUMBER, 0))
    assert out.events == [BeatPulse(False)]
    assert not out.slow_changed


def test_tempo_and_rig_volume_set_fields():
    state = DeviceState()
    out = state.apply(set_single(0x00, 0x00, gen.PAGE_RIG_SETTINGS, gen.TEMPO_NUMBER, 7680))
    assert out.events == [TempoBpm(120)]
    assert out.slow_changed
    assert state.rig.tempo_bpm == 120

    out = state.apply(set_single(0x00, 0x00, gen.PAGE_RIG_SETTINGS, gen.RIG_VOLUME_NUMBER, 4096))
    assert out.slow_changed
    assert state.rig.volume == 4096


def test_amp_on_and_gain_set_fields():
    state = DeviceState()
    out = state.apply(set_single(0x00, 0x00, gen.AMP_PAGE, gen.AMP_ON_NUMBER, 1))
    assert out.slow_changed
    assert state.amp.on is True

    out = state.apply(set_single(0x00, 0x00, gen.AMP_PAGE, gen.GAIN_NUMBER, 5000))
    assert out.slow_changed
    assert state.amp.gain == 5000
    assert out.events == [ParamChanged(0x0A, 4, 5000)]


def test_output_volumes_set_fields():
    state = DeviceState()
    out = state.apply(set_single(0x00, 0x00, gen.SYSTEM_PAGE, gen.MAIN_VOLUME_NUMBER, 9000))
    assert out.slow_changed
    assert state.output.main_volume == 9000

    out = state.apply(set_single(0x00, 0x00, gen.SYSTEM_PAGE, gen.MONITOR_VOLUME_NUMBER, 3000))
    assert out.slow_changed
    assert state.output.monitor_volume == 3000


def test_morph_sets_the_position():
    state = DeviceState()
    out = state.apply(set_single(0x00, 0x00, gen.PAGE_MORPH, gen.MORPH_NUMBER, 8192))
    assert out.events == [MorphChanged(8192)]
    assert out.slow_changed
    assert state.morph == 8192


def test_tuner_deviance_is_fast():
    state = DeviceState()
    out = state.apply(set_single(0x00, 0x00, gen.PAGE_REALTIME, gen.TUNER_DEVIANCE_NUMBER, 8192))
    assert out.events == [TunerDeviance(8192)]
    assert not out.slow_changed
    assert state.tuner.deviance == 8192
    assert state.tuner.in_tune is True


def test_tuner_note_is_slow():
    state = DeviceState()
    out = state.apply(set_single(0x00, 0x00, gen.PAGE_TUNER_NOTE, gen.TUNER_NOTE_NUMBER, 45))
    assert out.events == [TunerNote(45)]
    assert out.slow_changed
    assert state.tuner.note == 45


def test_untracked_generic_param_is_not_slow():
    state = DeviceState()
    out = state.apply(set_single(0x00, 0x00, 0x09, 3, 5000))
    assert not out.slow_changed
    assert out.events == [ParamChanged(0x09, 3, 5000)]


def test_rig_load_dump_routes_multiple_values():
    state = DeviceState()
    values = bytes(
        [
            *u14_split(33),  # number 0: Type = Green Scream
            0x10,
            0x00,  # number 1
            0x20,
            0x00,  # number 2
            0x00,
            0x01,  # number 3: On/Off = 1
        ]
    )
    out = state.apply(sysex(0x00, 0x00, FN_MULTI, 0x32, 0, values))
    assert out.slow_changed
    assert out.events == [
        EffectChanged(0),
        ParamChanged(0x32, 1, u14(0x10, 0x00)),
        ParamChanged(0x32, 2, u14(0x20, 0x00)),
        EffectChanged(0),
    ]
    assert state.effects[0].kind == 33
    assert state.effects[0].type_name == "Green Scream"
    assert state.effects[0].on is True


def test_rendered_string_reply_is_a_fast_event():
    state = DeviceState()
    msb, lsb = u14_split(8192)
    message = sysex(0x02, DEVICE_OMNI, FN_RENDERED, 0x3C, 53, bytes([msb, lsb]) + b"<0.0>\x00")
    out = state.apply(message)
    assert not out.slow_changed
    assert out.events == [RenderedString(0x3C, 53, 8192, "<0.0>")]


def test_non_kemper_messages_are_ignored():
    state = DeviceState()
    assert state.apply(bytes([0xB0, 0x20, 0x01])) == ApplyOutcome.empty()
    assert state.apply(b"") == ApplyOutcome.empty()
    # A $01 with no value pair is ignored.
    assert state.apply(sysex(0x00, 0x00, 0x01, 0x0A, 4, b"")) == ApplyOutcome.empty()
    # A $06 too short to carry its 5-byte address and value is ignored.
    assert state.apply(sysex(0x00, 0x00, 0x06, 0x0A, 4, b"\x00\x00")) == ApplyOutcome.empty()


def test_morph_position_is_slow():
    state = DeviceState()
    out = state.apply(set_single(0x00, 0x00, gen.PAGE_MORPH, gen.MORPH_NUMBER, 8192))
    assert out.slow_changed
    assert out.events == [MorphChanged(8192)]
    assert state.morph == 8192


def test_morph_button_is_momentary():
    state = DeviceState()
    press = state.apply(set_single(0x00, 0x00, gen.PAGE_MORPH, gen.MORPH_BUTTON_NUMBER, 1))
    assert press.events == [MorphButton(on=True)]
    assert not press.slow_changed, "the button stores nothing"
    release = state.apply(set_single(0x00, 0x00, gen.PAGE_MORPH, gen.MORPH_BUTTON_NUMBER, 0))
    assert release.events == [MorphButton(on=False)]
    # The button says a morph happened; it never says where the fader sits.
    assert state.morph is None


def test_morph_is_not_at_0x0b():
    """0x0B came from a third-party mapping and is wrong: the device answers a
    request there with a constant 0 whether the rig is morphed or at base, and
    never pushes it. Nothing may land in ``morph`` from it, or the same silent
    mistake returns -- a value that simply never moves."""
    state = DeviceState()
    out = state.apply(set_single(0x00, 0x00, gen.PAGE_MORPH, 0x0B, 16383))
    assert state.morph is None
    assert not out.slow_changed
    assert out.events == [ParamChanged(0x00, 0x0B, 16383)]


def test_apply_cbor_routes_the_same_as_the_stream():
    """The two channels are one event universe: a value arriving over CBOR lands
    in the same field, and raises the same event, as one arriving over MIDI3."""
    state = DeviceState()
    morph = state.apply_cbor(gen.MORPH_ADDRESS, 8192)
    assert morph.events == [MorphChanged(8192)]
    assert morph.slow_changed
    assert state.morph == 8192

    bank = state.apply_cbor(gen.CURRENT_BANK_ADDRESS, 3)
    assert bank.events == [CurrentPosition(bank=3, slot=None)]
    slot = state.apply_cbor(gen.CURRENT_RIG_SLOT_ADDRESS, 4)
    assert slot.events == [CurrentPosition(bank=3, slot=4)]
    assert state.current_rig_index == 19

    # An unchanged value is not a change, and an unknown address is ignored.
    assert state.apply_cbor(gen.MORPH_ADDRESS, 8192) == ApplyOutcome.empty()
    assert state.apply_cbor(102_405, 31) == ApplyOutcome.empty()
    # A value too wide for the field is dropped, not truncated.
    assert state.apply_cbor(gen.MORPH_ADDRESS, 70_000) == ApplyOutcome.empty()
    assert state.morph == 8192


# ---------------------------------------------------------------------------
# The fold: one funnel for both wires
# ---------------------------------------------------------------------------

#: A value of each kind that is in range and distinct from the fresh tree's.
_SAMPLE_BY_KIND = {
    gen.Kind.U14: 1234,
    gen.Kind.U16: 40000,
    gen.Kind.U7: 9,
    gen.Kind.BOOL: True,
    gen.Kind.TEXT: "sample",
    gen.Kind.BPM: 120,
    gen.Kind.MULTI: RealtimeStatus(raw=tuple(range(gen.METER_COUNT))),
}
#: Rows the tree deliberately has no field for: they are events and nothing more.
_MOMENTARY_FIELDS = {gen.Field.MORPH_BUTTON, gen.Field.BEAT_PULSE}


@pytest.mark.parametrize("field", list(gen.Field), ids=lambda f: f.value)
def test_every_field_is_settable(field):
    """Rust and Swift get exhaustiveness over ``Field`` from the compiler; here a
    row the switch forgot would only surface when that address arrived. So walk
    every field through the switch: a write must land where a read finds it, or,
    for a momentary, leave the tree untouched."""
    routes = [r for r in gen.STATE_ROUTES if r.field is field]
    assert routes, f"{field} has no row in STATE_ROUTES"
    for route in routes:
        state = DeviceState()
        value = _SAMPLE_BY_KIND[route.kind]
        _routes.write(state, route, value)
        if field in _MOMENTARY_FIELDS:
            assert _routes.read(state, route) is None
            assert state == DeviceState()
        else:
            assert _routes.read(state, route) == value
            assert state != DeviceState()


def test_every_route_address_is_found_by_lookup():
    for route in gen.STATE_ROUTES:
        assert _routes.lookup(route.address) is route
    assert _routes.lookup(102_405) is None


def test_apply_cbor_text_lands_the_same_as_a_stream_tag():
    """A string on the control channel is the same tag as a ``$03`` on the
    stream, and dedupes against it: the second wire's copy is a no-op."""
    state = DeviceState()
    out = state.apply_cbor_text(nrpn.STRING_RIG_NAME, "AC30")
    assert state.rig.name == "AC30"
    assert out.slow_changed
    assert out.events == [StringTag(1), RigChanged()]
    assert state.apply(sysex(0x00, 0x7F, FN_STRING, PAGE_STRINGS, 1, b"AC30\x00")) == (
        ApplyOutcome.empty()
    )
    # The bank preview is reachable the same way, by its flat address.
    out = state.apply_cbor_text(gen.PAGE_BANK_PREVIEW * 128 + gen.BANK_AMP_NAME_BASE + 2, "Twin")
    assert out.events == [BankPreview(number=gen.BANK_AMP_NAME_BASE + 2)]
    assert state.bank.slots[2].amp_name == "Twin"


def test_control_channel_copies_of_stream_rows_are_dropped():
    """The control channel carries its own meter, beat and tuner feeds at the
    stream's addresses; those rows are the stream's, so the copies are silent."""
    state = DeviceState()
    fresh = state.snapshot()
    for address in (
        gen.PAGE_REALTIME * 128 + gen.METER_BLOCK_NUMBER + 3,
        gen.PAGE_REALTIME * 128 + gen.BEAT_PULSE_NUMBER,
        gen.PAGE_REALTIME * 128 + gen.TUNER_DEVIANCE_NUMBER,
        gen.PAGE_TUNER_NOTE * 128 + gen.TUNER_NOTE_NUMBER,
        gen.PAGE_MORPH * 128 + gen.MORPH_BUTTON_NUMBER,
    ):
        assert state.apply_cbor(address, 1) == ApplyOutcome.empty()
    assert state == fresh
    # An untracked control address is silent too -- no generic event on this wire.
    assert state.apply_cbor(0x09 * 128 + 3, 5000) == ApplyOutcome.empty()
    # A negative value is not a parameter value and never reaches the table.
    assert state.apply_cbor(gen.MORPH_ADDRESS, -1) == ApplyOutcome.empty()


def test_tracked_rows_dedupe_on_the_decoded_value():
    state = DeviceState()
    assert state.apply(set_single(0x00, 0x00, gen.AMP_PAGE, gen.AMP_ON_NUMBER, 1)).slow_changed
    # A different wire value that decodes to the same bool is not a change.
    assert state.apply_cbor(gen.AMP_PAGE * 128 + gen.AMP_ON_NUMBER, 5) == ApplyOutcome.empty()
    # The meter frame is the one FAST row with state, and it never dedupes.
    frame = sysex(
        0x00,
        0x00,
        FN_MULTI,
        gen.PAGE_REALTIME,
        gen.METER_BLOCK_NUMBER,
        meter_block([7] * gen.METER_COUNT),
    )
    assert len(state.apply(frame).events) == 1
    assert len(state.apply(frame).events) == 1


def test_dump_window_lets_live_pushes_outrank_dump_items():
    """The state dump is a copy taken when it was asked for. A value pushed
    while it streams is newer, so the dump's item for that address must not
    overwrite it -- and only that address: the rest of the dump still lands."""
    state = DeviceState()
    bank = gen.CURRENT_BANK_ADDRESS
    slot = gen.CURRENT_RIG_SLOT_ADDRESS

    def dump(address: int, value: int) -> ApplyOutcome:
        return state.apply_update(Update(Channel.CONTROL, Phase.DUMP, address, Num(value)))

    state.begin_dump()
    assert state.apply_cbor(bank, 3).events == [CurrentPosition(bank=3, slot=None)]
    assert dump(bank, 2) == ApplyOutcome.empty()
    assert dump(slot, 1).events == [CurrentPosition(bank=3, slot=1)]
    state.end_dump()
    assert (state.current_bank, state.current_rig_slot) == (3, 1)
    # Outside a window a dump item folds like a live value.
    assert dump(bank, 2).events == [CurrentPosition(bank=2, slot=1)]
    # The bookkeeping is not part of the snapshot: two trees holding the same
    # values compare equal whatever their dump windows are doing.
    other = state.snapshot()
    state.begin_dump()
    state.apply_cbor(slot, 1)  # deduped, but marks the address
    assert state == other


def test_a_block_off_the_meter_base_folds_element_by_element():
    state = DeviceState()
    out = state.apply_update(
        Update(Channel.STREAM, Phase.LIVE, gen.PAGE_RIG_SETTINGS * 128, Block((7680, 9000)))
    )
    assert out.slow_changed
    assert out.events == [TempoBpm(120), ParamChanged(gen.PAGE_RIG_SETTINGS, 1, 9000)]
    assert (state.rig.tempo_bpm, state.rig.volume) == (120, 9000)
    # A block at the meter base is the frame whatever its length: a short
    # read zero-fills the tail rather than spraying a run of generic reports
    # at meter rate.
    base = gen.PAGE_REALTIME * 128 + gen.METER_BLOCK_NUMBER
    out = state.apply_update(Update(Channel.STREAM, Phase.LIVE, base, Block((1, 2))))
    assert not out.slow_changed
    raw = (1, 2) + (0,) * (gen.METER_COUNT - 2)
    assert out.events == [Status(RealtimeStatus(raw=raw))]
    assert state.status == RealtimeStatus(raw=raw)


def test_a_string_at_a_numeric_row_is_untracked():
    """Page 0 is dual-use; a row's kind says which face it stores."""
    state = DeviceState()
    state.apply_cbor(gen.PAGE_RIG_SETTINGS * 128 + gen.RIG_VOLUME_NUMBER, 100)
    numeric_row = gen.PAGE_RIG_SETTINGS * 128 + gen.RIG_VOLUME_NUMBER
    assert state.apply_cbor_text(numeric_row, "x") == ApplyOutcome.empty()
    assert state.rig.volume == 100
    assert state.apply_update(Update(Channel.STREAM, Phase.LIVE, 1, Text("x"))).events == [
        StringTag(1),
        RigChanged(),
    ]
    out = state.apply_update(Update(Channel.STREAM, Phase.LIVE, 1, Num(5)))
    assert out == ApplyOutcome.fast(ParamChanged(0, 1, 5))
    assert state.rig.name == "x"


def test_sensitive_text_is_redacted_before_it_is_stored():
    """A device secret the dump volunteers in the clear is replaced by the
    placeholder in the row decoder itself, so no path past it can see it; an
    ordinary string comes through as it is."""
    secret = Text("hunter2")
    assert _decode(gen.Kind.TEXT, secret, gen.SENSITIVE_ADDRESSES[0]) == gen.REDACTED_PLACEHOLDER
    assert _decode(gen.Kind.TEXT, secret, gen.STRING_RIG_NAME) == "hunter2"


def test_wire_authority_refuses_only_the_control_copy():
    """Rule 3 as a table: a ``stream`` row drops the control channel's copy and
    nothing else is refused -- a ``control`` row takes the stream's value, and
    a ``both`` row takes either wire's."""
    beat = gen.PAGE_REALTIME * 128 + gen.BEAT_PULSE_NUMBER
    tempo = gen.PAGE_RIG_SETTINGS * 128 + gen.TEMPO_NUMBER
    assert _routes.lookup(beat).wire is gen.Wire.STREAM
    assert _routes.lookup(gen.MORPH_ADDRESS).wire is gen.Wire.CONTROL
    assert _routes.lookup(tempo).wire is gen.Wire.BOTH
    cases = [
        (beat, Channel.CONTROL, True),
        (beat, Channel.STREAM, False),
        (gen.MORPH_ADDRESS, Channel.STREAM, False),
        (gen.MORPH_ADDRESS, Channel.CONTROL, False),
        (tempo, Channel.STREAM, False),
        (tempo, Channel.CONTROL, False),
    ]
    for address, source, refused in cases:
        state = DeviceState()
        out = state.apply_update(Update(source, Phase.LIVE, address, Num(1)))
        assert (out == ApplyOutcome.empty()) is refused, (address, source)
