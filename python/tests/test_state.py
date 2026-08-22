"""The device-state tree and its pure decode routing."""

from __future__ import annotations

import pytest

from libkp import _generated as gen
from libkp import nrpn
from libkp.nrpn import DEVICE_OMNI, PAGE_STRINGS, set_single, sysex, u14, u14_split
from libkp.state import (
    ApplyOutcome,
    BeatPulse,
    Connection,
    DeviceState,
    EffectChanged,
    MorphChanged,
    ParamChanged,
    RealtimeStatus,
    RenderedString,
    RigChanged,
    Status,
    StringTag,
    TempoBpm,
    TunerDeviance,
    TunerNote,
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

    state.apply(sysex(0x00, 0x7F, FN_STRING, PAGE_STRINGS, 10, b"JCM"))
    state.apply(sysex(0x00, 0x7F, FN_STRING, PAGE_STRINGS, 32, b"412"))
    assert state.amp.name == "JCM"
    assert state.cabinet.name == "412"

    # An untracked string tag leaves the snapshot unchanged.
    out = state.apply(sysex(0x00, 0x7F, FN_STRING, PAGE_STRINGS, 99, b"x"))
    assert not out.slow_changed
    assert out.events == [StringTag(99)]


def test_ext_string_recovers_amp_name():
    state = DeviceState()
    out = state.apply(ext_string(PAGE_STRINGS, 10, b"JCM800"))
    assert state.amp.name == "JCM800"
    assert out.slow_changed
    assert out.events == [StringTag(10)]


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
    # $06 extended params are not decoded.
    assert state.apply(sysex(0x00, 0x00, 0x06, 0x0A, 4, b"\x00\x00")) == ApplyOutcome.empty()
