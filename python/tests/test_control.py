"""The 7-bit control vocabulary."""

from __future__ import annotations

import pytest

from libkp import control as c
from libkp.errors import UnknownSlotError


def test_continuous_controller_bytes():
    assert c.Gain(64).message(0) == bytes([0xB0, 72, 64])
    assert c.DelayMix(10).message(0) == bytes([0xB0, 68, 10])
    assert c.ReverbTime(127).message(0) == bytes([0xB0, 71, 127])
    assert c.MorphPedal(0).message(0) == bytes([0xB0, 11, 0])


def test_tap_tempo_is_cc30():
    assert c.TapTempo().message(0) == bytes([0xB0, 30, 1])


def test_tuner_mode_open_is_cc31_value_1():
    assert c.TunerMode(True).message(0) == bytes([0xB0, 31, 1])
    assert c.TunerMode(False).message(0) == bytes([0xB0, 31, 0])


@pytest.mark.parametrize("n,controller", [(3, 52), (1, 50), (5, 54), (0, 50), (99, 54)])
def test_load_slot_maps_to_cc50_plus_and_clamps(n, controller):
    assert c.LoadSlot(n).message(0) == bytes([0xB0, controller, 1])


@pytest.mark.parametrize("n,controller", [(4, 78), (1, 75), (0, 75), (9, 78)])
def test_effect_button_maps_to_cc75_plus_and_clamps(n, controller):
    assert c.EffectButton(n).message(0) == bytes([0xB0, controller, 1])


def test_morph_button_rise_is_cc80_value_1():
    assert c.MorphButton(True).message(0) == bytes([0xB0, 80, 1])
    assert c.MorphButton(False).message(0) == bytes([0xB0, 80, 0])


def test_slot_enable_uses_the_spillover_cc_for_dly_and_rev():
    assert c.SlotEnable("REV", True).message(0) == bytes([0xB0, 29, 1])
    assert c.SlotEnable("DLY", False).message(0) == bytes([0xB0, 27, 0])
    assert c.SlotEnable("A", True).message(0) == bytes([0xB0, 17, 1])
    assert c.SlotEnable("X", True).message(0) == bytes([0xB0, 22, 1])
    assert c.SlotEnable(c.ModuleSlot.MOD, False).message(0) == bytes([0xB0, 24, 0])


def test_slot_enable_accepts_any_case():
    assert c.SlotEnable("rev", True).message(0) == c.SlotEnable("REV", True).message(0)


def test_switch_variants_emit_one_or_zero():
    assert c.RotaryFast(True).message(0) == bytes([0xB0, 33, 1])
    assert c.RotaryFast(False).message(0) == bytes([0xB0, 33, 0])
    assert c.DelayInfinity(True).message(0) == bytes([0xB0, 34, 1])
    assert c.Freeze(True).message(0) == bytes([0xB0, 35, 1])
    assert c.ToggleAllModules().message(0) == bytes([0xB0, 16, 1])
    assert c.Up().message(0) == bytes([0xB0, 48, 1])
    assert c.Down().message(0) == bytes([0xB0, 49, 1])


def test_program_change_bytes():
    assert c.ProgramChange(5).message(0) == bytes([0xC0, 5])
    assert c.program_change(0, 5) == bytes([0xC0, 5])
    assert c.program_change(2, 0) == bytes([0xC2, 0])


def test_bank_select_is_cc0_then_cc32():
    assert c.BankSelect(0, 3).message(0) == bytes([0xB0, 0, 0, 0xB0, 32, 3])


def test_bank_preselect_passes_the_value():
    assert c.BankPreselect(3).message(0) == bytes([0xB0, 47, 3])


def test_channel_is_masked_into_the_status_byte():
    assert c.Gain(64).message(15) == bytes([0xBF, 72, 64])
    assert c.Gain(64).message(16) == bytes([0xB0, 72, 64])
    assert c.ProgramChange(0).message(15) == bytes([0xCF, 0])


def test_seven_bit_values_are_masked():
    assert c.WahPedal(200).message(0) == bytes([0xB0, 1, 72])
    assert c.ProgramChange(200).message(0) == bytes([0xC0, 72])
    assert c.BankSelect(130, 129).message(0) == bytes([0xB0, 0, 2, 0xB0, 32, 1])


@pytest.mark.parametrize(
    "slot,cc",
    [("A", 17), ("B", 18), ("C", 19), ("D", 20), ("X", 22), ("MOD", 24), ("DLY", 27), ("REV", 29)],
)
def test_slot_enable_cc_table(slot, cc):
    assert c.slot_enable_cc(slot) == cc


def test_control_from_op_matches_the_direct_constructors():
    assert c.control_from_op("gain", value=64) == c.Gain(64)
    assert c.control_from_op("slot_enable", slot="REV", on=True) == c.SlotEnable("REV", True)
    assert c.control_from_op("bank_select", msb=1, lsb=2) == c.BankSelect(1, 2)
    with pytest.raises(KeyError):
        c.control_from_op("not_a_control")


def test_default_channel_is_zero():
    assert c.Gain(64).message() == c.Gain(64).message(0)


def test_distinct_control_types_do_not_compare_equal():
    assert c.Gain(64) != c.DelayMix(64)


def test_unknown_slot_raises_a_libkp_error():
    """Every slot-name entry point rejects a bad name the same way."""
    for call in (
        lambda: c.slot_enable_cc("nope"),
        lambda: c.ModuleSlot.parse("nope"),
        lambda: c.SlotEnable("nope", True).message(0),
        lambda: c.control_from_op("slot_enable", slot="nope", on=True).message(0),
    ):
        with pytest.raises(UnknownSlotError):
            call()
