"""The typed device-state tree — the **store snapshot** a UI binds to — and the
pure, network-free decode logic that fills it in.

Everything in the tree is plain data (dataclasses of scalars, strings and
``None``), cheap to copy for each snapshot the store emits and easy to mirror
across an FFI boundary as value types.

:class:`DeviceState` is the root. :meth:`DeviceState.apply` decodes ONE
already-unframed MIDI message and returns an :class:`ApplyOutcome`: the granular
:class:`DeviceEvent` list plus whether any *slow* field changed. It does no I/O,
so tests drive it with synthesized messages. The async handle that owns a socket
and feeds this is :class:`libkp.model.DeviceModel`.

FAST vs SLOW
------------

**FAST** = the meter :class:`RealtimeStatus` block, the beat pulse, and tuner
deviance — high-rate values a UI polls per animation frame. **SLOW** =
everything else (rig / amp / cab / effect / output / tempo / morph / tuner-note /
connection), which drives the coalesced snapshot stream.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from enum import Enum

from . import _generated as gen
from . import nrpn, params
from .nrpn import NrpnHeader, u14

__all__ = [
    "Connection",
    "RealtimeStatus",
    "Rig",
    "Amp",
    "Cabinet",
    "Effect",
    "Tuner",
    "Output",
    "BankSlot",
    "Bank",
    "DeviceState",
    "DeviceEvent",
    "RigChanged",
    "StringTag",
    "BankPreview",
    "EffectChanged",
    "ParamChanged",
    "Status",
    "BeatPulse",
    "TempoBpm",
    "MorphButton",
    "MorphChanged",
    "TunerDeviance",
    "TunerNote",
    "RenderedString",
    "CurrentPosition",
    "Connected",
    "Disconnected",
    "ApplyOutcome",
]


class Connection(Enum):
    """Whether a live session to the Profiler is currently open."""

    #: A session is open and the stream is being ingested.
    CONNECTED = "connected"
    #: No session (initial state, or the device closed the connection).
    DISCONNECTED = "disconnected"


# ---------------------------------------------------------------------------
# Realtime status (the FAST lane)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RealtimeStatus:
    """A decoded realtime status / meter-block frame.

    The eleven 14-bit values the stream pushes at NRPN ``0x7C/78..88`` (function
    ``$02``, page ``0x7C``, number ``0x4E``). Field identities come from observed
    experimentation and are described by :data:`libkp._generated.METER_FIELDS`.
    """

    #: The eleven raw 14-bit values (v0..v10) in NRPN order.
    raw: tuple[int, ...] = (0,) * gen.METER_COUNT

    @classmethod
    def from_values(cls, vals: bytes) -> RealtimeStatus:
        """Decode an 11x14-bit value block (MSB/LSB pairs) into a status frame.

        Missing trailing values default to 0; extra bytes are ignored.
        """
        raw = [0] * gen.METER_COUNT
        for i in range(min(len(vals) // 2, gen.METER_COUNT)):
            raw[i] = u14(vals[2 * i], vals[2 * i + 1])
        return cls(raw=tuple(raw))

    @property
    def strobe_phase(self) -> int:
        """Tuner strobe phase (v3): a wrapping 0–16383 phase whose rotation rate
        tracks pitch deviance (stationary = in tune)."""
        return self.raw[gen.STROBE_PHASE_INDEX]

    @property
    def strobe_segments(self) -> tuple[int, ...]:
        """The three strobe display-segment drivers (v0..v2)."""
        return tuple(self.raw[i] for i in gen.STROBE_SEGMENT_INDICES)

    @property
    def strobe_active(self) -> bool:
        """True while the tuner is tracking a pitch (any strobe field non-zero)."""
        return any(self.raw[i] for i in (*gen.STROBE_SEGMENT_INDICES, gen.STROBE_PHASE_INDEX))

    @property
    def stack_level(self) -> int:
        """Stack tap level (v4): pre-rig-volume amplitude."""
        return self.raw[4]

    @property
    def stack_power(self) -> int:
        """Stack power (v5): roughly the square of :attr:`stack_level`."""
        return self.raw[5]

    @property
    def rig_out_level(self) -> int:
        """Rig output level (v6): post-rig-volume amplitude."""
        return self.raw[6]

    @property
    def rig_out_power(self) -> int:
        """Rig output power (v7): roughly the square of :attr:`rig_out_level`."""
        return self.raw[7]

    @property
    def loudness(self) -> int:
        """Loudness (v9): slow RMS of the output."""
        return self.raw[9]

    def field(self, ident: str) -> int:
        """One meter value by its spec id (e.g. ``"loudness"``, ``"strobe_phase"``)."""
        for index, _number, fid, _name, _render in gen.METER_FIELDS:
            if fid == ident:
                return self.raw[index]
        raise KeyError(ident)


# ---------------------------------------------------------------------------
# The state tree
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Rig:
    """The loaded rig's metadata and rig-wide settings."""

    #: Rig Name (string tag 1).
    name: str | None = None
    #: Rig Author (string tag 2).
    author: str | None = None
    #: Rig Comment (string tag 4).
    comment: str | None = None
    #: Rig Creation Date (string tag 3).
    date: str | None = None
    #: Rig Volume (NRPN ``0x04/1``, 14-bit), once seen.
    volume: int | None = None
    #: Tempo in whole beats per minute (NRPN ``0x04/0``, wire value / 64).
    tempo_bpm: int | None = None


