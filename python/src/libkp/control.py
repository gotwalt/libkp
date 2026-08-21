"""Typed MIDI control-message vocabulary for the Profiler.

These are the **7-bit** channel-voice messages — Control Change (CC), Program
Change (PC), and Bank Select — that the Profiler responds to, distinct from the
14-bit NRPN-over-SysEx traffic in :mod:`libkp.nrpn`. Every CC number comes from
the Kemper MIDI Parameter Documentation, cross-checked against PySwitch.

Two ways to use it:

- the named ``CC_*`` constants, for hand-building messages, and
- the :class:`Control` classes plus :meth:`Control.message`, a discoverable,
  misuse-resistant API that clamps ranges and emits the raw MIDI bytes.

Everything is pure and offline. CC bytes are produced via
:func:`libkp.nrpn.control_change`, which masks channel/controller/value to
7 bits.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, ClassVar

from . import _generated as gen
from .errors import UnknownSlotError
from .nrpn import control_change, program_change

__all__ = [
    # Continuous controllers
    "CC_WAH_PEDAL",
    "CC_PITCH_PEDAL",
    "CC_VOLUME_PEDAL",
    "CC_PANORAMA",
    "CC_MORPH_PEDAL",
    "CC_DELAY_MIX",
    "CC_DELAY_FEEDBACK",
    "CC_REVERB_MIX",
    "CC_REVERB_TIME",
    "CC_GAIN",
    "CC_MONITOR_VOLUME",
    # Switches
    "CC_TOGGLE_ALL_MODULES",
    "CC_MODULE_A",
    "CC_MODULE_B",
    "CC_MODULE_C",
    "CC_MODULE_D",
    "CC_MODULE_X",
    "CC_MODULE_MOD",
    "CC_MODULE_DLY",
    "CC_MODULE_DLY_NO_SPILL",
    "CC_MODULE_REV",
    "CC_MODULE_REV_NO_SPILL",
    "CC_TAP_TEMPO",
    "CC_TUNER_MODE",
    "CC_ROTARY_SPEED",
    "CC_DELAY_INFINITY",
    "CC_FREEZE",
    "CC_BANK_PRESELECT",
    "CC_UP",
    "CC_DOWN",
    "CC_LOAD_SLOT_1",
    "CC_LOAD_SLOT_2",
    "CC_LOAD_SLOT_3",
    "CC_LOAD_SLOT_4",
    "CC_LOAD_SLOT_5",
    "CC_EFFECT_BUTTON_I",
    "CC_EFFECT_BUTTON_II",
    "CC_EFFECT_BUTTON_III",
    "CC_EFFECT_BUTTON_IIII",
    "CC_MORPH_BUTTON",
    "CC_BANK_SELECT_MSB",
    "CC_BANK_SELECT_LSB",
    "PROGRAM_CHANGE_STATUS",
    # API
    "ModuleSlot",
    "slot_enable_cc",
    "program_change",
    "Control",
    "WahPedal",
    "PitchPedal",
    "VolumePedal",
    "Panorama",
    "MorphPedal",
    "Gain",
    "DelayMix",
    "DelayFeedback",
    "ReverbMix",
    "ReverbTime",
    "MonitorVolume",
    "ToggleAllModules",
    "SlotEnable",
    "RotaryFast",
    "DelayInfinity",
    "Freeze",
    "TapTempo",
    "TunerMode",
    "BankPreselect",
    "Up",
    "Down",
    "LoadSlot",
    "EffectButton",
    "MorphButton",
    "ProgramChange",
    "BankSelect",
    "CONTROL_OPS",
    "control_from_op",
]

# ---------------------------------------------------------------------------
# Continuous controllers
# ---------------------------------------------------------------------------

#: CC1 — Wah Pedal (continuous, 0–127).
CC_WAH_PEDAL: int = gen.CC_WAH_PEDAL
#: CC4 — Pitch Pedal (continuous, 0–127).
CC_PITCH_PEDAL: int = gen.CC_PITCH_PEDAL
#: CC7 — Volume Pedal (continuous, 0–127).
CC_VOLUME_PEDAL: int = gen.CC_VOLUME_PEDAL
#: CC10 — Panorama (continuous, 0–127).
CC_PANORAMA: int = gen.CC_PANORAMA
#: CC11 — Morph Pedal (continuous, 0–127).
CC_MORPH_PEDAL: int = gen.CC_MORPH_PEDAL
#: CC68 — Delay Mix (continuous, 0–127).
CC_DELAY_MIX: int = gen.CC_DELAY_MIX
#: CC69 — Delay Feedback (continuous, 0–127).
CC_DELAY_FEEDBACK: int = gen.CC_DELAY_FEEDBACK
#: CC70 — Reverb Mix (continuous, 0–127).
CC_REVERB_MIX: int = gen.CC_REVERB_MIX
#: CC71 — Reverb Time (continuous, 0–127).
CC_REVERB_TIME: int = gen.CC_REVERB_TIME
#: CC72 — Gain (continuous, 0–127).
CC_GAIN: int = gen.CC_GAIN
#: CC73 — Monitor (Output) Volume (continuous, 0–127).
CC_MONITOR_VOLUME: int = gen.CC_MONITOR_VOLUME

# ---------------------------------------------------------------------------
# Switches
# ---------------------------------------------------------------------------

#: CC16 — toggle all modules (A–REV) on/off.
CC_TOGGLE_ALL_MODULES: int = gen.CC_TOGGLE_ALL_MODULES
#: CC17 — module A on/off (1 on / 0 off).
CC_MODULE_A: int = gen.CC_MODULE_A
#: CC18 — module B on/off (1 on / 0 off).
CC_MODULE_B: int = gen.CC_MODULE_B
#: CC19 — module C on/off (1 on / 0 off).
CC_MODULE_C: int = gen.CC_MODULE_C
#: CC20 — module D on/off (1 on / 0 off).
CC_MODULE_D: int = gen.CC_MODULE_D
#: CC22 — module X on/off (1 on / 0 off).
CC_MODULE_X: int = gen.CC_MODULE_X
#: CC24 — module MOD on/off (1 on / 0 off).
CC_MODULE_MOD: int = gen.CC_MODULE_MOD
#: CC26 — module DLY off, **without** spillover.
CC_MODULE_DLY_NO_SPILL: int = gen.CC_MODULE_DLY_NO_SPILL
#: CC27 — module DLY on/off, **with** spillover.
CC_MODULE_DLY: int = gen.CC_MODULE_DLY
#: CC28 — module REV off, **without** spillover.
CC_MODULE_REV_NO_SPILL: int = gen.CC_MODULE_REV_NO_SPILL
#: CC29 — module REV on/off, **with** spillover.
CC_MODULE_REV: int = gen.CC_MODULE_REV
#: CC30 — Tap Tempo (any value taps; 1/0 also toggles the Beat Scanner).
CC_TAP_TEMPO: int = gen.CC_TAP_TEMPO
#: CC31 — Tuner Mode (1 open / 0 close).
CC_TUNER_MODE: int = gen.CC_TUNER_MODE
#: CC33 — Rotary speaker speed (1 fast / 0 slow).
CC_ROTARY_SPEED: int = gen.CC_ROTARY_SPEED
#: CC34 — Delay Infinity (1 on / 0 off).
CC_DELAY_INFINITY: int = gen.CC_DELAY_INFINITY
#: CC35 — Delay + Reverb Freeze (1 on / 0 off).
CC_FREEZE: int = gen.CC_FREEZE
#: CC47 — Bank/Performance preselect (value = bank − 1).
CC_BANK_PRESELECT: int = gen.CC_BANK_PRESELECT
#: CC48 — Performance/Rig up.
CC_UP: int = gen.CC_UP
#: CC49 — Performance/Rig down.
CC_DOWN: int = gen.CC_DOWN
#: CC50–CC54 — load performance Slot 1–5 (value 1).
CC_LOAD_SLOT_1: int = gen.CC_LOAD_SLOT_1
CC_LOAD_SLOT_2: int = gen.CC_LOAD_SLOT_2
CC_LOAD_SLOT_3: int = gen.CC_LOAD_SLOT_3
CC_LOAD_SLOT_4: int = gen.CC_LOAD_SLOT_4
CC_LOAD_SLOT_5: int = gen.CC_LOAD_SLOT_5
#: CC75–CC78 — Effect Buttons I–IIII.
CC_EFFECT_BUTTON_I: int = gen.CC_EFFECT_BUTTON_I
CC_EFFECT_BUTTON_II: int = gen.CC_EFFECT_BUTTON_II
CC_EFFECT_BUTTON_III: int = gen.CC_EFFECT_BUTTON_III
CC_EFFECT_BUTTON_IIII: int = gen.CC_EFFECT_BUTTON_IIII
#: CC80 — Morph button (1 rise / 0 fall).
CC_MORPH_BUTTON: int = gen.CC_MORPH_BUTTON

#: CC0 — Bank Select MSB.
CC_BANK_SELECT_MSB: int = gen.CC_BANK_SELECT_MSB
#: CC32 — Bank Select LSB.
CC_BANK_SELECT_LSB: int = gen.CC_BANK_SELECT_LSB

#: The MIDI Program Change status nibble (``0xC0 | channel``).
PROGRAM_CHANGE_STATUS: int = gen.PROGRAM_CHANGE_STATUS


class ModuleSlot(str, Enum):
    """One of the eight Profiler effect-module slots."""

    A = "A"
    B = "B"
    C = "C"
    D = "D"
    X = "X"
    MOD = "MOD"
    DLY = "DLY"
    REV = "REV"

    @classmethod
    def parse(cls, name: str | ModuleSlot) -> ModuleSlot:
        """Resolve a slot name case-insensitively (``"rev"``, ``"Dly"``, …).

        Raises :class:`~libkp.errors.UnknownSlotError` — a
        :class:`~libkp.errors.LibKPError` — for a name outside the eight slots,
        matching what :meth:`libkp.model.DeviceModel.set_effect_enabled` raises.
        """
        if isinstance(name, cls):
            return name
        try:
            return cls(str(name).upper())
        except ValueError as exc:
            raise UnknownSlotError(str(name)) from exc


def slot_enable_cc(slot: str | ModuleSlot) -> int:
    """The on/off CC for an effect slot.

    DLY and REV return their **with-spillover** CCs (27/29); the no-spillover
    variants (26/28) are available as :data:`CC_MODULE_DLY_NO_SPILL` /
    :data:`CC_MODULE_REV_NO_SPILL`.
    """
    return gen.SLOT_ENABLE_CC[ModuleSlot.parse(slot).value]


def _sw(on: bool) -> int:
    """Map a switch to its MIDI value: ``True`` → 1, ``False`` → 0."""
    return 1 if on else 0


@dataclass(frozen=True)
class Control:
    """Base class for a Kemper control action.

    Subclasses implement :meth:`message`, which renders the raw MIDI bytes for a
    given channel. Value bytes are masked to 7 bits; indexed variants clamp to
    their valid ranges.
    """

    def message(self, channel: int = 0) -> bytes:
        """Build the raw MIDI bytes for this control on ``channel`` (0-15)."""
        raise NotImplementedError


@dataclass(frozen=True)
class _CCValue(Control):
    """A single Control Change whose value passes straight through."""

    value: int

    #: The CC number this control writes to.
    CONTROLLER: ClassVar[int] = 0

    def message(self, channel: int = 0) -> bytes:
        return control_change(channel, self.CONTROLLER, self.value)


@dataclass(frozen=True)
class _CCSwitch(Control):
    """A single Control Change emitting 1 (on) or 0 (off)."""

    on: bool

    #: The CC number this control writes to.
    CONTROLLER: ClassVar[int] = 0

    def message(self, channel: int = 0) -> bytes:
        return control_change(channel, self.CONTROLLER, _sw(self.on))


@dataclass(frozen=True)
class _CCTrigger(Control):
    """A momentary Control Change that always emits value 1."""

    #: The CC number this control writes to.
    CONTROLLER: ClassVar[int] = 0

    def message(self, channel: int = 0) -> bytes:
        return control_change(channel, self.CONTROLLER, 1)


# Continuous controllers ----------------------------------------------------


@dataclass(frozen=True)
class WahPedal(_CCValue):
    """Wah pedal position (CC1), 0-127."""

    CONTROLLER: ClassVar[int] = CC_WAH_PEDAL


@dataclass(frozen=True)
class PitchPedal(_CCValue):
    """Pitch pedal position (CC4), 0-127."""

    CONTROLLER: ClassVar[int] = CC_PITCH_PEDAL


@dataclass(frozen=True)
class VolumePedal(_CCValue):
    """Volume pedal position (CC7), 0-127."""

    CONTROLLER: ClassVar[int] = CC_VOLUME_PEDAL


@dataclass(frozen=True)
class Panorama(_CCValue):
    """Panorama (CC10), 0-127."""

    CONTROLLER: ClassVar[int] = CC_PANORAMA


@dataclass(frozen=True)
class MorphPedal(_CCValue):
    """Morph pedal position (CC11), 0-127."""

    CONTROLLER: ClassVar[int] = CC_MORPH_PEDAL


@dataclass(frozen=True)
class Gain(_CCValue):
    """Gain (CC72), 0-127."""

    CONTROLLER: ClassVar[int] = CC_GAIN


@dataclass(frozen=True)
class DelayMix(_CCValue):
    """Delay Mix (CC68), 0-127."""

    CONTROLLER: ClassVar[int] = CC_DELAY_MIX


@dataclass(frozen=True)
class DelayFeedback(_CCValue):
    """Delay Feedback (CC69), 0-127."""

    CONTROLLER: ClassVar[int] = CC_DELAY_FEEDBACK


@dataclass(frozen=True)
class ReverbMix(_CCValue):
    """Reverb Mix (CC70), 0-127."""

    CONTROLLER: ClassVar[int] = CC_REVERB_MIX


@dataclass(frozen=True)
class ReverbTime(_CCValue):
    """Reverb Time (CC71), 0-127."""

    CONTROLLER: ClassVar[int] = CC_REVERB_TIME


@dataclass(frozen=True)
class MonitorVolume(_CCValue):
    """Monitor (Output) Volume (CC73), 0-127."""

    CONTROLLER: ClassVar[int] = CC_MONITOR_VOLUME


@dataclass(frozen=True)
class BankPreselect(_CCValue):
    """Bank/Performance preselect (CC47). Value is the bank number minus 1."""

    CONTROLLER: ClassVar[int] = CC_BANK_PRESELECT


# Switches ------------------------------------------------------------------


@dataclass(frozen=True)
class RotaryFast(_CCSwitch):
    """Rotary speaker speed (CC33): ``True`` fast / ``False`` slow."""

    CONTROLLER: ClassVar[int] = CC_ROTARY_SPEED


@dataclass(frozen=True)
class DelayInfinity(_CCSwitch):
    """Delay Infinity (CC34): ``True`` on / ``False`` off."""

    CONTROLLER: ClassVar[int] = CC_DELAY_INFINITY


@dataclass(frozen=True)
class Freeze(_CCSwitch):
    """Delay + Reverb Freeze (CC35): ``True`` on / ``False`` off."""

    CONTROLLER: ClassVar[int] = CC_FREEZE


@dataclass(frozen=True)
class TunerMode(_CCSwitch):
    """Tuner Mode (CC31): ``True`` open / ``False`` close."""

    CONTROLLER: ClassVar[int] = CC_TUNER_MODE


@dataclass(frozen=True)
class MorphButton(_CCSwitch):
    """Morph button (CC80): ``True`` rise / ``False`` fall."""

    CONTROLLER: ClassVar[int] = CC_MORPH_BUTTON


# Triggers ------------------------------------------------------------------


@dataclass(frozen=True)
class ToggleAllModules(_CCTrigger):
    """Toggle every module A-REV on/off (CC16)."""

    CONTROLLER: ClassVar[int] = CC_TOGGLE_ALL_MODULES


@dataclass(frozen=True)
class TapTempo(_CCTrigger):
    """Tap Tempo (CC30). Any value taps; this emits value 1."""

    CONTROLLER: ClassVar[int] = CC_TAP_TEMPO


@dataclass(frozen=True)
class Up(_CCTrigger):
    """Performance/Rig up (CC48)."""

    CONTROLLER: ClassVar[int] = CC_UP


@dataclass(frozen=True)
class Down(_CCTrigger):
    """Performance/Rig down (CC49)."""

    CONTROLLER: ClassVar[int] = CC_DOWN


# Compound / indexed --------------------------------------------------------


@dataclass(frozen=True)
class SlotEnable(Control):
    """Enable/disable one effect module.

    ``slot`` maps to its enable CC via :func:`slot_enable_cc`; DLY/REV use the
    **with-spillover** CC.
    """

    slot: str | ModuleSlot
    on: bool

    def message(self, channel: int = 0) -> bytes:
        return control_change(channel, slot_enable_cc(self.slot), _sw(self.on))


@dataclass(frozen=True)
class LoadSlot(Control):
    """Load a performance slot 1-5 (CC50-54, value 1). ``n`` is clamped to 1..5."""

    n: int

    def message(self, channel: int = 0) -> bytes:
        n = min(max(self.n, 1), 5)
        return control_change(channel, CC_LOAD_SLOT_1 + (n - 1), 1)


@dataclass(frozen=True)
class EffectButton(Control):
    """Press an Effect Button I-IIII (CC75-78, value 1). ``n`` is clamped to 1..4."""

    n: int

    def message(self, channel: int = 0) -> bytes:
        n = min(max(self.n, 1), 4)
        return control_change(channel, CC_EFFECT_BUTTON_I + (n - 1), 1)


@dataclass(frozen=True)
class ProgramChange(Control):
    """Program Change (``0xC0|ch, program``). ``program`` is masked to 7 bits."""

    program: int

    def message(self, channel: int = 0) -> bytes:
        return program_change(channel, self.program)


@dataclass(frozen=True)
class BankSelect(Control):
    """Bank Select: the CC0 (MSB) + CC32 (LSB) pair, concatenated."""

    msb: int
    lsb: int

    def message(self, channel: int = 0) -> bytes:
        return control_change(channel, CC_BANK_SELECT_MSB, self.msb) + control_change(
            channel, CC_BANK_SELECT_LSB, self.lsb
        )


#: Stable op names → the :class:`Control` they build. The op names match the
#: shared conformance vectors, so a caller can drive the vocabulary from data.
CONTROL_OPS: dict[str, type[Control]] = {
    "wah_pedal": WahPedal,
    "pitch_pedal": PitchPedal,
    "volume_pedal": VolumePedal,
    "panorama": Panorama,
    "morph_pedal": MorphPedal,
    "gain": Gain,
    "delay_mix": DelayMix,
    "delay_feedback": DelayFeedback,
    "reverb_mix": ReverbMix,
    "reverb_time": ReverbTime,
    "monitor_volume": MonitorVolume,
    "bank_preselect": BankPreselect,
    "toggle_all_modules": ToggleAllModules,
    "slot_enable": SlotEnable,
    "rotary_fast": RotaryFast,
    "delay_infinity": DelayInfinity,
    "freeze": Freeze,
    "tap_tempo": TapTempo,
    "tuner_mode": TunerMode,
    "up": Up,
    "down": Down,
    "load_slot": LoadSlot,
    "effect_button": EffectButton,
    "morph_button": MorphButton,
    "program_change": ProgramChange,
    "bank_select": BankSelect,
}


def control_from_op(op: str, **params: Any) -> Control:
    """Build a :class:`Control` from a stable op name and keyword parameters.

    ``control_from_op("gain", value=64)`` is the same as ``Gain(64)``. Raises
    :class:`KeyError` for an unknown op.
    """
    return CONTROL_OPS[op](**params)
