"""``DeviceModel`` — an async store over a Profiler's two channels.

Two layers:

- :class:`libkp.state.DeviceState` is the **pure, network-free core**: a
  plain-data tree with one fold, :meth:`~libkp.state.DeviceState.apply_update`,
  that every value goes through whichever wire carried it.
- :class:`DeviceModel` is the **async handle**, and the only object in libkp
  that holds a socket to the device. It owns a MIDI3 **stream link** (the meter
  lane, the parameter pushes, every command and every request) and, by
  default, a CBOR **control link** (the state dump and the morph position, which
  the stream never carries). Both feed one funnel that is the single writer of
  the tree, so an app sees one handle, one state tree and one event stream, and
  never a channel name except in :class:`~libkp.state.ChannelChanged`.

This is a store
---------------

Four access points, mirroring a UI store:

- :meth:`state` — ``getState``: an independent snapshot.
- :meth:`subscribe` — the **store**: a fresh snapshot is queued when *slow*
  state changed, coalesced to at most once per ingested chunk on either wire.
- :meth:`events` — the granular delta stream (every
  :class:`libkp.state.DeviceEvent`, including the fast ones).
- :meth:`status` — the **fast lane**: poll this per animation frame for the
  meter/tuner frame (equals ``state().status``).

Parameters, requests and actions
--------------------------------

- **Parameters** (``set_*``) — settable values the device stores. They go out as
  14-bit NRPN ``$01`` Single Parameter Changes; the device applies the write
  silently and does *not* echo it back on the stream, so follow a set with
  :meth:`DeviceModel.request_param` when :meth:`state` should confirm the new
  value.
- **Requests** (``request_*``, :meth:`refresh`) — reads. Each one is
  request/reply: it goes out through the request lane, which keeps at most
  :data:`libkp._generated.MAX_IN_FLIGHT_REQUESTS` on the wire, and resolves
  with the value that answers it or fails after
  :data:`libkp._generated.REQUEST_TIMEOUT_MS`, never retrying. The reply folds
  into the tree on its way, so :meth:`state` agrees with what was returned.
- **Actions** (verbs) — momentary presses and live expression that carry no
  stored value. They go out as 7-bit Control Change messages from the
  :mod:`libkp.control` vocabulary and are *not* reflected in state.
- **Navigation** (:meth:`DeviceModel.navigate_to`, :meth:`DeviceModel.step_rig`,
  :meth:`DeviceModel.step_bank`, :meth:`DeviceModel.select_slot`) — the one
  way to load a rig, described below.

The Navigator
-------------

A rig load is the one command that can wedge the device: a second load
arriving while the first is still landing leaves it on a delayed fuse that
only a power cycle clears. So nothing in libkp sends a load directly --
:meth:`DeviceModel.send_control` and :meth:`DeviceModel.send_raw` refuse one
with :class:`~libkp.errors.RigLoadRequiresNavigatorError` before a byte is
written -- and a client *aims* instead. The Navigator serialises the loads:
the first aim goes out at once as the documented pair (the bank preselect,
CC47, then the slot load, CC50-54, that commits it) and is *in flight* for
:data:`libkp._generated.RIG_LOAD_SETTLE_MS`; every aim that arrives meanwhile
only moves the target, and when the settle elapses the final target is sent,
once. A burst of taps therefore costs two loads however long it is. The device
reports its position on both wires as it lands, and a report that matches the
aim retires it (:class:`~libkp.state.NavigationSettled`); an aim it never
confirms -- one past the last rig, where it stays put and says so -- is
dropped :data:`libkp._generated.PENDING_WINDOW_MS` after its move settled
(:class:`~libkp.state.NavigationDropped`). The settle is never shortened by an
early report: it is the measured time the device needs, not a guess.
:attr:`~libkp.state.DeviceState.navigation` shows the aim and whether a move
is in flight, and :attr:`~libkp.state.DeviceState.aimed_rig_index` is where
the client is headed -- the slot a rig browser should highlight.

The two links
-------------

The stream is required: :meth:`DeviceModel.connect` fails without it, and
losing it is losing the connection. The control link is opened beside it
(:attr:`ControlPolicy.BEST_EFFORT`, the default): its state dump folds the
morph and everything else the channel carries into the tree, and its live
pushes keep the morph moving. Failing to open it, or losing it, only
*degrades* the connection (:attr:`~libkp.state.Connection.DEGRADED`); it is
never reopened on its own unless asked (:attr:`ReconnectPolicy.control_reopen`,
or :meth:`DeviceModel.reopen_control`), because every socket to the device is
a cost it pays (docs/11). Reconnecting the stream after a loss is opt-in the
same way (:attr:`ReconnectPolicy.stream`): by default the model reports
:class:`~libkp.state.Disconnected` and stops, exactly as before.

Every socket is opened through :class:`libkp.session.Session`, whose ledger
spaces sockets to one device apart; the model adds no sleeps of its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import warnings
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from enum import Enum

from . import _generated as gen
from . import _routes, nrpn, params
from . import control as control_mod
from ._broadcast import Broadcast as _Broadcast
from ._lane import RequestLane
from ._link import StreamLink
from .cbor import ControlItem, ControlLink
from .control import Control
from .errors import (
    ChannelDisconnectedError,
    ChannelOffError,
    ChannelSessionError,
    ChannelTooSoonError,
    DisconnectedError,
    RequestDisconnectedError,
    RequestError,
    RequestTimeoutError,
    RequestUnreadableError,
    RigLoadRequiresNavigatorError,
    SessionError,
    UnknownSlotError,
)
from .nav import Dropped, NavAction, NavigatorState, Send, Settled, StartSettle, StartWindow
from .protocol import PORT
from .state import (
    ApplyOutcome,
    Block,
    Channel,
    ChannelChanged,
    ChannelState,
    Connected,
    Connection,
    ConnectionChanged,
    CurrentPosition,
    DeviceEvent,
    DeviceState,
    Disconnected,
    NavDrop,
    Navigation,
    NavigationDropped,
    NavigationSettled,
    Num,
    Phase,
    RealtimeStatus,
    RenderedString,
    RequestTimedOut,
    SyncCompleted,
    Text,
    Update,
    _decode_stream,
)

__all__ = [
    "DeviceModel",
    "ConnectOptions",
    "ControlPolicy",
    "SyncStrategy",
    "ReconnectPolicy",
    "Backoff",
    "RealtimeStatus",
    "DeviceEvent",
    "ApplyOutcome",
    "DeviceState",
]

#: Product byte addressed in outbound SysEx (0x00 = Profiler).
PRODUCT: int = nrpn.PRODUCT_PROFILER
#: Device byte addressed in outbound SysEx (0x7F = omni).
DEVICE: int = nrpn.DEVICE_OMNI
#: MIDI channel used for Control Change commands (0 = channel 1).
CC_CHANNEL: int = 0
#: How long after the dump trigger the dump phase ends if the end marker never
#: comes, in seconds. The marker is the :data:`libkp._generated.DUMP_END_RUNS`-th
#: run based at :data:`libkp._generated.DUMP_END_ADDRESS`: a dump has two
#: sections, and each closes with one.
DUMP_SETTLE: float = gen.DUMP_SETTLE_MS / 1000.0
#: The least time between two control opens, in seconds.
CONTROL_REOPEN_MIN_GAP: float = gen.CONTROL_REOPEN_MIN_GAP_MS / 1000.0
#: How long a rig load is in flight after it is sent, in seconds.
RIG_LOAD_SETTLE: float = gen.RIG_LOAD_SETTLE_MS / 1000.0
#: How long after its move settled an unconfirmed aim is kept, in seconds.
PENDING_WINDOW: float = gen.PENDING_WINDOW_MS / 1000.0

#: The controls that load a rig, refused by :meth:`DeviceModel.send_control`.
#: :class:`~libkp.control.BankSelect` loads nothing by itself but exists only
#: to qualify a Program Change, so it is refused with it.
_RIG_LOAD_CONTROLS = (
    control_mod.LoadSlot,
    control_mod.Up,
    control_mod.Down,
    control_mod.ProgramChange,
    control_mod.BankSelect,
)
#: One past the last flat address the page/number request forms can name; at
#: or above it a request goes out in its extended (``$46`` / ``$47``) form.
_FLAT_ADDRESS_LIMIT = 128 * 128


# ---------------------------------------------------------------------------
# Connect options
# ---------------------------------------------------------------------------


class ControlPolicy(Enum):
    """Whether, and how firmly, the model opens the CBOR control link."""

    #: Never open it. The connection reports :attr:`~libkp.state.Connection.CONNECTED`
    #: and the morph stays unknown. For tooling, and for a second model beside
    #: one that already holds the control link.
    OFF = "off"
    #: Open it beside the stream; failing to, or losing it, degrades the
    #: connection and nothing more. The default.
    BEST_EFFORT = "best_effort"
    #: The connect fails unless both links open. For tooling that exists to
    #: read the dump.
    REQUIRED = "required"


class SyncStrategy(Enum):
    """What :meth:`DeviceModel.connect` asks the device for once the stream is up."""

    #: Nothing. The tree fills in only as the device pushes.
    OFF = "off"
    #: The request burst: every ``request = true`` row of the routing table
    #: (the string tags, each effect slot's type and state, the bank preview,
    #: the position, and the numeric rows the tree tracks) through the request
    #: lane. 46 requests; the device answers all of them inside ~50 ms
    #: (docs/11). The default.
    STREAM_BURST = "stream_burst"


@dataclass(frozen=True, slots=True)
class Backoff:
    """How long to wait before redialing a lost stream: ``initial`` seconds,
    doubling on each failed attempt up to ``max``."""

    initial: float = gen.RECONNECT_DELAY_MS / 1000.0
    max: float = gen.RECONNECT_MAX_DELAY_MS / 1000.0

    @classmethod
    def default_stream(cls) -> Backoff:
        """The spec's reconnect pacing: 4 s doubling to 30 s -- what a
        long-running dashboard opts into."""
        return cls()

    def after(self, delay: float) -> float:
        """The delay that follows ``delay``."""
        return min(delay * 2, self.max)


@dataclass(frozen=True, slots=True)
class ReconnectPolicy:
    """What the model does on its own when a link goes away. Both default to
    nothing: reconnecting is a choice, since every socket costs the device."""

    #: Redial the stream after it is lost, with this backoff; ``None`` reports
    #: :class:`~libkp.state.Disconnected` and stops.
    stream: Backoff | None = None
    #: Reopen a failed or lost control link, at most once every this many
    #: seconds (never closer than
    #: :data:`libkp._generated.CONTROL_REOPEN_MIN_GAP_MS`), while the stream is
    #: up; ``None`` leaves it to :meth:`DeviceModel.reopen_control`.
    control_reopen: float | None = None


@dataclass(frozen=True, slots=True)
class ConnectOptions:
    """Everything :meth:`DeviceModel.connect` can be told. The defaults are
    what an app wants: both links, the request burst, no reconnecting."""

    port: int = PORT
    control: ControlPolicy = ControlPolicy.BEST_EFFORT
    sync: SyncStrategy = SyncStrategy.STREAM_BURST
    reconnect: ReconnectPolicy = field(default_factory=ReconnectPolicy)


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------


class DeviceModel:
    """An async handle to a live :class:`~libkp.state.DeviceState` store synced
    from a Profiler's stream and control channel.

    Example::

        model = await DeviceModel.connect("192.168.1.50")
        snapshots = model.subscribe()
        await model.set_effect_enabled("REV", False)
        state = await snapshots.get()
        print(state.effect("REV").on, state.morph)
        await model.close()
    """

    __slots__ = (
        "_ip",
        "_options",
        "_state",
        "_snapshots",
        "_events",
        "_listeners",
        "_lane",
        "_stream",
        "_control",
        "_tasks",
        "_supervisor",
        "_epoch",
        "_closed",
        "_last_control_open",
        "_dump_open",
        "_dump_end_runs",
        "_dump_timer",
        "_nav",
        "_settle_timer",
        "_window_timer",
    )

    def __init__(self, ip: str, options: ConnectOptions) -> None:
        self._ip = ip
        self._options = options
        self._state = DeviceState()
        self._snapshots = _Broadcast()
        self._events = _Broadcast()
        self._listeners: list[Callable[[DeviceEvent], None]] = []
        self._lane = RequestLane(self._send, self._on_request_timeout)
        self._stream: StreamLink | None = None
        self._control: ControlLink | None = None
        #: The tasks of the current life: the sync burst, the control open, a
        #: scheduled control reopen. Cancelled together when the life ends.
        self._tasks: set[asyncio.Task] = set()
        #: The task recovering from a stream loss (and, with a policy, running
        #: the reconnect loop). At most one at a time.
        self._supervisor: asyncio.Task | None = None
        #: Bumped whenever a life ends, so a late callback or task from a
        #: previous life can recognise itself and touch nothing.
        self._epoch = 0
        self._closed = False
        self._last_control_open = float("-inf")
        self._dump_open = False
        #: Runs based at ``DUMP_END_ADDRESS`` folded since the trigger: each
        #: of the dump's two sections closes with one, and the second ends it.
        self._dump_end_runs = 0
        self._dump_timer: asyncio.TimerHandle | None = None
        #: The Navigator's state machine, and the one timer of each kind it
        #: can have armed: re-arming replaces the last, and both are cancelled
        #: when a life ends, so a timer from a previous life never fires into
        #: the next.
        self._nav = NavigatorState()
        self._settle_timer: asyncio.TimerHandle | None = None
        self._window_timer: asyncio.TimerHandle | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @classmethod
    async def connect(
        cls, ip: str, port: int = PORT, *, options: ConnectOptions | None = None
    ) -> DeviceModel:
        """Connect to ``ip``, open the stream, and -- by ``options`` -- start
        the control link and the request burst.

        Returns once the stream is established; the tree fills in as the
        device answers. The stream is required and any failure to open it
        propagates as a :class:`~libkp.errors.SessionError`; the control link
        is opened in the background unless the policy is
        :attr:`ControlPolicy.REQUIRED`, in which case its failure also fails
        the connect, with nothing left open. A ``port`` other than the default
        overrides ``options.port``.
        """
        if options is None:
            options = ConnectOptions()
        if port != PORT:
            options = replace(options, port=port)
        model = cls(ip, options)
        try:
            await model._open()
        except BaseException:
            await model.close()
            raise
        return model

    async def close(self) -> None:
        """Close both links and report :class:`~libkp.state.Disconnected`.

        Receivers stay open and simply see no more items. Idempotent.
        """
        if self._closed:
            return
        self._closed = True
        self._epoch += 1
        supervisor = self._supervisor
        self._supervisor = None
        if supervisor is not None and supervisor is not asyncio.current_task():
            supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await supervisor
        await self._drop_links(lost=False)
        self._set_connection(Connection.DISCONNECTED)
        self._publish()

    async def __aenter__(self) -> DeviceModel:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def reopen_control(self) -> None:
        """Open the control link again after it failed or was lost.

        Refused with :class:`~libkp.errors.ChannelTooSoonError` inside
        :data:`libkp._generated.CONTROL_REOPEN_MIN_GAP_MS` of the last control
        open, with :class:`~libkp.errors.ChannelOffError` when the policy is
        :attr:`ControlPolicy.OFF`, and with
        :class:`~libkp.errors.ChannelDisconnectedError` while the stream is
        down. A link already open, or already opening, is left alone: the
        call returns at once, since what was asked for is there or under way,
        and a live socket is never dropped to dial another. A failure to open
        raises :class:`~libkp.errors.ChannelSessionError` and leaves the
        channel :attr:`~libkp.state.ChannelState.UNAVAILABLE`.
        """
        if self._options.control is ControlPolicy.OFF:
            raise ChannelOffError()
        if self._closed or self._state.channels.stream is not ChannelState.OPEN:
            raise ChannelDisconnectedError()
        if self._state.channels.control in (ChannelState.CONNECTING, ChannelState.OPEN):
            return
        remaining = self._last_control_open + CONTROL_REOPEN_MIN_GAP - _now()
        if remaining > 0:
            raise ChannelTooSoonError(remaining)
        try:
            await self._open_control(self._epoch)
        except SessionError as exc:
            raise ChannelSessionError(exc) from exc

    # ------------------------------------------------------------------
    # Store access
    # ------------------------------------------------------------------

    def state(self) -> DeviceState:
        """An independent snapshot of the current state (``getState``)."""
        return self._state.snapshot()

    def status(self) -> RealtimeStatus:
        """The latest FAST meter/tuner frame — poll this per animation frame."""
        return self._state.status

    @property
    def connected(self) -> bool:
        """True while the stream is open -- :attr:`~libkp.state.Connection.CONNECTED`
        or :attr:`~libkp.state.Connection.DEGRADED`, since a missing control
        link takes nothing away from what the stream carries."""
        return self._state.connection in (Connection.CONNECTED, Connection.DEGRADED)

    def subscribe(self) -> asyncio.Queue:
        """Subscribe to the **store**: a fresh :class:`~libkp.state.DeviceState`
        snapshot each time *slow* state changes, coalesced to at most one per
        ingested chunk.

        Joining broadcasts one fresh snapshot to every subscriber, so the new
        one starts from the current tree rather than from the next change.
        """
        queue = self._snapshots.subscribe()
        self._snapshots.send(self._state.snapshot())
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """Drop a queue returned by :meth:`subscribe`."""
        self._snapshots.unsubscribe(queue)

    def events(self) -> asyncio.Queue:
        """Subscribe to the granular delta stream — every
        :class:`~libkp.state.DeviceEvent`, including the FAST ones."""
        return self._events.subscribe()

    def unsubscribe_events(self, queue: asyncio.Queue) -> None:
        """Drop a queue returned by :meth:`events`."""
        self._events.unsubscribe(queue)

    def add_event_listener(self, callback: Callable[[DeviceEvent], None]) -> None:
        """Register a synchronous callback invoked for every granular event.

        Exceptions raised by a listener are suppressed so one bad consumer cannot
        take down the ingest task.
        """
        self._listeners.append(callback)

    def remove_event_listener(self, callback: Callable[[DeviceEvent], None]) -> None:
        """Unregister a callback added with :meth:`add_event_listener`."""
        with contextlib.suppress(ValueError):
            self._listeners.remove(callback)

    # ------------------------------------------------------------------
    # Parameters — NRPN $01, 14-bit (0-16383), state-tracked
    # ------------------------------------------------------------------

    async def set_param(self, page: int, number: int, value: int) -> None:
        """Set an arbitrary numeric parameter (``$01`` Single Parameter Change).

        The escape hatch for any address without a named setter, and the backbone
        every parameter setter routes through.
        """
        await self._enqueue(nrpn.set_single(PRODUCT, DEVICE, page, number, value))

    async def set_gain(self, value: int) -> None:
        """Set the amp Gain, 0–16383 (NRPN ``0x0A/4``)."""
        await self.set_param(gen.AMP_PAGE, gen.GAIN_NUMBER, value)

    async def set_rig_volume(self, value: int) -> None:
        """Set the Rig Volume, 0–16383 (NRPN ``0x04/1``)."""
        await self.set_param(gen.PAGE_RIG_SETTINGS, gen.RIG_VOLUME_NUMBER, value)

    async def set_main_volume(self, value: int) -> None:
        """Set the Main Output Volume, 0–16383 (NRPN ``0x7F/0``)."""
        await self.set_param(gen.SYSTEM_PAGE, gen.MAIN_VOLUME_NUMBER, value)

    async def set_monitor_volume(self, value: int) -> None:
        """Set the Monitor Output Volume, 0–16383 (NRPN ``0x7F/2``)."""
        await self.set_param(gen.SYSTEM_PAGE, gen.MONITOR_VOLUME_NUMBER, value)

    async def set_effect_enabled(self, slot: str, on: bool) -> None:
        """Turn an effect slot on or off via a ``$01`` write to number 3.

        ``slot`` is A/B/C/D/X/MOD/DLY/REV, case-insensitive. Raises
        :class:`~libkp.errors.UnknownSlotError` on an unknown name.
        """
        await self.set_param(_slot_page(slot), params.EFFECT_PARAM_STATE, 1 if on else 0)

    async def set_effect_mix(self, slot: str, value: int) -> None:
        """Set an effect slot's dry/wet Mix, 0–16383 (NRPN ``<slot>/4``)."""
        await self.set_param(_slot_page(slot), params.EFFECT_PARAM_MIX, value)

    async def set_tempo_bpm(self, bpm: int) -> None:
        """Set the Tempo in whole beats per minute (NRPN ``0x04/0``).

        The wire value is ``bpm * 64``, clamped to the 14-bit maximum (~255 BPM).
        The momentary counterpart is :meth:`tap_tempo`.
        """
        value = min(max(bpm, 0) * gen.TEMPO_BPM_SCALE, gen.FULL_SCALE)
        await self.set_param(gen.PAGE_RIG_SETTINGS, gen.TEMPO_NUMBER, value)

    # ------------------------------------------------------------------
    # Requests — request/reply through the lane
    # ------------------------------------------------------------------

    async def request_param(self, page: int, number: int) -> int:
        """Request one numeric parameter (``$41``) and return the 14-bit value
        the ``$01`` reply carries.

        The reply also folds into the tree. Raises
        :class:`~libkp.errors.RequestTimeoutError` when the device does not
        answer, and :class:`~libkp.errors.RequestUnreadableError` at once,
        sending nothing, for an address the stream cannot answer (the morph
        position) -- and after the fact for a reply wider than the 14 bits a
        ``$01`` carries: only a value from the other wire resolving the same
        address could be, and one that does not fit the stream's word is not
        the stream's answer.
        """
        flat = page * 128 + number
        value = await self._request_num(flat, nrpn.request_single(PRODUCT, DEVICE, page, number))
        if value > gen.FULL_SCALE:
            raise RequestUnreadableError(flat)
        return value

    async def request_string(self, page: int, number: int) -> str:
        """Request one string parameter (``$43``) and return the ``$03`` reply's
        text -- a page-0 tag such as the rig name."""
        flat = page * 128 + number
        return await self._request_text(flat, nrpn.request_string(PRODUCT, DEVICE, page, number))

    async def request_ext_param(self, address: int) -> int:
        """Request a numeric parameter at a flat or extended address (``$46``)
        and return the value the ``$06`` reply carries -- the device's current
        bank or rig slot, for instance."""
        return await self._request_num(
            address, nrpn.request_extended_param(PRODUCT, DEVICE, address)
        )

    async def request_ext_string(self, address: int) -> str:
        """Request a string parameter at a flat or extended address (``$47``)
        and return the ``$07`` reply's text -- a bank-preview name, for
        instance."""
        return await self._request_text(
            address, nrpn.request_extended_string(PRODUCT, DEVICE, address)
        )

    async def request_render(self, page: int, number: int, value: int) -> str:
        """Request a parameter value rendered to its exact display text (``$7C``)
        and return it.

        The ``$3C`` reply is matched by its page, number and value; it is also
        raised as a :class:`~libkp.state.RenderedString` event, and stores
        nothing.
        """
        flat = page * 128 + number
        self._check_readable(flat)
        text = await self._lane.request(
            ("render", page, number, value),
            flat,
            nrpn.request_rendered_string(PRODUCT, DEVICE, page, number, value),
        )
        return str(text)

    async def refresh(self) -> None:
        """Ask the device for every value the tree tracks: each ``request =
        true`` row of the routing table, through the request lane.

        This is the connect-time sync (:attr:`SyncStrategy.STREAM_BURST`), and
        it may be run again at any time. Read-only: it changes nothing on the
        device. Returns once every request has been answered, or raises
        :class:`~libkp.errors.RequestTimeoutError` for the first that was not
        -- the others still landed in the tree.
        """
        await self._request_rows(route for route in gen.STATE_ROUTES if route.request)

    async def refresh_rig(self) -> None:
        """Re-request the rig strings and every effect slot's Type/On-Off: the
        subset of :meth:`refresh` that describes the loaded rig."""
        await self._request_rows(
            route for route in gen.STATE_ROUTES if route.request and route.field in _RIG_FIELDS
        )

    async def refresh_bank(self) -> None:
        """Request the current bank's five-slot name preview (rig / amp / cabinet
        names): the subset of :meth:`refresh` that fills in
        :attr:`~libkp.state.DeviceState.bank`.

        The device also pushes this block unasked on a bank change, so a
        controller need only call this once at connect.
        """
        await self._request_rows(
            route
            for route in gen.STATE_ROUTES
            if route.request and route.field in _BANK_PREVIEW_FIELDS
        )

    async def refresh_position(self) -> None:
        """Ask the device where it is: the current bank and rig slot, the subset
        of :meth:`refresh` behind
        :attr:`~libkp.state.DeviceState.current_bank` and
        :attr:`~libkp.state.DeviceState.current_rig_slot`.

        Only needed once, at connect: the device pushes an unsolicited ``$06``
        for whichever of the two changed on every subsequent rig change, whoever
        caused it.
        """
        await self._request_rows(
            route for route in gen.STATE_ROUTES if route.request and route.field in _POSITION_FIELDS
        )

    def apply_cbor(self, address: int, value: int) -> None:
        """Fold one CBOR value into the tree by hand.

        **Deprecated.** The model opens the control link itself and folds what
        it carries, so nothing outside the model needs to feed it CBOR; this
        stays only for callers written against the old two-session pattern and
        goes through the same funnel as the link's own values.
        """
        warnings.warn(
            "DeviceModel.apply_cbor is deprecated: the model's control link folds "
            "CBOR values itself",
            DeprecationWarning,
            stacklevel=2,
        )
        if value < 0:
            return
        if self._fold(Update(Channel.CONTROL, Phase.LIVE, address, Num(value))):
            self._snapshots.send(self._state.snapshot())

    async def send_beacon(
        self, init: bool = True, tuner: bool = True, lease_secs: int = 30
    ) -> None:
        """Send the bidirectional beacon, asking the device to stream a parameter
        set and to keep sending until the lease expires."""
        await self._enqueue(nrpn.beacon(init, tuner, lease_secs, product=PRODUCT))

    # ------------------------------------------------------------------
    # Actions — CC, momentary/expression, NOT stored in state
    # ------------------------------------------------------------------

    async def send_control(self, control: Control, channel: int = CC_CHANNEL) -> None:
        """Send an arbitrary :class:`~libkp.control.Control` — the generic entry
        point behind every action convenience method below.

        A control that loads a rig -- :class:`~libkp.control.LoadSlot`,
        :class:`~libkp.control.Up`, :class:`~libkp.control.Down`,
        :class:`~libkp.control.ProgramChange` and the
        :class:`~libkp.control.BankSelect` that qualifies one -- is refused
        with :class:`~libkp.errors.RigLoadRequiresNavigatorError` before any
        byte is written: loads go through the Navigator alone, so two can never
        overlap on the wire.
        """
        if isinstance(control, _RIG_LOAD_CONTROLS):
            raise RigLoadRequiresNavigatorError(type(control).__name__)
        message = control.message(channel)
        _refuse_rig_loads(message)
        await self._enqueue(message)

    async def bank(self, n: int) -> None:
        """Preselect bank ``n`` (1-based; CC47). Loads nothing: it takes effect
        with the next slot load, which is the Navigator's to send."""
        await self.send_control(control_mod.BankPreselect(max(n - 1, 0)))

    async def tap_tempo(self) -> None:
        """Tap the tempo (CC30)."""
        await self.send_control(control_mod.TapTempo())

    async def tuner_mode(self, open_: bool) -> None:
        """Open (``True``) or close (``False``) the tuner (CC31)."""
        await self.send_control(control_mod.TunerMode(open_))

    async def morph_button(self, rise: bool) -> None:
        """Morph button (CC80): ``rise`` to the morph target, else fall back."""
        await self.send_control(control_mod.MorphButton(rise))

    async def morph_pedal(self, value: int) -> None:
        """Set the morph pedal position 0–127 (CC11)."""
        await self.send_control(control_mod.MorphPedal(value))

    async def freeze(self, on: bool) -> None:
        """Delay + Reverb Freeze (CC35)."""
        await self.send_control(control_mod.Freeze(on))

    async def rotary_fast(self, fast: bool) -> None:
        """Rotary speaker speed (CC33)."""
        await self.send_control(control_mod.RotaryFast(fast))

    async def delay_infinity(self, on: bool) -> None:
        """Delay Infinity (CC34)."""
        await self.send_control(control_mod.DelayInfinity(on))

    async def toggle_all_modules(self) -> None:
        """Toggle every module A–REV on/off (CC16)."""
        await self.send_control(control_mod.ToggleAllModules())

    async def effect_button(self, n: int) -> None:
        """Press Effect Button ``n`` (I–IIII, clamped to 1..4; CC75–78)."""
        await self.send_control(control_mod.EffectButton(n))

    async def wah_pedal(self, value: int) -> None:
        """Set the wah pedal position 0–127 (CC1)."""
        await self.send_control(control_mod.WahPedal(value))

    async def pitch_pedal(self, value: int) -> None:
        """Set the pitch pedal position 0–127 (CC4)."""
        await self.send_control(control_mod.PitchPedal(value))

    async def volume_pedal(self, value: int) -> None:
        """Set the volume pedal position 0–127 (CC7)."""
        await self.send_control(control_mod.VolumePedal(value))

    async def panorama(self, value: int) -> None:
        """Set the panorama 0–127 (CC10)."""
        await self.send_control(control_mod.Panorama(value))

    async def send_raw(self, message: bytes) -> None:
        """Enqueue raw (pre-framing) MIDI bytes for the stream's writer.

        Refused with :class:`~libkp.errors.RigLoadRequiresNavigatorError`,
        with nothing written, when the bytes hold a Program Change (status
        ``0xC0``-``0xCF``) or a Control Change on one of the rig-loading
        controllers (:data:`libkp._generated.RIG_LOAD_CONTROLLERS`): the raw
        door is not a way around the Navigator.
        """
        _refuse_rig_loads(message)
        await self._enqueue(message)

    # ------------------------------------------------------------------
    # The Navigator — the only way to load a rig
    # ------------------------------------------------------------------

    def navigate_to(self, index: int) -> None:
        """Aim at a rig by its flat, 0-based index and return at once.

        The device's own numbering, and the only address that reaches a rig
        outside the current bank: the index divides by ``BANK_SLOTS``, so
        index 123 is bank 25, slot 4. The load goes out now if none is in
        flight, otherwise when the current one settles -- see the module
        notes: a burst of aims costs two loads however long it is.

        Nothing here assumes how many banks a device has. Aim past the end and
        the device stays where it is and says so, so
        :attr:`~libkp.state.DeviceState.current_rig_index` reflects where it
        actually is, and the aim is dropped after the pending window
        (:class:`~libkp.state.NavigationDropped`). While the stream is down the
        aim is dropped at once, the same way, rather than raising: an aim is
        not a command that failed, it is a destination the model could not
        reach.
        """
        self._navigate(max(index, 0))

    def step_rig(self, delta: int) -> None:
        """Aim ``delta`` rigs from :attr:`~libkp.state.DeviceState.aimed_rig_index`,
        floored at index 0, so a run of steps counts from the last step rather
        than from wherever the device has got to. Ignored while no position is
        known yet (before the connect burst has answered): there is nothing to
        step *from*, and doing nothing beats a guess. A step that lands where
        the aim already is sends nothing."""
        aimed = self._state.aimed_rig_index
        if aimed is None:
            return
        target = max(aimed + delta, 0)
        if target != aimed:
            self._navigate(target)

    def step_bank(self, forward: bool) -> None:
        """Aim one bank up (``True``) or down from
        :attr:`~libkp.state.DeviceState.aimed_rig_index`, keeping the slot,
        floored at index 0. Ignored while no position is known yet."""
        self.step_rig(gen.BANK_SLOTS if forward else -gen.BANK_SLOTS)

    def select_slot(self, slot: int) -> None:
        """Aim at ``slot`` (1..``BANK_SLOTS``) of the aimed bank -- the bank
        of :attr:`~libkp.state.DeviceState.aimed_rig_index`, so a slot tapped
        right after a bank step lands in the bank that step is heading for,
        which the device's own position would not yet show. Ignored for a slot
        out of range, and while no position is known yet: there is no bank to
        name."""
        if not 1 <= slot <= gen.BANK_SLOTS:
            return
        aimed = self._state.aimed_rig_index
        if aimed is None:
            return
        self._navigate(aimed // gen.BANK_SLOTS * gen.BANK_SLOTS + slot - 1)

    def _navigate(self, index: int) -> None:
        """One aim through the machine, its actions carried out, and a
        snapshot if the aim or the flight changed."""
        if self._run_nav(self._nav.navigate(index)):
            self._snapshots.send(self._state.snapshot())

    def _run_nav(self, actions: list[NavAction]) -> bool:
        """Carry out the machine's actions in order -- bytes, timers, events --
        then mirror its aim and flight into the tree. Returns whether that
        mirror changed, which is a slow change the caller reports.

        A send while the stream is down cannot be carried out: the machine is
        reset and the aim reported dropped, with nothing else of the list
        done, since the timer it would arm belongs to a move that never went.
        """
        epoch = self._epoch
        for action in actions:
            if isinstance(action, Send):
                if not self._send_rig_load(action.index):
                    self._nav = NavigatorState()
                    self._emit(NavigationDropped(action.index, NavDrop.UNCONFIRMED))
                    break
            elif isinstance(action, StartSettle):
                self._settle_timer = self._arm(
                    self._settle_timer, RIG_LOAD_SETTLE, self._on_settle_elapsed, epoch
                )
            elif isinstance(action, StartWindow):
                self._window_timer = self._arm(
                    self._window_timer, PENDING_WINDOW, self._on_window_elapsed, epoch
                )
            elif isinstance(action, Settled):
                self._emit(NavigationSettled(action.index))
            elif isinstance(action, Dropped):
                self._emit(NavigationDropped(action.index, NavDrop.UNCONFIRMED))
        return self._mirror_navigation()

    def _send_rig_load(self, index: int) -> bool:
        """The documented pair for a flat index: the absolute bank preselect
        (CC47), then the slot load (CC50-54) that commits it. Returns whether
        both were queued; they are not while the stream is down, and not when
        the writer has stopped draining, which is the same thing about to be
        noticed."""
        stream = self._stream
        if self._closed or stream is None or self._state.channels.stream is not ChannelState.OPEN:
            return False
        bank, slot = divmod(index, gen.BANK_SLOTS)
        try:
            stream.send_nowait(control_mod.BankPreselect(bank).message(CC_CHANNEL))
            stream.send_nowait(control_mod.LoadSlot(slot + 1).message(CC_CHANNEL))
        except asyncio.QueueFull:
            return False
        return True

    def _arm(
        self,
        current: asyncio.TimerHandle | None,
        delay: float,
        callback: Callable[[int], None],
        epoch: int,
    ) -> asyncio.TimerHandle:
        """Arm one of the Navigator's timers, replacing the one of its kind:
        a window armed for a new aim must not be cut short by the window of
        the aim it replaced."""
        if current is not None:
            current.cancel()
        return asyncio.get_running_loop().call_later(delay, callback, epoch)

    def _on_settle_elapsed(self, epoch: int) -> None:
        if epoch != self._epoch or self._closed:
            return
        self._settle_timer = None
        if self._run_nav(self._nav.settle_elapsed()):
            self._snapshots.send(self._state.snapshot())

    def _on_window_elapsed(self, epoch: int) -> None:
        if epoch != self._epoch or self._closed:
            return
        self._window_timer = None
        if self._run_nav(self._nav.window_elapsed()):
            self._snapshots.send(self._state.snapshot())

    def _on_position(self, index: int) -> bool:
        """A position the core folded, from either wire, forwarded to the
        machine. Returns whether the navigation changed, for the chunk's one
        snapshot."""
        return self._run_nav(self._nav.position(index))

    def _mirror_navigation(self) -> bool:
        """Keep :attr:`~libkp.state.DeviceState.navigation` equal to the
        machine's public half; returns whether it moved."""
        navigation = self._state.navigation
        nav = self._nav
        if navigation.aim == nav.aim and navigation.in_flight == nav.in_flight:
            return False
        self._state.navigation = Navigation(aim=nav.aim, in_flight=nav.in_flight)
        return True

    def _reset_navigation(self) -> None:
        """A life ended: no timer may fire into the next, and an aim the
        device can no longer be asked for is cleared without an event."""
        for timer in (self._settle_timer, self._window_timer):
            if timer is not None:
                timer.cancel()
        self._settle_timer = None
        self._window_timer = None
        self._nav = NavigatorState()
        self._mirror_navigation()

    # ------------------------------------------------------------------
    # Supervisor: one life of the connection
    # ------------------------------------------------------------------

    async def _open(self) -> None:
        """Bring one life up: the stream, then the sync burst and the control
        link. Raises :class:`~libkp.errors.SessionError` when the stream cannot
        be opened -- or, under :attr:`ControlPolicy.REQUIRED`, the control link
        cannot, in which case the stream is closed again first."""
        epoch = self._epoch
        self._set_channel(Channel.STREAM, ChannelState.CONNECTING)
        self._publish()
        try:
            stream = await StreamLink.open(self._ip, self._options.port)
        except SessionError:
            self._set_channel(Channel.STREAM, ChannelState.UNAVAILABLE)
            self._publish()
            raise
        self._stream = stream
        # One snapshot for the whole transition: the channel, then the
        # connection it brings up, never the tree half-moved between them.
        self._set_channel(Channel.STREAM, ChannelState.OPEN)
        self._set_connection(Connection.CONNECTED)
        self._publish()
        stream.start(
            lambda messages: self._on_stream_chunk(epoch, messages),
            lambda: self._on_stream_lost(epoch),
        )

        if self._options.sync is SyncStrategy.STREAM_BURST:
            self._spawn(self._sync_stream(epoch))

        if self._options.control is ControlPolicy.REQUIRED:
            try:
                await self._open_control(epoch)
            except SessionError:
                # The stream was up and is torn down with the attempt: it reads
                # ``LOST`` like any stream this life had and no longer has. The
                # caller -- ``connect`` closing, or the reconnect loop counting
                # the next attempt -- publishes the transition it ends in.
                await self._drop_links(lost=True)
                raise
        elif self._options.control is ControlPolicy.BEST_EFFORT:
            self._spawn(self._open_control_quietly(epoch))

    async def _sync_stream(self, epoch: int) -> None:
        """The connect-time burst, in the background: connect does not wait for
        the replies. Reports :class:`~libkp.state.SyncCompleted` when the last
        one has landed or timed out."""
        with contextlib.suppress(RequestError):
            await self.refresh()
        if epoch == self._epoch:
            self._emit(SyncCompleted(Channel.STREAM))

    async def _open_control(self, epoch: int) -> None:
        """The control task: dial, handshake, preamble, the dump trigger, then
        ``OPEN`` with the dump phase running. A failure anywhere before that
        leaves the channel ``UNAVAILABLE`` and the connection degraded, and
        raises the :class:`~libkp.errors.SessionError`."""
        self._set_channel(Channel.CONTROL, ChannelState.CONNECTING)
        self._publish()
        self._last_control_open = _now()
        try:
            link = await ControlLink.open(
                self._ip,
                self._options.port,
                lambda items: self._on_control_items(epoch, items),
                lambda: self._on_control_closed(epoch),
            )
        except SessionError:
            if epoch == self._epoch and not self._closed:
                self._set_channel(Channel.CONTROL, ChannelState.UNAVAILABLE)
                self._refresh_connection()
                self._publish()
                self._schedule_control_reopen(epoch)
            raise
        if epoch != self._epoch or self._closed:
            # A late open from a life that has since ended: not ours to keep.
            await link.close()
            return
        self._control = link
        # The dump begins before the link's first read is folded, so the
        # handshake tail's items -- which the link holds for that read --
        # fold as the dump they are.
        self._begin_dump(epoch)
        self._set_channel(Channel.CONTROL, ChannelState.OPEN)
        self._refresh_connection()
        self._publish()

    async def _open_control_quietly(self, epoch: int) -> None:
        """Best effort: the failure is already in the tree; nobody awaits it."""
        with contextlib.suppress(SessionError):
            await self._open_control(epoch)

    def _schedule_control_reopen(self, epoch: int) -> None:
        """With :attr:`ReconnectPolicy.control_reopen` set, try the control link
        again once the gap has passed since the last open."""
        gap = self._options.reconnect.control_reopen
        if gap is None:
            return
        self._spawn(self._reopen_control_later(epoch, max(gap, CONTROL_REOPEN_MIN_GAP)))

    async def _reopen_control_later(self, epoch: int, gap: float) -> None:
        await asyncio.sleep(max(self._last_control_open + gap - _now(), 0.0))
        if (
            epoch == self._epoch
            and not self._closed
            and self._state.channels.stream is ChannelState.OPEN
            and self._state.channels.control in (ChannelState.UNAVAILABLE, ChannelState.LOST)
        ):
            await self._open_control_quietly(epoch)

    def _on_stream_lost(self, epoch: int) -> None:
        """The stream's socket ended (read error or EOF), reported from its own
        task: hand recovery to a fresh task, since it has sockets to close."""
        if epoch != self._epoch or self._closed:
            return
        previous = self._supervisor
        self._supervisor = asyncio.get_running_loop().create_task(self._recover(epoch, previous))

    async def _recover(self, epoch: int, previous: asyncio.Task | None) -> None:
        """Tear the lost life down and, with a policy, dial again until the
        stream is back or the model is closed."""
        if epoch != self._epoch or self._closed:
            return
        self._epoch += 1
        if previous is not None and not previous.done():
            previous.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await previous
        await self._drop_links(lost=True)

        # One snapshot for the whole loss, published only now that both
        # sockets are closed and the connection has moved on.
        backoff = self._options.reconnect.stream
        if backoff is None:
            self._set_connection(Connection.DISCONNECTED)
            self._publish()
            return
        attempt, delay = 1, backoff.initial
        self._set_reconnecting(attempt)
        self._publish()
        while not self._closed:
            await asyncio.sleep(delay)
            if self._closed:
                return
            try:
                await self._open()
            except SessionError:
                attempt += 1
                delay = backoff.after(delay)
                self._set_reconnecting(attempt)
                self._publish()
                continue
            return

    async def _drop_links(self, *, lost: bool) -> None:
        """End the current life's sockets and tasks. Both links drop together:
        the control link is ``CLOSED`` (not lost -- it was not the one that
        went), the stream ``LOST`` or ``CLOSED`` by ``lost``. Publishes
        nothing: the caller sends the one snapshot for the transition this is
        part of, once the connection has moved with the channels."""
        tasks = list(self._tasks)
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._finish_dump(report=False)
        self._reset_navigation()
        control, self._control = self._control, None
        if control is not None:
            await control.close()
        self._set_channel(Channel.CONTROL, ChannelState.CLOSED)
        stream, self._stream = self._stream, None
        if stream is not None:
            await stream.close()
        self._set_channel(Channel.STREAM, ChannelState.LOST if lost else ChannelState.CLOSED)
        self._lane.fail_all()

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------------
    # Core: the single writer of the tree
    # ------------------------------------------------------------------

    def _on_stream_chunk(self, epoch: int, messages: list[bytes]) -> None:
        """Fold one read's worth of stream messages: fan out each granular
        event, answer any request the values satisfy, and -- if any message
        changed a slow field -- emit exactly one snapshot."""
        if epoch != self._epoch:
            return
        slow_changed = False
        for message in messages:
            decoded = _decode_stream(message)
            if decoded is None:
                continue
            if isinstance(decoded, RenderedString):
                self._emit(decoded)
                self._lane.resolve(
                    ("render", decoded.page, decoded.number, decoded.value), decoded.text
                )
            else:
                slow_changed = self._fold(decoded) or slow_changed
        if slow_changed:
            self._snapshots.send(self._state.snapshot())

    def _on_control_items(self, epoch: int, items: list[ControlItem]) -> None:
        """Fold one read's worth of control items, each with the phase the dump
        bookkeeping says it is in, and close the dump phase after the run that
        ends a dump: the :data:`libkp._generated.DUMP_END_RUNS`-th one based
        at :data:`libkp._generated.DUMP_END_ADDRESS` since the trigger -- the
        dump's system section and its rig section each close with one, and
        the first alone ends nothing. One snapshot per chunk, as for the
        stream."""
        if epoch != self._epoch:
            return
        slow_changed = False
        for item in items:
            phase = Phase.DUMP if self._dump_open else Phase.LIVE
            for address, decoded in item.values:
                # A negative value is not a parameter value on this channel
                # (the same rule as ``DeviceState.apply_cbor``).
                if isinstance(decoded, Num) and decoded.value < 0:
                    continue
                update = Update(Channel.CONTROL, phase, address, decoded)
                slow_changed = self._fold(update) or slow_changed
            if self._dump_open and item.base == gen.DUMP_END_ADDRESS:
                self._dump_end_runs += 1
                if self._dump_end_runs >= gen.DUMP_END_RUNS:
                    self._finish_dump(report=True)
        if slow_changed:
            self._snapshots.send(self._state.snapshot())

    def _fold(self, update: Update) -> bool:
        """One value through the funnel: fold it, raise its events, hand a
        position to the Navigator, and answer any request waiting on its
        address. Returns whether a slow field changed. A request is answered
        whether or not the fold stored anything: a reply carrying the value
        already held is deduped to nothing, yet it is the answer all the
        same."""
        outcome: ApplyOutcome = self._state.apply_update(update)
        slow_changed = outcome.slow_changed
        for event in outcome.events:
            self._emit(event)
            if isinstance(event, CurrentPosition):
                # Whichever wire carried it, and whichever half changed: the
                # machine only cares where the device says it is now.
                index = self._state.current_rig_index
                if index is not None:
                    slow_changed = self._on_position(index) or slow_changed
        decoded = update.decoded
        if isinstance(decoded, Num):
            self._lane.resolve((update.address, Num), decoded.value)
        elif isinstance(decoded, Text):
            self._lane.resolve((update.address, Text), decoded.text)
        elif isinstance(decoded, Block):
            for i, value in enumerate(decoded.values):
                self._lane.resolve((update.address + i, Num), value)
        return slow_changed

    def _on_control_closed(self, epoch: int) -> None:
        """The device ended the control socket: the channel is ``LOST``, the
        connection degraded, and the morph frozen where it was."""
        if epoch != self._epoch or self._closed:
            return
        link, self._control = self._control, None
        if link is not None:
            # Close the dead socket for the ledger's sake, off this callback.
            self._spawn(link.close())
        self._finish_dump(report=False)
        self._set_channel(Channel.CONTROL, ChannelState.LOST)
        self._refresh_connection()
        self._publish()
        self._schedule_control_reopen(epoch)

    def _begin_dump(self, epoch: int) -> None:
        """The trigger is written: every control item folds as
        :attr:`~libkp.state.Phase.DUMP` until the run that ends the dump (the
        second based at ``DUMP_END_ADDRESS``), or the settle time if it never
        comes."""
        self._finish_dump(report=False)
        self._state.begin_dump()
        self._dump_open = True
        self._dump_end_runs = 0
        self._dump_timer = asyncio.get_running_loop().call_later(
            DUMP_SETTLE, self._settle_dump, epoch
        )

    def _settle_dump(self, epoch: int) -> None:
        if epoch == self._epoch and self._dump_open:
            self._finish_dump(report=True)

    def _finish_dump(self, *, report: bool) -> None:
        if self._dump_timer is not None:
            self._dump_timer.cancel()
            self._dump_timer = None
        if not self._dump_open:
            return
        self._dump_open = False
        self._state.end_dump()
        if report:
            self._emit(SyncCompleted(Channel.CONTROL))

    # ------------------------------------------------------------------
    # Transitions
    # ------------------------------------------------------------------

    def _set_channel(self, channel: Channel, state: ChannelState) -> None:
        """Move one link and raise the event that says so; nothing if it is
        already there. Publishes no snapshot: a channel moves as part of a
        composite transition -- a connect, a loss, a control open or its
        failure -- and that transition sends one snapshot when all of it has
        happened (:meth:`_publish`), so a subscriber never sees the tree
        half-moved."""
        channels = self._state.channels
        current = channels.stream if channel is Channel.STREAM else channels.control
        if current is state:
            return
        if channel is Channel.STREAM:
            channels.stream = state
        else:
            channels.control = state
        self._emit(ChannelChanged(channel, state))

    def _set_connection(self, connection: Connection) -> None:
        """Move the connection and raise the compatibility event it
        corresponds to, then :class:`~libkp.state.ConnectionChanged`; nothing
        if it is already there. The snapshot is the composite transition's,
        as for :meth:`_set_channel`."""
        previous = self._state.connection
        if previous is connection:
            return
        self._state.connection = connection
        self._state.reconnect_attempt = 0
        if connection is Connection.CONNECTED and previous in (
            Connection.DISCONNECTED,
            Connection.RECONNECTING,
        ):
            self._emit(Connected())
        elif connection is Connection.DISCONNECTED:
            self._emit(Disconnected())
        self._emit(ConnectionChanged(connection))

    def _set_reconnecting(self, attempt: int) -> None:
        """Each attempt is its own transition: the attempt number is what a
        client shows, so every increment is reported. The snapshot is the
        caller's, as for :meth:`_set_connection`."""
        self._state.connection = Connection.RECONNECTING
        self._state.reconnect_attempt = attempt
        self._emit(ConnectionChanged(Connection.RECONNECTING))

    def _publish(self) -> None:
        """The one snapshot a composite transition ends in."""
        self._snapshots.send(self._state.snapshot())

    def _refresh_connection(self) -> None:
        """While the stream is up, the connection is degraded exactly when the
        control link was asked for and is not there."""
        if self._state.connection not in (Connection.CONNECTED, Connection.DEGRADED):
            return
        control = self._state.channels.control
        degraded = self._options.control is not ControlPolicy.OFF and control in (
            ChannelState.UNAVAILABLE,
            ChannelState.LOST,
        )
        self._set_connection(Connection.DEGRADED if degraded else Connection.CONNECTED)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _emit(self, event: DeviceEvent) -> None:
        self._events.send(event)
        for listener in self._listeners:
            try:
                listener(event)
            except Exception:  # pragma: no cover - a listener must not break ingest
                pass

    def _on_request_timeout(self, address: int) -> None:
        self._emit(RequestTimedOut(address))

    async def _enqueue(self, message: bytes) -> None:
        """A command for the stream's writer."""
        stream = self._stream
        if self._closed or stream is None or self._state.channels.stream is not ChannelState.OPEN:
            raise DisconnectedError()
        await stream.send(message)

    async def _send(self, message: bytes) -> None:
        """A request for the stream's writer, on the lane's behalf."""
        stream = self._stream
        if self._closed or stream is None:
            raise RequestDisconnectedError()
        await stream.send(message)

    def _check_readable(self, address: int) -> None:
        """A request needs an address the stream can answer, and then the
        stream: the table routes the morph position to the control channel
        alone, and a ``$41`` for it draws no reply, so that refusal comes
        first -- it is a property of the address, true whether or not the
        stream is up."""
        route = _routes.lookup(address)
        if route is not None and route.wire is gen.Wire.CONTROL:
            raise RequestUnreadableError(address)
        if self._closed or self._state.channels.stream is not ChannelState.OPEN:
            raise RequestDisconnectedError()

    async def _request_num(self, address: int, message: bytes) -> int:
        self._check_readable(address)
        return int(await self._lane.request((address, Num), address, message))

    async def _request_text(self, address: int, message: bytes) -> str:
        self._check_readable(address)
        return str(await self._lane.request((address, Text), address, message))

    async def _request_rows(self, routes: Iterable[gen.Route]) -> None:
        """Issue one request per row, all through the lane at once (it paces
        them), and report the first timeout after the rest have landed."""
        results = await asyncio.gather(
            *(self._request_row(route) for route in routes), return_exceptions=True
        )
        timeout: RequestTimeoutError | None = None
        for result in results:
            if isinstance(result, RequestTimeoutError):
                timeout = timeout or result
            elif isinstance(result, BaseException):
                raise result
        if timeout is not None:
            raise timeout

    async def _request_row(self, route: gen.Route) -> object:
        """The request a routing-table row calls for: a page/number form below
        the flat limit, the extended form at or above it, text or numeric by
        the row's kind."""
        address = route.address
        page, number = divmod(address, 128)
        if route.kind is gen.Kind.TEXT:
            if address < _FLAT_ADDRESS_LIMIT:
                message = nrpn.request_string(PRODUCT, DEVICE, page, number)
            else:
                message = nrpn.request_extended_string(PRODUCT, DEVICE, address)
            return await self._request_text(address, message)
        if address < _FLAT_ADDRESS_LIMIT:
            message = nrpn.request_single(PRODUCT, DEVICE, page, number)
        else:
            message = nrpn.request_extended_param(PRODUCT, DEVICE, address)
        return await self._request_num(address, message)


#: The rows :meth:`DeviceModel.refresh_rig` asks for: the page-0 tags and each
#: slot's type and state.
_RIG_FIELDS = frozenset(
    {
        gen.Field.RIG_NAME,
        gen.Field.RIG_AUTHOR,
        gen.Field.RIG_DATE,
        gen.Field.RIG_COMMENT,
        gen.Field.AMP_NAME,
        gen.Field.CABINET_NAME,
        gen.Field.EFFECT_TYPE,
        gen.Field.EFFECT_ON,
    }
)
_BANK_PREVIEW_FIELDS = frozenset(
    {gen.Field.BANK_RIG_NAME, gen.Field.BANK_AMP_NAME, gen.Field.BANK_CABINET_NAME}
)
_POSITION_FIELDS = frozenset({gen.Field.CURRENT_BANK, gen.Field.CURRENT_RIG_SLOT})


def _now() -> float:
    """The event loop's monotonic clock, the one the ledger keeps time by."""
    return asyncio.get_running_loop().time()


def _refuse_rig_loads(message: bytes) -> None:
    """Raise :class:`~libkp.errors.RigLoadRequiresNavigatorError` if the MIDI
    bytes would load a rig: a Program Change status, or a Control Change on
    one of :data:`libkp._generated.RIG_LOAD_CONTROLLERS`. Every status byte
    is examined, wherever it sits in the buffer (a control may render several
    messages back to back); the data bytes between them are all below
    ``0x80`` and cannot be mistaken for one, so malformed input is refused
    the same way everywhere rather than parsed around."""
    for i, byte in enumerate(message):
        status = byte & 0xF0
        if status == gen.PROGRAM_CHANGE_STATUS:
            raise RigLoadRequiresNavigatorError("a Program Change")
        if status == gen.CONTROL_CHANGE_STATUS and i + 1 < len(message):
            controller = message[i + 1]
            if controller in gen.RIG_LOAD_CONTROLLERS:
                raise RigLoadRequiresNavigatorError(f"CC{controller}")


def _slot_page(slot: str) -> int:
    page = params.effect_slot_page(slot)
    if page is None:
        raise UnknownSlotError(slot)
    return page