@dataclass(slots=True)
class Amp:
    """The amplifier block."""

    #: Amp Name (string tag 10).
    name: str | None = None
    #: On/Off state (NRPN ``0x0A/2``), once seen.
    on: bool | None = None
    #: Gain (NRPN ``0x0A/4``, 14-bit), once seen.
    gain: int | None = None


@dataclass(slots=True)
class Cabinet:
    """The cabinet block."""

    #: Cabinet Name (string tag 32).
    name: str | None = None
    #: On/Off state, once seen.
    on: bool | None = None


@dataclass(slots=True)
class Effect:
    """One effect slot's identity and state within the loaded rig."""

    #: Slot short name in signal-chain order (``"A"``..``"REV"``).
    slot: str
    #: The slot's address page.
    page: int
    #: Effect Type value (effect number 0), if known.
    kind: int | None = None
    #: On/Off state (effect number 3), if known.
    on: bool | None = None
    #: Dry/wet Mix (effect number 4, 14-bit), if known.
    mix: int | None = None

    @property
    def type_name(self) -> str | None:
        """The effect Type's human name, or ``None`` if unknown or unmapped."""
        return None if self.kind is None else params.effect_type_name(self.kind)

    @property
    def category_name(self) -> str | None:
        """The effect Type's category, or ``None`` if unknown or in no block."""
        return None if self.kind is None else params.effect_category_name(self.kind)

    @property
    def is_empty(self) -> bool:
        """True if the slot holds no effect (Type == 0, "empty")."""
        return self.kind == 0


@dataclass(slots=True)
class Tuner:
    """The tuner readout."""

    #: Detected note index (NRPN ``0x7D/0x54``, low 7 bits), once seen.
    note: int | None = None
    #: Pitch deviance (NRPN ``0x7C/0x0F``; 8192 = perfectly in tune), once seen.
    deviance: int | None = None

    @property
    def in_tune(self) -> bool | None:
        """Whether the detected pitch is inside the in-tune window, or ``None`` if
        no deviance has been seen yet."""
        if self.deviance is None:
            return None
        return abs(self.deviance - gen.TUNER_IN_TUNE_CENTER) <= gen.TUNER_IN_TUNE_WINDOW


