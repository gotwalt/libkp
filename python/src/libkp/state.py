"""The typed device-state tree — the **store snapshot** a UI binds to — and the
pure, network-free decode logic that fills it in.

Everything in the tree is plain data (dataclasses of scalars, strings and
``None``), cheap to copy for each snapshot the store emits and easy to mirror
across an FFI boundary as value types.

:class:`DeviceState` is the root. A value reaches it on one of two wires -- a
MIDI3 SysEx message through :meth:`DeviceState.apply`, or a CBOR item through
:meth:`DeviceState.apply_cbor` / :meth:`DeviceState.apply_cbor_text` -- and both
are thin decoders that build an :class:`Update` and hand it to the one fold,
:meth:`DeviceState.apply_update`. The fold consults the routing table generated
from ``spec/state.toml`` (:data:`libkp._generated.STATE_ROUTES`) and returns an
:class:`ApplyOutcome`: the granular :class:`DeviceEvent` list plus whether any
*slow* field changed. It does no I/O, so tests drive it with synthesized
messages. The async handle that owns a socket and feeds this is
:class:`libkp.model.DeviceModel`.

FAST vs SLOW
------------

**FAST** = the meter :class:`RealtimeStatus` block, the beat pulse, tuner
deviance and the morph button — high-rate values a UI polls per animation
frame. **SLOW** = everything else (rig / amp / cab / effect / output / tempo /
morph / tuner-note / position / connection), which drives the coalesced snapshot
stream. Which is which is the ``lane`` column of the table.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from enum import Enum

from . import _generated as gen
from . import _routes, nrpn, params
from .nrpn import NrpnHeader, u14

__all__ = [
    "Channel",
    "Phase",
    "Decoded",
    "Num",
    "Text",
    "Block",
    "Update",
    "Connection",
    "ChannelState",
    "Channels",
    "Navigation",
    "NavDrop",
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
    "NavigationSettled",
    "NavigationDropped",
    "Connected",
    "Disconnected",
    "ConnectionChanged",
    "ChannelChanged",
    "SyncCompleted",
    "RequestTimedOut",
    "ApplyOutcome",
]


class Connection(Enum):
    """The model's overall link to the Profiler, summarising its two channels.

    The MIDI3 stream is the link that matters: without it there is no
    connection at all. The CBOR control channel is the optional second socket
    that carries what the stream omits (the morph position), and losing it only
    *degrades* the connection -- everything the stream carries keeps flowing.
    """

    #: No session: the initial state, the device closed the connection and the
    #: model was not asked to reconnect, or :meth:`~libkp.model.DeviceModel.close`
    #: was called.
    DISCONNECTED = "disconnected"
    #: The stream was lost and the model is waiting out a backoff before dialing
    #: again (:attr:`DeviceState.reconnect_attempt` counts the tries). Only seen
    #: when a :class:`~libkp.model.ReconnectPolicy` asked for it.
    RECONNECTING = "reconnecting"
    #: The stream is open and being ingested, and the control channel is either
    #: open, still on its way, or was never asked for.
    CONNECTED = "connected"
    #: The stream is open but the control channel was asked for and is not
    #: there: it could not be opened, or it was open and the device ended it.
    #: The morph position is stale or unknown; nothing else is affected.
    DEGRADED = "degraded"


class ChannelState(Enum):
    """Where one of the model's two sockets is in its life."""

    #: Not asked for: the policy is off, or no attempt has been made yet.
    CLOSED = "closed"
    #: Dialing, or in the protocol handshake.
    CONNECTING = "connecting"
    #: Handshaken and streaming.
    OPEN = "open"
    #: The open failed -- the dial, the handshake, or (for the control channel)
    #: the write that asks for the state dump.
    UNAVAILABLE = "unavailable"
    #: Was open, then the socket ended.
    LOST = "lost"


@dataclass(slots=True)
class Channels:
    """The state of the model's two sockets, side by side."""

    #: The MIDI3 stream: the meter lane, the parameter pushes, every request.
    stream: ChannelState = ChannelState.CLOSED
    #: The CBOR control channel: the state dump and the morph position.
    control: ChannelState = ChannelState.CLOSED


