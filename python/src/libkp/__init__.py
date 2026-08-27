"""libkp — the Kemper Profiler network protocol in pure Python.

Layers, lowest to highest:

- :mod:`libkp.protocol` — the TagStream wire encoding and the discovery poll.
- :mod:`libkp.discovery` — async UDP broadcast discovery.
- :mod:`libkp.session` — TCP connect plus the line-based protocol handshake.
- :mod:`libkp.midi3` — the 4-byte stream framing that carries MIDI over TCP.
- :mod:`libkp.nrpn` — Kemper NRPN-over-SysEx builders and parsers.
- :mod:`libkp.control` — the 7-bit CC/PC control vocabulary.
- :mod:`libkp.params` / :mod:`libkp.registry` — offline name and type lookups.
- :mod:`libkp.cbor` — the native CBOR control channel: codec and control link.
- :mod:`libkp.state` — the device-state tree and its pure decode routing.
- :mod:`libkp.nav` — the Navigator's pure state machine, the one way a rig is
  loaded.
- :mod:`libkp.model` — :class:`~libkp.model.DeviceModel`, the async store over
  the stream and the control link.

Beside the layers, :mod:`libkp.testing` holds :class:`~libkp.testing.FakeDevice`,
an in-process Profiler for driving all of the above without a device.

Constants and lookup tables come from :mod:`libkp._generated`, which is emitted
from the shared spec; the protocol logic is hand-written here and held to the
shared conformance vectors.

Quick start::

    import asyncio
    from libkp import DeviceModel, find_first

    async def main():
        reply = await find_first()
        model = await DeviceModel.connect(reply.ip)
        snapshots = model.subscribe()
        state = await snapshots.get()
        print(state.rig.name, state.status.loudness)
        await model.close()

    asyncio.run(main())
"""

from __future__ import annotations

from ._generated import SPEC_VERSION
from .cbor import (
    CborSession,
    StateSnapshot,
    extract_snapshot,
    fetch_state_snapshot,
    numeric_values,
    param_write,
    state_dump_request,
)
from .control import (
    Control,
    ModuleSlot,
    control_from_op,
    program_change,
    slot_enable_cc,
)
from .discovery import DiscoveryOptions, DiscoveryPort, Reply, discover, find_first
from .errors import (
    ChannelDisconnectedError,
    ChannelError,
    ChannelOffError,
    ChannelSessionError,
    ChannelTooSoonError,
    CommandError,
    ConnectError,
    ConnectionClosedError,
    DisconnectedError,
    DiscoverError,
    FieldOverrunError,
    LibKPError,
    ParseError,
    PortUnavailableError,
    ProtocolRejectedError,
    RequestDisconnectedError,
    RequestError,
    RequestTimeoutError,
    RequestUnreadableError,
    RigLoadRequiresNavigatorError,
    SessionError,
    TimeoutErrorLibKP,
    TooShortError,
    UnknownSlotError,
)
from .midi3 import Unframer, frame, is_kemper_sysex
from .model import (
    Backoff,
    ConnectOptions,
    ControlPolicy,
    DeviceModel,
    ReconnectPolicy,
    SyncStrategy,
)
from .nav import (
    Dropped,
    NavAction,
    NavigatorState,
    Send,
    Settled,
    StartSettle,
    StartWindow,
)
from .nrpn import (
    NrpnHeader,
    beacon,
    control_change,
    ext_decode,
    ext_encode,
    multi_values,
    parse_extended_string,
    parse_rendered_string,
    request_multi,
    request_rendered_string,
    request_single,
    request_string,
    set_single,
    sysex,
    u14,
    u14_split,
)
from .params import (
    EFFECT_SLOTS,
    describe,
    describe_numeric,
    effect_category_name,
    effect_slot_page,
    effect_type_name,
    page_name,
    param_name,
    string_tag_name,
)
from .protocol import PORT, TagStream, build_poll_request
from .registry import ParamDescriptor, ParamKind, descriptor, format_value
from .session import (
    CONNECTION_COOLDOWN,
    PROTOCOL_CBOR_CONTROL,
    PROTOCOL_MIDI3_STREAM,
    PROTOCOL_REQUEST_RESPONSE,
    HandshakeOutcome,
    Session,
)
from .state import (
    Amp,
    ApplyOutcome,
    Bank,
    BankPreview,
    BankSlot,
    BeatPulse,
    Block,
    Cabinet,
    Channel,
    ChannelChanged,
    Channels,
    ChannelState,
    Connected,
    Connection,
    ConnectionChanged,
    CurrentPosition,
    Decoded,
    DeviceEvent,
    DeviceState,
    Disconnected,
    Effect,
    EffectChanged,
    MorphButton,
    MorphChanged,
    NavDrop,
    Navigation,
    NavigationDropped,
    NavigationSettled,
    Num,
    Output,
    ParamChanged,
    Phase,
    RealtimeStatus,
    RenderedString,
    RequestTimedOut,
    Rig,
    RigChanged,
    Status,
    StringTag,
    SyncCompleted,
    TempoBpm,
    Text,
    Tuner,
    TunerDeviance,
    TunerNote,
    Update,
)