@dataclass(slots=True)
class Output:
    """The global output volumes."""

    #: Main Output Volume (NRPN ``0x7F/0``, 14-bit), once seen.
    main_volume: int | None = None
    #: Headphone Output Volume (NRPN ``0x7F/1``, 14-bit), once seen. Driven 1:1 by
    #: the physical Master Volume knob.
    headphone_volume: int | None = None
    #: Monitor Output Volume (NRPN ``0x7F/2``, 14-bit), once seen. Driven 1:1 by
    #: the physical Master Volume knob.
    monitor_volume: int | None = None

    @property
    def master_volume(self) -> int | None:
        """The physical Master Volume knob's value.

        The knob is a potentiometer that drives Headphone (``0x7F/1``) and Monitor
        (``0x7F/2``) 1:1 under the default output routing, so report Headphone,
        falling back to Monitor. **Read-only**: the pot has no soft-takeover, so a
        written value is authoritative only until the knob next moves.
        """
        return self.headphone_volume if self.headphone_volume is not None else self.monitor_volume


@dataclass(slots=True)
class BankSlot:
    """One bank slot's preview names — the identity of one of the current bank's
    five rigs, as shown on the device's rig browser."""

    #: Rig name in this slot (Bank Preview number 0..4), once seen.
    rig_name: str | None = None
    #: The rig's amp name (Bank Preview number 5..9), once seen.
    amp_name: str | None = None
    #: The rig's cabinet name (Bank Preview number 10..14), once seen.
    cabinet_name: str | None = None


@dataclass(slots=True)
class Bank:
    """The loaded bank's five-slot name preview (page ``0x96``).

    The device pushes the whole block on a bank change, and it is readable on
    demand via :meth:`libkp.model.DeviceModel.refresh_bank`. ``slots[0]`` is rig
    slot 1.
    """

    slots: list[BankSlot] = field(
        default_factory=lambda: [BankSlot() for _ in range(gen.BANK_SLOTS)]
    )


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeviceEvent:
    """Base class for a typed change emitted by :meth:`DeviceState.apply`."""


@dataclass(frozen=True, slots=True)
class RigChanged(DeviceEvent):
    """The loaded rig changed (its Rig Name was (re)applied)."""


@dataclass(frozen=True, slots=True)
class StringTag(DeviceEvent):
    """A page-0 string tag was applied (``number`` 1 = Rig Name, 10 = Amp Name…)."""

    number: int


@dataclass(frozen=True, slots=True)
class BankPreview(DeviceEvent):
    """A bank-preview name was applied (page ``0x96``): one of the current bank's
    rig / amp / cabinet names. Read :attr:`DeviceState.bank` for the new value."""

    #: The Bank Preview number (0..14): 0–4 rig, 5–9 amp, 10–14 cabinet.
    number: int


@dataclass(frozen=True, slots=True)
class EffectChanged(DeviceEvent):
    """An effect slot's Type, On/Off state or Mix changed."""

    #: Index into :data:`libkp.params.EFFECT_SLOTS` (0 = A … 7 = REV).
    slot: int


@dataclass(frozen=True, slots=True)
class ParamChanged(DeviceEvent):
    """A numeric parameter changed that the model does not model specially."""

    page: int
    number: int
    value: int


@dataclass(frozen=True, slots=True)
class Status(DeviceEvent):
    """A new realtime status / meter frame arrived."""

    status: RealtimeStatus


@dataclass(frozen=True, slots=True)
class BeatPulse(DeviceEvent):
    """The tempo/beat pulse toggled (``on`` = value != 0)."""

    on: bool


@dataclass(frozen=True, slots=True)
class TempoBpm(DeviceEvent):
    """The tempo changed, in whole beats per minute."""

    bpm: int


@dataclass(frozen=True, slots=True)
class MorphChanged(DeviceEvent):
    """The morph position changed (``$01`` page 0 / number 0x77): 0 = base,
    16383 = fully morphed.

    Only a CBOR session ever sees this: the position is never sent on the MIDI3
    stream, even while a morph is ramping. See :class:`MorphButton`.
    """

    value: int