@dataclass(slots=True)
class Navigation:
    """Where the model's Navigator is between the client's aim and the device.

    The Navigator (:meth:`libkp.model.DeviceModel.navigate_to` and its
    siblings) is the only way libkp loads a rig: it serialises loads so two
    can never overlap on the wire, which is what wedges the device. This is
    its public face -- enough for a UI to highlight the slot a client tapped
    before the device confirms it.
    """

    #: The flat, 0-based rig index the client is aiming at, until the device
    #: reports it as the current position (then ``None``) or never does and
    #: the aim is dropped (:class:`NavigationDropped`).
    aim: int | None = None
    #: Whether a load is on the wire and inside its settle
    #: (:data:`libkp._generated.RIG_LOAD_SETTLE_MS`), during which a new aim
    #: waits.
    in_flight: bool = False


class NavDrop(Enum):
    """Why the Navigator gave up on an aim."""

    #: The device never reported the aim as its position inside
    #: :data:`libkp._generated.PENDING_WINDOW_MS` of the move settling -- an
    #: index past the last rig, typically, where the device stays put and says
    #: so. Also the reason when the stream was down at the moment of the send.
    UNCONFIRMED = "unconfirmed"


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

    Only the control link ever carries this: the position is never sent on the
    MIDI3 stream, even while a morph is ramping, so a model whose control link
    is off or lost never raises it. See :class:`MorphButton`.
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
class NavigationSettled(DeviceEvent):
    """The device reported the Navigator's aim as its current position: the
    load landed, and :attr:`DeviceState.navigation` has no aim."""

    #: The flat, 0-based rig index that was aimed at and confirmed.
    index: int


@dataclass(frozen=True, slots=True)
class NavigationDropped(DeviceEvent):
    """The Navigator gave up on an aim the device never confirmed.

    :attr:`DeviceState.navigation` has no aim, and
    :attr:`DeviceState.current_rig_index` is where the device actually is.
    """

    #: The flat, 0-based rig index that was aimed at.
    index: int
    reason: NavDrop


@dataclass(frozen=True, slots=True)
class Connected(DeviceEvent):
    """The model connected to a device."""


@dataclass(frozen=True, slots=True)
class Disconnected(DeviceEvent):
    """The device closed the connection, or the model was closed."""


@dataclass(frozen=True, slots=True)
class ConnectionChanged(DeviceEvent):
    """:attr:`DeviceState.connection` moved to a new value.

    Raised on *every* transition, so a client watching this one event follows
    the whole life of the link -- reconnect attempts and degradation included.
    :class:`Connected` and :class:`Disconnected` are still raised alongside it
    for the two transitions they name.
    """

    connection: Connection


@dataclass(frozen=True, slots=True)
class ChannelChanged(DeviceEvent):
    """One of the model's two sockets moved to a new :class:`ChannelState`.

    The only event that names a channel: everything else about the two wires
    is folded into one tree and one event stream.
    """

    channel: Channel
    state: ChannelState


@dataclass(frozen=True, slots=True)
class SyncCompleted(DeviceEvent):
    """A channel finished filling the tree in.

    For :attr:`Channel.STREAM` the connect-time request burst has had its last
    reply (or its last timeout); for :attr:`Channel.CONTROL` the state dump
    has ended. After either, what that channel can tell is in the snapshot.
    """

    source: Channel


@dataclass(frozen=True, slots=True)
class RequestTimedOut(DeviceEvent):
    """A read request drew no reply inside its deadline and was abandoned.

    Never retried: the device ignores an address it cannot answer, and asking
    again would only cost it more. ``address`` is the flat address asked for.
    """

    address: int


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
    #: Every flat rig index this update reported for the Navigator: a position
    #: row that stored -- or deduped an unchanged report -- with both halves
    #: known and the index inside sixteen bits. A deduped report raises no
    #: event, but the Navigator still needs it: re-loading the rig already
    #: loaded is confirmed by a push that changes nothing.
    positions: list[int] = field(default_factory=list)

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
# Updates: what the two wires decode to before the fold
# ---------------------------------------------------------------------------