__version__ = "0.1.0"

__all__ = [
    "SPEC_VERSION",
    "__version__",
    # protocol / transport
    "PORT",
    "TagStream",
    "build_poll_request",
    "DiscoveryOptions",
    "Reply",
    "discover",
    "DiscoveryPort",
    "find_first",
    "Session",
    "HandshakeOutcome",
    "PROTOCOL_MIDI3_STREAM",
    "PROTOCOL_REQUEST_RESPONSE",
    "PROTOCOL_CBOR_CONTROL",
    "CONNECTION_COOLDOWN",
    "Unframer",
    "frame",
    "is_kemper_sysex",
    # messages
    "NrpnHeader",
    "sysex",
    "beacon",
    "u14",
    "u14_split",
    "set_single",
    "request_single",
    "request_multi",
    "request_string",
    "request_rendered_string",
    "control_change",
    "program_change",
    "multi_values",
    "ext_decode",
    "ext_encode",
    "parse_extended_string",
    "parse_rendered_string",
    # control vocabulary
    "Control",
    "ModuleSlot",
    "slot_enable_cc",
    "control_from_op",
    # lookups
    "EFFECT_SLOTS",
    "param_name",
    "page_name",
    "string_tag_name",
    "effect_type_name",
    "effect_category_name",
    "effect_slot_page",
    "describe",
    "describe_numeric",
    "ParamDescriptor",
    "ParamKind",
    "descriptor",
    "format_value",
    # state + model
    "Connection",
    "ChannelState",
    "Channels",
    "Navigation",
    "NavDrop",
    "DeviceState",
    "RealtimeStatus",
    "Rig",
    "Amp",
    "Cabinet",
    "Effect",
    "Tuner",
    "Output",
    "BankSlot",
    "Bank",
    "ApplyOutcome",
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
    "Update",
    "Decoded",
    "Num",
    "Text",
    "Block",
    "Channel",
    "Phase",
    "DeviceModel",
    "ConnectOptions",
    "ControlPolicy",
    "SyncStrategy",
    "ReconnectPolicy",
    "Backoff",
    # the Navigator's state machine
    "NavigatorState",
    "NavAction",
    "Send",
    "StartSettle",
    "StartWindow",
    "Settled",
    "Dropped",
    # cbor state-dump snapshot
    "CborSession",
    "StateSnapshot",
    "extract_snapshot",
    "numeric_values",
    "fetch_state_snapshot",
    "param_write",
    "state_dump_request",
    # errors
    "LibKPError",
    "ParseError",
    "PortUnavailableError",
    "TooShortError",
    "FieldOverrunError",
    "DiscoverError",
    "SessionError",
    "ConnectError",
    "ConnectionClosedError",
    "ProtocolRejectedError",
    "TimeoutErrorLibKP",
    "CommandError",
    "DisconnectedError",
    "UnknownSlotError",
    "RigLoadRequiresNavigatorError",
    "RequestError",
    "RequestDisconnectedError",
    "RequestTimeoutError",
    "RequestUnreadableError",
    "ChannelError",
    "ChannelOffError",
    "ChannelTooSoonError",
    "ChannelDisconnectedError",
    "ChannelSessionError",
]