@dataclass(frozen=True, slots=True)
class MorphButton(DeviceEvent):
    """The morph button was pressed or released (``$01`` page 0 / number 0x50) --
    momentary, so nothing about it is stored in the snapshot.

    This is what a MIDI3 client sees of a morph: *that* one happened, and what it
    did to the audio parameters, but never where the fader sits.
    """

    #: True on press, False on release.
    on: bool


@dataclass(frozen=True, slots=True)
class TunerDeviance(DeviceEvent):
    """The tuner pitch deviance changed (``$01`` page 0x7C / number 0x0F)."""

    value: int


@dataclass(frozen=True, slots=True)
class TunerNote(DeviceEvent):
    """The tuner's detected note changed (``$01`` page 0x7D / number 0x54)."""

    note: int


@dataclass(frozen=True, slots=True)
class RenderedString(DeviceEvent):
    """A rendered-string reply arrived (``$3C``).

    Carries a value's exact display text (e.g. ``5.2``, ``120 BPM``, ``<0.0>``)
    for the requested address. Transient: it is not stored in the snapshot tree.
    """

    page: int
    number: int
    value: int
    text: str


@dataclass(frozen=True, slots=True)
class CurrentPosition(DeviceEvent):
    """The device's current position changed, or was learned for the first time.

    Read :attr:`DeviceState.current_bank` and :attr:`DeviceState.current_rig_slot`
    for the new values (both 0-based); a ``None`` here is a half not yet known,
    not a cleared one.
    """

    #: Current bank, 0-based, if known.
    bank: int | None
    #: Current rig slot within the bank, 0-based, if known.
    slot: int | None


@dataclass(frozen=True, slots=True)
class Connected(DeviceEvent):
    """The model connected to a device."""


@dataclass(frozen=True, slots=True)
class Disconnected(DeviceEvent):
    """The device closed the connection."""


@dataclass(slots=True)
class ApplyOutcome:
    """The result of applying one message: the granular events it produced, plus
    whether any **slow** (snapshot) field changed.

    The store uses :attr:`slow_changed` to decide whether to emit a fresh
    snapshot. It is ``False`` for FAST-only traffic (the meter block, the beat
    pulse, tuner deviance) and for untracked generic params that leave the
    snapshot unchanged; the granular events are emitted regardless.
    """

    events: list[DeviceEvent] = field(default_factory=list)
    slow_changed: bool = False

    @classmethod
    def empty(cls) -> ApplyOutcome:
        """An empty outcome: nothing happened, no slow change."""
        return cls()

    @classmethod
    def fast(cls, event: DeviceEvent) -> ApplyOutcome:
        """One event that changed no slow field (FAST lane or untracked generic)."""
        return cls(events=[event], slow_changed=False)

    @classmethod
    def slow(cls, events: list[DeviceEvent]) -> ApplyOutcome:
        """Events that changed a slow (snapshot-visible) field."""
        return cls(events=events, slow_changed=True)


# ---------------------------------------------------------------------------
# The root, with the decode routing
# ---------------------------------------------------------------------------

#: String tags this tree stores, keyed by tag number → (block attr, field attr).
_STRING_TAG_FIELDS: dict[int, tuple[str, str]] = {
    1: ("rig", "name"),
    2: ("rig", "author"),
    3: ("rig", "date"),
    4: ("rig", "comment"),
    10: ("amp", "name"),
    32: ("cabinet", "name"),
}


def _new_effects() -> list[Effect]:
    return [Effect(slot=name, page=page) for name, page in params.EFFECT_SLOTS]