class Channel(Enum):
    """Which wire carried a value: the MIDI3 stream or the CBOR control channel."""

    STREAM = "stream"
    CONTROL = "control"


class Phase(Enum):
    """Whether a value was pushed live or is an item of the CBOR state dump.

    The dump is a copy of the device's state taken when it was asked for, so a
    live push that lands while it is still streaming is newer than the dump's
    item for the same address. :meth:`DeviceState.begin_dump` and
    :meth:`DeviceState.end_dump` bracket the window in which that matters.
    """

    LIVE = "live"
    DUMP = "dump"


@dataclass(frozen=True, slots=True)
class Decoded:
    """Base class for a wire value once its transport encoding is stripped."""


@dataclass(frozen=True, slots=True)
class Num(Decoded):
    """A numeric value: a 14-bit ``$01`` pair, a 35-bit ``$06`` extended value,
    or a CBOR integer."""

    value: int


@dataclass(frozen=True, slots=True)
class Text(Decoded):
    """A string: a ``$03`` / ``$07`` tag, or a CBOR ``[4, addr, text]`` item."""

    text: str


@dataclass(frozen=True, slots=True)
class Block(Decoded):
    """The values of one ``$02`` multi-value message, starting at the update's
    address. Stream only: the control channel has no equivalent shape."""

    values: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class Update:
    """One value on its way into the tree, tagged with where it came from.

    :meth:`DeviceState.apply` and :meth:`DeviceState.apply_cbor` are thin
    decoders that build these; :meth:`DeviceState.apply_update` is the one fold
    every value goes through, whichever wire carried it.
    """

    source: Channel
    phase: Phase
    #: The flat address: ``page * 128 + number``, or a bare extended address.
    address: int
    decoded: Decoded


#: One past the last flat address the page/number space can name. Below it an
#: address is a ``(page, number)`` pair the stream reports generically; at or
#: above it an address is an extended one and has no generic report.
_FLAT_ADDRESS_LIMIT = 128 * 128


# ---------------------------------------------------------------------------
# The root, with the fold
# ---------------------------------------------------------------------------


def _new_effects() -> list[Effect]:
    return [Effect(slot=name, page=page) for name, page in params.EFFECT_SLOTS]


