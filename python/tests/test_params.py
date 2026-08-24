"""Offline parameter-name lookups."""

from __future__ import annotations

import pytest

from libkp import params


def test_input_section_address_resolves():
    assert params.param_name(0x09, 0x03) == "Noise Gate Intensity"
    assert params.describe(0x09, 0x03) == "Input Section: Noise Gate Intensity"


@pytest.mark.parametrize("page", [0x32, 0x33, 0x34, 0x35, 0x38, 0x3A, 0x3C, 0x3D])
def test_effect_modules_share_the_map(page):
    assert params.is_effect_page(page)
    assert params.param_name(page, 0) == "Type"
    assert params.param_name(page, 4) == "Mix"


def test_non_effect_pages_are_not_effect_pages():
    assert not params.is_effect_page(0x04)
    assert not params.is_effect_page(0x7C)


def test_key_addresses():
    assert params.param_name(0x04, 0) == "Tempo bpm"
    assert params.param_name(0x0A, 4) == "Gain"
    assert params.param_name(0x7F, 0) == "Main Output Volume"
    assert params.param_name(0x7D, 88) == "Looper Record/Playback/Overdub"
    assert params.param_name(0x00, 1) == "Rig Name"
    # Gaps stay unknown.
    assert params.param_name(0x04, 5) is None
    assert params.param_name(0x7D, 112) is None
    assert params.param_name(0x99, 0) is None


def test_effect_types():
    assert params.effect_type_name(0) == "empty"
    assert params.effect_type_name(32) == "Kemper Drive"
    assert params.effect_type_name(193) == "Spring Reverb"
    assert params.effect_type_name(5) is None


def test_effect_categories():
    assert params.effect_category_name(16) == "Wah"
    assert params.effect_category_name(17) == "Shaper"
    # A type with no name still resolves to its block.
    assert params.effect_category_name(76) == "Modulation"
    assert params.effect_category_name(0) is None
    assert params.effect_category_name(300) is None


def test_realtime_page_addresses():
    assert params.page_name(0x7C) == "Realtime/Meters"
    assert params.param_name(0x7C, 0x4E) == "Tuner Strobe Segment (phase-low)"
    assert params.param_name(0x7C, 81) == "Tuner Strobe Phase"
    assert params.param_name(0x7C, 84) == "Meter: Rig Output Level"
    assert params.param_name(0x7C, 88) == "Meter: (unused v10)"
    assert params.param_name(0x7C, 0) == "Tempo/Beat Pulse"
    assert params.param_name(0x7C, 15) == "Tuner Deviance"
    assert params.describe(0x7C, 0x4E) == "Realtime/Meters: Tuner Strobe Segment (phase-low)"


def test_undocumented_addresses():
    assert params.param_name(0x05, 6) == "Fixed Noise Gate On/Off"
    assert params.param_name(0x7D, 84) == "Tuner Note"
    assert params.param_name(0x7F, 126) == "Tuner Mode State"


def test_page_zero_is_dual_use():
    assert params.page0_numeric_name(0x77) == "Morph Position"
    assert params.page0_numeric_name(0x50) == "Morph Button"
    assert params.describe_numeric(0x00, 0x77) == "Page 0: Morph Position"
    # 0x0B is a string tag, and is *not* the morph -- see test_morph_is_not_at_0x0b.
    assert params.page0_numeric_name(0x0B) is None
    assert params.describe(0x00, 0x11) == "String Tags: Amp Author"
    # An unnamed page-0 number still describes, by number.
    assert params.describe_numeric(0x00, 0x60) == "Page 0: #96 (0x60)"
    # Off page 0 the two describes agree.
    assert params.describe_numeric(0x0A, 4) == params.describe(0x0A, 4)


def test_describe_falls_back_for_unknown_pages_and_numbers():
    assert params.describe(0x99, 5) == "page 0x99 #5 (0x05)"
    assert params.describe(0x04, 5) == "Rig Settings: #5 (0x05)"


def test_effect_slot_helpers():
    assert params.EFFECT_SLOT_NAMES == ("A", "B", "C", "D", "X", "MOD", "DLY", "REV")
    assert params.effect_slot_page("rev") == 0x3D
    assert params.effect_slot_page("A") == 0x32
    assert params.effect_slot_page("nope") is None
    assert params.effect_slot_name(0x3A) == "MOD"
    assert params.effect_slot_name(0x04) is None
    assert params.effect_slot_index(0x3D) == 7
    assert params.effect_slot_index(0x04) is None


def test_function_names():
    assert params.function_name(0x01) == "single-param"
    assert params.function_name(0x7E) == "beacon"
    assert params.function_name(0x55) is None