@dataclass(slots=True)
class DeviceState:
    """The device-state snapshot — the store's value type.

    A plain-data bag the UI reads directly (``state.rig.name``,
    ``state.effects[0].on``, …). :class:`libkp.model.DeviceModel` hands out
    copies of this through its snapshot stream.
    """

    #: Whether a live session is open.
    connection: Connection = Connection.DISCONNECTED
    #: The loaded rig's metadata and settings.
    rig: Rig = field(default_factory=Rig)
    #: The amplifier block.
    amp: Amp = field(default_factory=Amp)
    #: The cabinet block.
    cabinet: Cabinet = field(default_factory=Cabinet)
    #: The eight effect slots in signal-chain order (A..REV).
    effects: list[Effect] = field(default_factory=_new_effects)
    #: The tuner readout.
    tuner: Tuner = field(default_factory=Tuner)
    #: The global output volumes.
    output: Output = field(default_factory=Output)
    #: The loaded bank's five-slot name preview (page ``0x96``).
    bank: Bank = field(default_factory=Bank)
    #: Current bank, 0-based, once known. Kept live by the ``$06`` Extended
    #: Parameter the device sends at :data:`libkp._generated.CURRENT_BANK_ADDRESS`
    #: whenever the bank changes -- from the front panel as readily as from a
    #: controller -- and by the reply to
    #: :meth:`libkp.model.DeviceModel.refresh_position`. Can also be seeded
    #: before a session opens, from the CBOR state-dump snapshot
    #: (:func:`libkp.cbor.fetch_state_snapshot`) via
    #: :meth:`libkp.model.DeviceModel.set_current_position`.
    current_bank: int | None = None
    #: Current rig slot within the bank, 0-based, once known. Same source as
    #: :attr:`current_bank`, at
    #: :data:`libkp._generated.CURRENT_RIG_SLOT_ADDRESS`; slot 0 is rig slot 1.
    current_rig_slot: int | None = None

    @property
    def current_rig_index(self) -> int | None:
        """Flat, 0-based rig index, once both halves are known.

        ``current_bank * BANK_SLOTS + current_rig_slot`` -- the device's own
        numbering, and the only address that can name a rig outside the current
        bank.
        """
        if self.current_bank is None or self.current_rig_slot is None:
            return None
        return self.current_bank * gen.BANK_SLOTS + self.current_rig_slot

    #: Latest morph position (0 = base, 16383 = fully morphed), once seen (NRPN
    #: ``0x00/0x77``). Filled only from a CBOR source -- the state dump
    #: (:attr:`libkp.cbor.StateSnapshot.morph`) or a CBOR session's live pushes.
    #: A MIDI3-only client never learns it, so this stays ``None`` there.
    morph: int | None = None
    #: The most recent realtime status / meter frame (the FAST lane).
    status: RealtimeStatus = field(default_factory=RealtimeStatus)

    # -- accessors ---------------------------------------------------------

    def effect(self, slot: str) -> Effect | None:
        """The effect slot named ``slot`` (case-insensitive), or ``None``."""
        wanted = slot.upper()
        for e in self.effects:
            if e.slot.upper() == wanted:
                return e
        return None

    def snapshot(self) -> DeviceState:
        """An independent copy of this state, safe to hand to another consumer."""
        return copy.deepcopy(self)

    # -- decode ------------------------------------------------------------

    def apply(self, msg: bytes) -> ApplyOutcome:
        """Parse and apply ONE decoded MIDI message.

        Pure: no I/O and no clock. Non-Kemper or ignored messages return an empty
        outcome. Routing:

        - ``$03`` string on page 0 → rig/amp/cab name tags + ``StringTag`` (plus
          ``RigChanged`` for tag 1, the Rig Name). SLOW.
        - ``$01`` single-param → beat pulse / effect / amp / rig / output / morph
          / tuner / generic ``ParamChanged``; FAST for the beat pulse and tuner
          deviance, SLOW for the tracked fields.
        - ``$02`` multi-param → the meter block (→ ``Status``, FAST) or a
          rig-load dump (each consecutive address routed like a single-param).
        - ``$07`` ext-string on the string page → the amp/cab/author tags the
          rig-load dump sends as extended strings.
        - ``$06`` extended param → the device's current bank / rig slot.
        - ``$3C`` rendered-string reply → a transient ``RenderedString``. FAST.
        - Anything else → ignored.
        """
        parsed = NrpnHeader.parse(msg)
        if parsed is None:
            return ApplyOutcome.empty()
        header, vals = parsed

        # The extended ($06) and extended-string ($07) addresses are 5x7-bit
        # encoded and do not fit the fixed header layout, so route both off the
        # raw message.
        if header.function == nrpn.FUNCTION_EXT_PARAM:
            return self._apply_ext_param(msg)
        if header.function == nrpn.FUNCTION_EXT_STRING_PARAM:
            return self._apply_ext_string(msg)

        if header.function == nrpn.FUNCTION_STRING_PARAM and header.page == nrpn.PAGE_STRINGS:
            return self._apply_string(header.number, vals)
        if header.function == nrpn.FUNCTION_SINGLE_PARAM:
            if len(vals) < 2:
                return ApplyOutcome.empty()
            return self._route_param(header.page, header.number, u14(vals[0], vals[1]))
        if header.function == nrpn.FUNCTION_MULTI_PARAM:
            return self._apply_multi(header.page, header.number, vals)
        if header.function == nrpn.FUNCTION_RENDERED_STRING_REPLY:
            return self._apply_rendered_string(msg)
        return ApplyOutcome.empty()

    # -- decode helpers ----------------------------------------------------

    def _apply_ext_param(self, msg: bytes) -> ApplyOutcome:
        """Fold a ``$06`` Extended Parameter into the tree.

        This is the device's position report. It carries a 5x7-bit address and a
        5x7-bit value; the two addresses that matter here are the current bank
        and the current rig slot, both 0-based. The device pushes whichever of
        the two changed on every rig change -- including changes made at the
        front panel -- and answers
        :meth:`libkp.model.DeviceModel.refresh_position` with both.

        Any other extended address is ignored, and a value too large for the
        16-bit field is dropped rather than truncated into a bogus position.
        """
        parsed = nrpn.parse_extended_param(msg)
        if parsed is None:
            return ApplyOutcome.empty()
        address, value = parsed
        if value > 0xFFFF:
            return ApplyOutcome.empty()
        return self._apply_address(address, value)

    def apply_cbor(self, address: int, value: int) -> ApplyOutcome:
        """Fold one **CBOR** address/value into the tree.

        The CBOR channel and the MIDI3 stream are two wire formats over one event
        universe, so a value arriving either way lands in the same field and
        raises the same event. This is the entry point for a
        :class:`libkp.cbor.CborSession`'s live pushes and for a state dump's items.

        A value outside the 16-bit range is dropped rather than truncated into a
        bogus reading.
        """
        if not 0 <= value <= 0xFFFF:
            return ApplyOutcome.empty()
        return self._apply_address(address, value)

    def _apply_address(self, address: int, value: int) -> ApplyOutcome:
        """Route one flat address to the field that tracks it, whichever
        transport carried it. Unknown addresses change nothing."""
        if address == gen.CURRENT_BANK_ADDRESS:
            if self.current_bank == value:
                return ApplyOutcome.empty()
            self.current_bank = value
            return ApplyOutcome.slow([CurrentPosition(bank=value, slot=self.current_rig_slot)])
        if address == gen.CURRENT_RIG_SLOT_ADDRESS:
            if self.current_rig_slot == value:
                return ApplyOutcome.empty()
            self.current_rig_slot = value
            return ApplyOutcome.slow([CurrentPosition(bank=self.current_bank, slot=value)])
        if address == gen.MORPH_ADDRESS:
            if self.morph == value:
                return ApplyOutcome.empty()
            self.morph = value
            return ApplyOutcome.slow([MorphChanged(value=value)])
        return ApplyOutcome.empty()

    def _apply_string(self, number: int, vals: bytes) -> ApplyOutcome:
        """Route a ``$03`` page-0 string tag into the tree."""
        return self._store_string_tag(number, _ascii_until_nul(vals))

    def _apply_ext_string(self, msg: bytes) -> ApplyOutcome:
        """Route a ``$07`` Extended String Parameter message.

        This is how the rig-load dump delivers the amp/cab/author metadata that
        plain ``$03`` strings omit. The extended address decodes as
        ``page * 128 + number``; a string on the string page carries the same tag
        numbers as ``$03``. Other pages are ignored.
        """
        parsed = nrpn.parse_extended_string(msg)
        if parsed is None:
            return ApplyOutcome.empty()
        address, text = parsed
        page, number = divmod(address, 128)
        if page == gen.PAGE_BANK_PREVIEW:
            return self._store_bank_preview(number, text)
        if page != nrpn.PAGE_STRINGS:
            return ApplyOutcome.empty()
        return self._store_string_tag(number, text)

    def _store_bank_preview(self, number: int, text: str) -> ApplyOutcome:
        """Store one bank-preview name (page ``0x96``) into :attr:`bank`.

        The number selects the field (rig / amp / cabinet) and slot; an
        out-of-range number is ignored. A stored value is a SLOW change.
        """
        resolved = params.bank_preview_slot_field(number)
        if resolved is None:
            return ApplyOutcome.empty()
        field_, slot = resolved
        target = self.bank.slots[slot]
        if field_ is params.BankPreviewField.RIG_NAME:
            target.rig_name = text
        elif field_ is params.BankPreviewField.AMP_NAME:
            target.amp_name = text
        else:
            target.cabinet_name = text
        return ApplyOutcome.slow([BankPreview(number=number)])

    def _store_string_tag(self, number: int, text: str) -> ApplyOutcome:
        """Store a decoded page-0 string tag into the matching field."""
        target = _STRING_TAG_FIELDS.get(number)
        if target is not None:
            block, attr = target
            setattr(getattr(self, block), attr, text)
        events: list[DeviceEvent] = [StringTag(number=number)]
        if number == nrpn.STRING_RIG_NAME:
            events.append(RigChanged())
        return ApplyOutcome(events=events, slow_changed=target is not None)

    def _route_param(self, page: int, number: int, value: int) -> ApplyOutcome:
        """Route one decoded numeric address/value pair into the tree.

        Shared by ``$01`` single-param and each address of a ``$02`` dump.
        """
        # Beat pulse: volatile, not stored (FAST lane).
        if page == gen.PAGE_REALTIME and number == gen.BEAT_PULSE_NUMBER:
            return ApplyOutcome.fast(BeatPulse(on=value != 0))
        # Tuner pitch deviance: volatile (FAST lane).
        if page == gen.PAGE_REALTIME and number == gen.TUNER_DEVIANCE_NUMBER:
            self.tuner.deviance = value
            return ApplyOutcome.fast(TunerDeviance(value=value))
        # Effect Type / On-Off / Mix: fold into the slot (SLOW).
        if params.is_effect_page(page) and number in (
            params.EFFECT_PARAM_TYPE,
            params.EFFECT_PARAM_STATE,
            params.EFFECT_PARAM_MIX,
        ):
            idx = self._apply_effect(page, number, value)
            if idx is None:
                return ApplyOutcome.empty()
            return ApplyOutcome.slow([EffectChanged(slot=idx)])
        # Amplifier On/Off and Gain (SLOW).
        if page == gen.AMP_PAGE and number == gen.AMP_ON_NUMBER:
            self.amp.on = value != 0
            return ApplyOutcome.slow([ParamChanged(page, number, value)])
        if page == gen.AMP_PAGE and number == gen.GAIN_NUMBER:
            self.amp.gain = value
            return ApplyOutcome.slow([ParamChanged(page, number, value)])
        # Tempo bpm (the wire value is bpm * 64) and Rig Volume (SLOW).
        if page == gen.PAGE_RIG_SETTINGS and number == gen.TEMPO_NUMBER:
            bpm = value // gen.TEMPO_BPM_SCALE
            self.rig.tempo_bpm = bpm
            return ApplyOutcome.slow([TempoBpm(bpm=bpm)])
        if page == gen.PAGE_RIG_SETTINGS and number == gen.RIG_VOLUME_NUMBER:
            self.rig.volume = value
            return ApplyOutcome.slow([ParamChanged(page, number, value)])
        # System output volumes (SLOW).
        if page == gen.SYSTEM_PAGE and number == gen.MAIN_VOLUME_NUMBER:
            self.output.main_volume = value
            return ApplyOutcome.slow([ParamChanged(page, number, value)])
        if page == gen.SYSTEM_PAGE and number == gen.HEADPHONE_VOLUME_NUMBER:
            self.output.headphone_volume = value
            return ApplyOutcome.slow([ParamChanged(page, number, value)])
        if page == gen.SYSTEM_PAGE and number == gen.MONITOR_VOLUME_NUMBER:
            self.output.monitor_volume = value
            return ApplyOutcome.slow([ParamChanged(page, number, value)])
        # Morph (page 0 is dual-use with the string page). The position is SLOW;
        # the button is momentary, so it is an event and nothing more.
        if page == gen.PAGE_MORPH and number == gen.MORPH_NUMBER:
            self.morph = value
            return ApplyOutcome.slow([MorphChanged(value=value)])
        if page == gen.PAGE_MORPH and number == gen.MORPH_BUTTON_NUMBER:
            return ApplyOutcome.fast(MorphButton(on=value != 0))
        # Tuner detected note (SLOW).
        if page == gen.PAGE_TUNER_NOTE and number == gen.TUNER_NOTE_NUMBER:
            note = value & 0x7F
            self.tuner.note = note
            return ApplyOutcome.slow([TunerNote(note=note)])
        # Untracked generic parameter: leaves the snapshot unchanged, so it is
        # not a slow change — only the granular event stream sees it.
        return ApplyOutcome.fast(ParamChanged(page, number, value))

    def _apply_effect(self, page: int, number: int, value: int) -> int | None:
        """Store a decoded effect-slot parameter into the matching slot."""
        idx = params.effect_slot_index(page)
        if idx is None:
            return None
        slot = self.effects[idx]
        if number == params.EFFECT_PARAM_TYPE:
            slot.kind = value
        elif number == params.EFFECT_PARAM_STATE:
            slot.on = value != 0
        elif number == params.EFFECT_PARAM_MIX:
            slot.mix = value
        return idx

    def _apply_rendered_string(self, msg: bytes) -> ApplyOutcome:
        """Route a ``$3C`` Rendered String reply — a transient, unstored event."""
        parsed = nrpn.parse_rendered_string(msg)
        if parsed is None:
            return ApplyOutcome.empty()
        page, number, value, text = parsed
        return ApplyOutcome.fast(RenderedString(page=page, number=number, value=value, text=text))

    def _apply_multi(self, page: int, number: int, vals: bytes) -> ApplyOutcome:
        """Route a ``$02`` multi-param message: the meter block, or a rig-load dump."""
        if page == gen.PAGE_REALTIME and number == gen.METER_BLOCK_NUMBER:
            self.status = RealtimeStatus.from_values(vals)
            return ApplyOutcome.fast(Status(status=self.status))
        out = ApplyOutcome.empty()
        for num, value in nrpn.multi_values(number, vals):
            step = self._route_param(page, num, value)
            out.events.extend(step.events)
            out.slow_changed = out.slow_changed or step.slow_changed
        return out


def _ascii_until_nul(data: bytes) -> str:
    end = data.find(0)
    if end >= 0:
        data = data[:end]
    return "".join(chr(b) for b in data)