@dataclass(slots=True)
class DeviceState:
    """The device-state snapshot — the store's value type.

    A plain-data bag the UI reads directly (``state.rig.name``,
    ``state.effects[0].on``, …). :class:`libkp.model.DeviceModel` hands out
    copies of this through its snapshot stream.
    """

    #: The model's overall link to the device, summarising :attr:`channels`.
    connection: Connection = Connection.DISCONNECTED
    #: Which reconnect attempt is pending while :attr:`connection` is
    #: :attr:`Connection.RECONNECTING`; ``0`` at every other time.
    reconnect_attempt: int = 0
    #: The state of each of the model's two sockets.
    channels: Channels = field(default_factory=Channels)
    #: The Navigator's aim and whether a rig load is in flight.
    navigation: Navigation = field(default_factory=Navigation)
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
    #: controller -- by the reply to
    #: :meth:`libkp.model.DeviceModel.refresh_position`, and by the control
    #: channel's state dump and pushes, all through the one fold.
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
        bank. ``None`` too for halves whose product leaves sixteen bits: a
        garbled wire value must yield no index, not a plausible-looking one.
        """
        if self.current_bank is None or self.current_rig_slot is None:
            return None
        flat = self.current_bank * gen.BANK_SLOTS + self.current_rig_slot
        return flat if flat <= 0xFFFF else None

    @property
    def aimed_rig_index(self) -> int | None:
        """Where the Navigator is headed: its aim while it has one, else
        :attr:`current_rig_index`.

        The index a UI's rig browser should highlight, and what
        :meth:`libkp.model.DeviceModel.step_rig` steps from -- so a burst of
        taps counts from the last tap, not from wherever the device has got to.
        """
        aim = self.navigation.aim
        return aim if aim is not None else self.current_rig_index

    #: Latest morph position (0 = base, 16383 = fully morphed), once seen (NRPN
    #: ``0x00/0x77``). Filled only from the CBOR control channel -- its state
    #: dump when the model's control link opens, then its live pushes -- so it
    #: stays ``None`` while :attr:`channels` says the control link never opened,
    #: and goes stale once it is lost (:attr:`Connection.DEGRADED`).
    morph: int | None = None
    #: The most recent realtime status / meter frame (the FAST lane).
    status: RealtimeStatus = field(default_factory=RealtimeStatus)

    # Dump bookkeeping (see :class:`Phase`). Internal: it is not part of the
    # snapshot a UI reads, so it takes no part in equality, and it is what
    # :meth:`begin_dump` / :meth:`end_dump` bracket.
    _dump_active: bool = field(default=False, compare=False, repr=False)
    _dump_touched: set[int] = field(default_factory=set, compare=False, repr=False)

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

    # -- decoders ----------------------------------------------------------

    def apply(self, msg: bytes) -> ApplyOutcome:
        """Decode and apply ONE already-unframed MIDI3 message.

        Pure: no I/O and no clock. A non-Kemper or unparseable message returns an
        empty outcome. Each message becomes one :class:`Update` (a ``$02`` block
        is one update carrying a :class:`Block`) on the :attr:`Channel.STREAM`
        wire and goes through :meth:`apply_update`:

        - ``$03`` string and ``$07`` extended string → :class:`Text` at
          ``page * 128 + number`` (the page-0 tags, the bank preview).
        - ``$01`` single-param → :class:`Num` at ``page * 128 + number``.
        - ``$02`` multi-param → :class:`Block` at ``page * 128 + number``: the
          meter frame when it sits at the meter base, otherwise one value per
          consecutive address (a rig-load dump).
        - ``$06`` extended param → :class:`Num` at the bare extended address
          (the device's current bank / rig slot).

        One reply stays outside the table: a ``$3C`` rendered-string reply is a
        transient :class:`RenderedString` event and stores nothing.
        """
        decoded = _decode_stream(msg)
        if decoded is None:
            return ApplyOutcome.empty()
        if isinstance(decoded, RenderedString):
            return ApplyOutcome.fast(decoded)
        return self.apply_update(decoded)

    def apply_cbor(self, address: int, value: int) -> ApplyOutcome:
        """Fold one live **CBOR** numeric into the tree.

        The CBOR channel and the MIDI3 stream are two wire formats over one event
        universe, so a value arriving either way lands in the same field and
        raises the same event. The model's control link folds its items through
        the same rules (with the dump phase set where it applies); this is the
        one-value entry point for tooling and vectors. A negative value is not a
        parameter value on this channel and is dropped before routing; anything
        else is range-checked by the row it lands on.
        """
        if value < 0:
            return ApplyOutcome.empty()
        return self.apply_update(Update(Channel.CONTROL, Phase.LIVE, address, Num(value)))

    def apply_cbor_text(self, address: int, text: str) -> ApplyOutcome:
        """Fold one live **CBOR** string (a ``[4, addr, text]`` item) into the tree,
        the control channel's counterpart of a ``$03`` / ``$07`` tag."""
        return self.apply_update(Update(Channel.CONTROL, Phase.LIVE, address, Text(text)))

    def begin_dump(self) -> None:
        """Open the window in which live pushes outrank the CBOR state dump.

        Until :meth:`end_dump`, a :attr:`Phase.LIVE` update marks its address, and
        a :attr:`Phase.DUMP` item for a marked address is dropped: the dump is a
        copy taken when it was requested, so a value pushed while it streams is
        the newer one. Outside the window a dump item folds like a live value.
        """
        self._dump_active = True
        self._dump_touched.clear()

    def end_dump(self) -> None:
        """Close the window :meth:`begin_dump` opened and forget the addresses it
        marked."""
        self._dump_active = False
        self._dump_touched.clear()

    # -- the fold ----------------------------------------------------------

    def apply_update(self, u: Update) -> ApplyOutcome:
        """THE funnel: fold one decoded value into the tree, whichever wire it
        came from, by the routing table in ``spec/state.toml``.

        In order:

        1. Look the address up in :data:`libkp._generated.STATE_ROUTES`. A
           :class:`Block` at a ``multi`` row's base decodes as one unit (the
           meter frame) whatever its length -- a short block zero-fills the
           tail, a long one is cut at the span; any other block folds element
           by element as :class:`Num` updates at ``address + i``, merging the
           outcomes.
        2. No row, or a row whose ``kind`` does not take this shape (a string at a
           numeric row, a numeric at a text row): nothing is stored. The stream
           still reports a numeric in the page/number space as a generic
           :class:`ParamChanged`; a control-channel value, a string, or an
           extended address is silent.
        3. The row's ``wire``: a ``stream`` row drops the control channel's copy
           (its meter, beat, tuner and momentary feeds are a different, unwanted
           stream). A ``control`` row accepts the stream anyway -- the morph
           position never appears there, but if it did it would be real.
        4. Decode and range-check by ``kind``: ``u14`` and ``bpm`` drop anything
           past 16383, ``u16`` past 65535 (dropped, never truncated); ``u7``
           keeps the low seven bits; ``bool`` is nonzero.
        5. Dump authority (see :meth:`begin_dump`).
        6. Dedupe: a row with ``dedupe`` set and the decoded value already stored
           is a no-op -- no event, no snapshot. The momentaries and the meter
           frame never dedupe; every arrival is the information. A deduped
           position row still reports its (unchanged) flat index in
           :attr:`ApplyOutcome.positions`.
        7. Store, and raise the row's event: FAST rows raise the event only, SLOW
           rows also flag the snapshot.
        """
        route = _routes.lookup(u.address)
        if isinstance(u.decoded, Block):
            if route is None or route.kind is not gen.Kind.MULTI or route.slot != 0:
                return self._fold_elements(u, u.decoded.values)
        elif route is None or not _accepts(route.kind, u.decoded):
            return self._untracked(u)

        if route.wire is gen.Wire.STREAM and u.source is Channel.CONTROL:
            return ApplyOutcome.empty()

        value = _decode(route.kind, u.decoded, u.address)
        if value is None:
            return ApplyOutcome.empty()

        if self._dump_active:
            if u.phase is Phase.LIVE:
                self._dump_touched.add(u.address)
            elif u.address in self._dump_touched:
                return ApplyOutcome.empty()

        if route.dedupe and _routes.read(self, route) == value:
            # A silenced position update still reports the (unchanged) index:
            # the Navigator is confirmed by pushes, not by changes.
            return ApplyOutcome(positions=self._position_report(route.field))

        _routes.write(self, route, value)
        events = self._events_for(route, u, value)
        return ApplyOutcome(
            events=events,
            slow_changed=route.lane is gen.Lane.SLOW,
            positions=self._position_report(route.field),
        )

    # -- fold helpers ------------------------------------------------------

    def _fold_elements(self, u: Update, values: tuple[int, ...]) -> ApplyOutcome:
        """Fold a block that is not the meter frame one address at a time."""
        out = ApplyOutcome.empty()
        for i, value in enumerate(values):
            step = self.apply_update(Update(u.source, u.phase, u.address + i, Num(value)))
            out.events.extend(step.events)
            out.slow_changed = out.slow_changed or step.slow_changed
            out.positions.extend(step.positions)
        return out

    def _position_report(self, field: gen.Field) -> list[int]:
        """What a position row reports for the Navigator once it has folded
        (or deduped): the flat rig index, when both halves are known and it
        fits sixteen bits. Every other row reports nothing."""
        if field in (gen.Field.CURRENT_BANK, gen.Field.CURRENT_RIG_SLOT):
            index = self.current_rig_index
            if index is not None:
                return [index]
        return []

    def _untracked(self, u: Update) -> ApplyOutcome:
        """An address the tree does not store.

        The stream's generic numerics are still worth an event -- a UI may watch
        a parameter the tree has no field for -- but the snapshot is untouched,
        so it is never a slow change. Nothing else has a generic report.
        """
        if (
            u.source is Channel.STREAM
            and isinstance(u.decoded, Num)
            and u.address < _FLAT_ADDRESS_LIMIT
        ):
            page, number = divmod(u.address, 128)
            return ApplyOutcome.fast(ParamChanged(page, number, u.decoded.value))
        return ApplyOutcome.empty()

    def _events_for(self, route: gen.Route, u: Update, value: object) -> list[DeviceEvent]:
        """The event(s) a row raises once its value is stored -- the ``event``
        column of ``spec/state.toml``, hand-written here because the payloads
        read the tree (the position carries both halves) and the wire value (a
        generic ``ParamChanged`` reports what arrived, not the decoded bool)."""
        f = route.field
        page, number = divmod(u.address, 128)
        if f is gen.Field.RIG_NAME:
            return [StringTag(number=number), RigChanged()]
        if f in _STRING_TAG_FIELDS:
            return [StringTag(number=number)]
        if f in _PARAM_CHANGED_FIELDS:
            raw = u.decoded.value if isinstance(u.decoded, Num) else 0
            return [ParamChanged(page, number, raw)]
        if f in _EFFECT_FIELDS:
            return [EffectChanged(slot=route.slot)]
        if f in _BANK_PREVIEW_FIELDS:
            return [BankPreview(number=number)]
        if f is gen.Field.MORPH_BUTTON:
            return [MorphButton(on=value)]
        if f is gen.Field.MORPH_POSITION:
            return [MorphChanged(value=value)]
        if f is gen.Field.TEMPO_BPM:
            return [TempoBpm(bpm=value)]
        if f is gen.Field.BEAT_PULSE:
            return [BeatPulse(on=value)]
        if f is gen.Field.TUNER_DEVIANCE:
            return [TunerDeviance(value=value)]
        if f is gen.Field.STATUS:
            return [Status(status=value)]
        if f is gen.Field.TUNER_NOTE:
            return [TunerNote(note=value)]
        if f in (gen.Field.CURRENT_BANK, gen.Field.CURRENT_RIG_SLOT):
            return [CurrentPosition(bank=self.current_bank, slot=self.current_rig_slot)]
        raise ValueError(f"no event for {f}")


#: The page-0 tags that raise a bare ``StringTag`` (the rig name raises
#: ``RigChanged`` as well).
_STRING_TAG_FIELDS = frozenset(
    {
        gen.Field.RIG_AUTHOR,
        gen.Field.RIG_DATE,
        gen.Field.RIG_COMMENT,
        gen.Field.AMP_NAME,
        gen.Field.CABINET_NAME,
    }
)
#: The scalar numerics with no event of their own.
_PARAM_CHANGED_FIELDS = frozenset(
    {
        gen.Field.RIG_VOLUME,
        gen.Field.AMP_ON,
        gen.Field.AMP_GAIN,
        gen.Field.CABINET_ON,
        gen.Field.MAIN_VOLUME,
        gen.Field.HEADPHONE_VOLUME,
        gen.Field.MONITOR_VOLUME,
    }
)
_EFFECT_FIELDS = frozenset({gen.Field.EFFECT_TYPE, gen.Field.EFFECT_ON, gen.Field.EFFECT_MIX})
_BANK_PREVIEW_FIELDS = frozenset(
    {gen.Field.BANK_RIG_NAME, gen.Field.BANK_AMP_NAME, gen.Field.BANK_CABINET_NAME}
)


def _accepts(kind: gen.Kind, decoded: Decoded) -> bool:
    """Does a row of ``kind`` take this shape of value? Page 0 is dual-use --
    the same numbers are string tags via ``$03`` and numerics via ``$01`` -- so
    the row's kind says which face it stores; the other is untracked. A block
    is never accepted here: the meter frame is matched before this, and any
    other block has already been split into its elements."""
    if isinstance(decoded, Text):
        return kind is gen.Kind.TEXT
    if isinstance(decoded, Num):
        return kind not in (gen.Kind.TEXT, gen.Kind.MULTI)
    return False


def _decode(kind: gen.Kind, decoded: Decoded, address: int) -> object | None:
    """Turn the wire value into what the tree stores, or ``None`` to drop it.

    An out-of-range value is dropped rather than truncated: a bank index or a
    fader position that wrapped would be a plausible-looking lie.
    """
    if isinstance(decoded, Block):
        # The frame whatever the block's length: a short read zero-fills the
        # tail, an extra value is ignored.
        raw = decoded.values[: gen.METER_COUNT]
        raw += (0,) * (gen.METER_COUNT - len(raw))
        return RealtimeStatus(raw=raw)
    if isinstance(decoded, Text):
        # A device secret the dump volunteers in the clear must never be stored.
        if address in gen.SENSITIVE_ADDRESSES:
            return gen.REDACTED_PLACEHOLDER
        return decoded.text
    value = decoded.value
    if kind is gen.Kind.U14:
        return value if value <= gen.FULL_SCALE else None
    if kind is gen.Kind.U16:
        return value if value <= 0xFFFF else None
    if kind is gen.Kind.U7:
        return value & 0x7F
    if kind is gen.Kind.BOOL:
        return value != 0
    if kind is gen.Kind.BPM:
        return value // gen.TEMPO_BPM_SCALE if value <= gen.FULL_SCALE else None
    raise ValueError(f"no decode for {kind}")


def _decode_stream(msg: bytes) -> Update | RenderedString | None:
    """What one unframed MIDI3 message carries, before any of it is folded.

    The decode half of :meth:`DeviceState.apply`, on its own so that the
    model's stream link can see the address and value of every message it
    ingests -- a request's reply is recognised by exactly that, whether or not
    the fold then stores it (a reply carrying the value already held is deduped
    to nothing, yet it answers the request all the same). Returns the
    :class:`Update` the message is, the :class:`RenderedString` reply that
    stays outside the table, or ``None`` for a non-Kemper or unparseable
    message.
    """
    parsed = NrpnHeader.parse(msg)
    if parsed is None:
        return None
    header, vals = parsed
    function = header.function
    flat = header.page * 128 + header.number
    decoded: Decoded

    # The extended ($06) and extended-string ($07) addresses are 5x7-bit
    # encoded and do not fit the fixed header layout, so decode both off the
    # raw message.
    if function == nrpn.FUNCTION_EXT_PARAM:
        ext = nrpn.parse_extended_param(msg)
        if ext is None:
            return None
        flat, value = ext
        decoded = Num(value)
    elif function == nrpn.FUNCTION_EXT_STRING_PARAM:
        ext_string = nrpn.parse_extended_string(msg)
        if ext_string is None:
            return None
        flat, text = ext_string
        decoded = Text(text)
    elif function == nrpn.FUNCTION_STRING_PARAM:
        decoded = Text(_ascii_until_nul(vals))
    elif function == nrpn.FUNCTION_SINGLE_PARAM:
        if len(vals) < 2:
            return None
        decoded = Num(u14(vals[0], vals[1]))
    elif function == nrpn.FUNCTION_MULTI_PARAM:
        decoded = Block(tuple(value for _number, value in nrpn.multi_values(0, vals)))
    elif function == nrpn.FUNCTION_RENDERED_STRING_REPLY:
        rendered = nrpn.parse_rendered_string(msg)
        if rendered is None:
            return None
        page, number, value, text = rendered
        return RenderedString(page, number, value, text)
    else:
        return None
    return Update(Channel.STREAM, Phase.LIVE, flat, decoded)


def _ascii_until_nul(data: bytes) -> str:
    end = data.find(0)
    if end >= 0:
        data = data[:end]
    return "".join(chr(b) for b in data)
