"""Typed parameter descriptors and value formatting."""

from __future__ import annotations

from libkp.registry import ParamKind, descriptor, format_value


def test_known_addresses_resolve():
    gain = descriptor(0x0A, 4)
    assert (gain.name, gain.kind) == ("Gain", ParamKind.CONTINUOUS)

    amp_onoff = descriptor(0x0A, 2)
    assert (amp_onoff.name, amp_onoff.kind) == ("On/Off", ParamKind.SWITCH)

    rig_volume = descriptor(0x04, 1)
    assert (rig_volume.name, rig_volume.kind) == ("Rig Volume", ParamKind.CONTINUOUS)

    tempo_enable = descriptor(0x04, 2)
    assert (tempo_enable.name, tempo_enable.kind) == ("Tempo Enable", ParamKind.SWITCH)

    main_volume = descriptor(0x7F, 0)
    assert (main_volume.name, main_volume.kind) == (
        "Main Output Volume",
        ParamKind.CONTINUOUS,
    )


def test_effect_slots_type_only_the_common_four():
    kind = descriptor(0x3D, 0)
    assert (kind.name, kind.kind) == ("Type", ParamKind.ENUM)
    assert descriptor(0x3D, 3).kind is ParamKind.SWITCH
    assert descriptor(0x3D, 4).name == "Mix"
    assert descriptor(0x3D, 6).name == "Volume"
    # An effect number outside the common four is not typed here.
    assert descriptor(0x3D, 7) is None
    # Undocumented / gap pages have no descriptor.
    assert descriptor(0x7C, 84) is None
    assert descriptor(0x99, 0) is None
    assert descriptor(0x0A, 99) is None


def test_format_switch_enum_and_continuous():
    switch = descriptor(0x0A, 2)
    assert format_value(switch, 1) == "On"
    assert format_value(switch, 0) == "Off"

    kind = descriptor(0x32, 0)
    assert format_value(kind, 32) == "Kemper Drive"
    assert format_value(kind, 5) == "type 5"

    continuous = descriptor(0x0A, 4)
    assert format_value(continuous, 6925) == "42.3%"
    assert format_value(continuous, 0) == "0.0%"
    assert format_value(continuous, 16383) == "100.0%"


def test_descriptor_records_its_address():
    desc = descriptor(0x0B, 7)
    assert (desc.page, desc.number, desc.name) == (0x0B, 7, "Presence")
    assert desc.unit is None
    assert desc.enum_names is None
