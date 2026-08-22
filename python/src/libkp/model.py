"""``DeviceModel`` — an async store over a Profiler's live stream.

Two layers:

- :class:`libkp.state.DeviceState` is the **pure, network-free core**: a
  plain-data tree that decodes one already-unframed message at a time via
  :meth:`~libkp.state.DeviceState.apply`.
- :class:`DeviceModel` is the **async handle**. :meth:`DeviceModel.connect`
  opens a :class:`libkp.session.Session`, spawns an ingest task that owns the
  socket and an :class:`libkp.midi3.Unframer`, spawns a writer task that drains
  the command queue to the wire, and kicks off a read-only initial sync.

This is a store
---------------

Four access points, mirroring a UI store:

- :meth:`state` — ``getState``: an independent snapshot.
- :meth:`subscribe` — the **store**: a fresh snapshot is queued only when *slow*
  state changed, coalesced to at most once per ingested stream chunk.
- :meth:`events` — the granular delta stream (every
  :class:`libkp.state.DeviceEvent`, including the fast ones).
- :meth:`status` — the **fast lane**: poll this per animation frame for the
  meter/tuner frame (equals ``state().status``).

Parameters vs actions
---------------------

- **Parameters** (``set_*``) — settable values the device stores. They go out as
  14-bit NRPN ``$01`` Single Parameter Changes; the device applies the write
  silently and does *not* echo it back on the stream, so follow a set with
  :meth:`DeviceModel.request_param` when :meth:`state` should confirm the new
  value — the ``$41`` reply flows through normal ingest.
- **Actions** (verbs) — momentary presses and live expression that carry no
  stored value. They go out as 7-bit Control Change messages from the
  :mod:`libkp.control` vocabulary and are *not* reflected in state.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Iterable

from . import _generated as gen
from . import control as control_mod
from . import midi3, nrpn, params
from .control import Control
from .errors import DisconnectedError, SessionError, UnknownSlotError
from .protocol import PORT
from .session import PROTOCOL_MIDI3_STREAM, Session
from .state import (
    ApplyOutcome,
    Connected,
    Connection,
    CurrentPosition,
    DeviceEvent,
    DeviceState,
    Disconnected,
    RealtimeStatus,
)

__all__ = ["DeviceModel", "RealtimeStatus", "DeviceEvent", "ApplyOutcome", "DeviceState"]

#: Product byte addressed in outbound SysEx (0x00 = Profiler).
PRODUCT: int = nrpn.PRODUCT_PROFILER
#: Device byte addressed in outbound SysEx (0x7F = omni).
DEVICE: int = nrpn.DEVICE_OMNI
#: MIDI channel used for Control Change commands (0 = channel 1).
CC_CHANNEL: int = 0
#: Read idle gap driving the ingest loop; short so it reacts per packet.
READ_IDLE: float = 0.03
#: Max bytes per stream read.
READ_MAX: int = 64 * 1024
#: Depth of each subscriber queue before the oldest item is dropped.
QUEUE_DEPTH: int = 256
#: String tags requested by the initial read-only sync, in request order.
SYNC_STRING_TAGS: tuple[int, ...] = (1, 2, 4, 3, 10, 32)


class _Broadcast:
    """Fan-out of values to per-subscriber queues, dropping the oldest on overflow.

    A slow consumer never blocks the ingest task; for snapshots, dropping an
    intermediate value loses nothing because the latest is always complete.
    """

    __slots__ = ("_queues",)

    def __init__(self) -> None:
        self._queues: list[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_DEPTH)
        self._queues.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with contextlib.suppress(ValueError):
            self._queues.remove(queue)

    def send(self, value: object) -> None:
        for queue in self._queues:
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(value)


class DeviceModel:
    """An async handle to a live :class:`~libkp.state.DeviceState` store synced
    from a Profiler's stream.

    Example::

        model = await DeviceModel.connect("192.168.1.50")
        snapshots = model.subscribe()
        await model.set_effect_enabled("REV", False)
        state = await snapshots.get()
        print(state.effect("REV").on)
        await model.close()
    """

    __slots__ = (
        "_session",
        "_state",
        "_snapshots",
        "_events",
        "_commands",
        "_tasks",
        "_closed",
        "_listeners",
    )

    def __init__(self, session: Session) -> None:
        self._session = session
        self._state = DeviceState()
        self._snapshots = _Broadcast()
        self._events = _Broadcast()
        self._commands: asyncio.Queue[bytes] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._closed = False
        self._listeners: list[Callable[[DeviceEvent], None]] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @classmethod
    async def connect(
        cls,
        ip: str,
        port: int = PORT,
        protocol: str = PROTOCOL_MIDI3_STREAM,
        sync: bool = True,
    ) -> DeviceModel:
        """Connect to ``ip:port``, open the streaming protocol, spawn the ingest
        and writer tasks, and kick off a read-only initial sync.

        Returns once the session is established; the state fills in as the
        device's replies stream back. Subscribe *before* awaiting fresh events.
        """
        session = await Session.connect(ip, port)
        try:
            outcome = await session.handshake([protocol], READ_IDLE)
            await session.write_session_preamble()
        except BaseException:
            await session.close()
            raise

        model = cls(session)
        # Connecting is a SLOW transition: flip the flag, then broadcast the
        # first snapshot and the granular Connected event.
        model._state.connection = Connection.CONNECTED
        model._emit(Connected())
        model._snapshots.send(model._state.snapshot())

        loop = asyncio.get_running_loop()
        model._tasks.append(loop.create_task(model._ingest(outcome.response_tail())))
        model._tasks.append(loop.create_task(model._writer()))

        if sync:
            await model.refresh_rig()
            await model.refresh_bank()
        return model

    async def close(self) -> None:
        """Stop the ingest/writer tasks and close the socket."""
        if self._closed:
            return
        self._closed = True
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        await self._session.close()
        self._disconnect()

    async def __aenter__(self) -> DeviceModel:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

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
        """True while the ingest task still holds an open session."""
        return self._state.connection is Connection.CONNECTED

    def subscribe(self) -> asyncio.Queue:
        """Subscribe to the **store**: a fresh :class:`~libkp.state.DeviceState`
        snapshot each time *slow* state changes, coalesced to at most one per
        ingested stream chunk."""
        return self._snapshots.subscribe()

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
    # Read-only requests
    # ------------------------------------------------------------------

    async def refresh_rig(self) -> None:
        """Re-request the rig strings and every effect slot's Type/On-Off.

        Also the initial sync run at connect time. Neither a parameter nor an
        action: it only issues value *requests* and changes nothing on the device.
        """
        for tag in SYNC_STRING_TAGS:
            await self._enqueue(nrpn.request_string(PRODUCT, DEVICE, nrpn.PAGE_STRINGS, tag))
        for _name, page in params.EFFECT_SLOTS:
            await self._enqueue(
                nrpn.request_single(PRODUCT, DEVICE, page, params.EFFECT_PARAM_TYPE)
            )
            await self._enqueue(
                nrpn.request_single(PRODUCT, DEVICE, page, params.EFFECT_PARAM_STATE)
            )

    async def refresh_bank(self) -> None:
        """Request the current bank's five-slot name preview (rig / amp / cabinet
        names) as extended strings (``$47``).

        The ``$07`` replies fold into :attr:`~libkp.state.DeviceState.bank`.
        Read-only: it changes nothing on the device. The device also pushes this
        block unasked on a bank change, so a controller need only call this once
        at connect.
        """
        for field_ in params.BankPreviewField:
            for slot in range(1, params.BANK_SLOTS + 1):
                address = params.bank_preview_address(field_, slot)
                await self._enqueue(nrpn.request_extended_string(PRODUCT, DEVICE, address))

    def set_current_position(self, bank: int | None, slot: int | None) -> None:
        """Fold a :class:`~libkp.cbor.StateSnapshot`'s current bank and rig slot
        into the state tree, emitting a
        :class:`~libkp.state.CurrentPosition` event and broadcasting a fresh
        snapshot.

        The MIDI3 stream never reports these indices; a controller learns them by
        running :func:`libkp.cbor.fetch_state_snapshot` over a separate CBOR
        session — which the device wants done *before* this streaming session
        opens, not concurrently — and applying the result here. Only the non-``None``
        arguments overwrite; a ``None`` leaves the current value untouched.
        Synchronous: no I/O.
        """
        if bank is not None:
            self._state.current_bank = bank
        if slot is not None:
            self._state.current_rig_slot = slot
        self._emit(CurrentPosition(bank=bank, slot=slot))
        self._snapshots.send(self._state.snapshot())

    async def request_param(self, page: int, number: int) -> None:
        """Request one numeric parameter (``$41``); the reply arrives as ``$01``."""
        await self._enqueue(nrpn.request_single(PRODUCT, DEVICE, page, number))

    async def request_render(self, page: int, number: int, value: int) -> None:
        """Request a parameter value rendered to its exact display text (``$7C``).

        A **streaming request/response**: it enqueues the request and returns
        immediately. Watch :meth:`events` for the matching
        :class:`~libkp.state.RenderedString` carrying the rendered text.
        """
        await self._enqueue(nrpn.request_rendered_string(PRODUCT, DEVICE, page, number, value))

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
        point behind every action convenience method below."""
        await self._enqueue(control.message(channel))

    async def select_rig(self, rig: int) -> None:
        """Select rig slot 1–5 in the current bank (CC50–54)."""
        await self.send_control(control_mod.LoadSlot(rig))

    async def rig_up(self) -> None:
        """Step to the next rig (CC48)."""
        await self.send_control(control_mod.Up())

    async def rig_down(self) -> None:
        """Step to the previous rig (CC49)."""
        await self.send_control(control_mod.Down())

    async def bank(self, n: int) -> None:
        """Preselect bank ``n`` (1-based; CC47). Takes effect with the next rig."""
        await self.send_control(control_mod.BankPreselect(max(n - 1, 0)))

    async def select_rig_index(self, index: int) -> None:
        """Load a rig by its flat, 0-based index.

        This is the device's own numbering, and the only address that reaches a
        rig outside the current bank. Sent as the documented pair: the absolute
        bank preselect (CC47) followed by the slot load (CC50-54) that commits
        it. The index divides by ``BANK_SLOTS``, so index 123 is bank 25, slot 4.

        Nothing here assumes how many banks a device has. Aim past the end and
        the device simply stays where it is -- and says so in the Bank Select /
        Program Change report that follows, so
        :attr:`~libkp.state.DeviceState.current_rig_index` always reflects where
        it actually landed, not where this aimed.
        """
        bank, slot = divmod(index, gen.BANK_SLOTS)
        await self.bank(bank + 1)
        await self.select_rig(slot + 1)

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
        """Enqueue raw (pre-framing) MIDI bytes for the writer task."""
        await self._enqueue(message)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _enqueue(self, message: bytes) -> None:
        if self._closed or not self.connected:
            raise DisconnectedError()
        await self._commands.put(message)

    async def _writer(self) -> None:
        """Drain the command queue to the wire, framing each message."""
        try:
            while True:
                message = await self._commands.get()
                await self._session.write_all(midi3.frame(message))
        except asyncio.CancelledError:
            raise
        except SessionError:
            self._disconnect()

    async def _ingest(self, tail: bytes) -> None:
        """Own the socket: read, unframe, decode into the shared state, and drive
        the coalesced snapshot store plus the granular event fan-out."""
        unframer = midi3.Unframer()
        try:
            # Decode anything that rode in on the handshake acceptance tail.
            self._apply_chunk(unframer.push(tail))
            while True:
                chunk = await self._session.read_once(READ_IDLE, READ_MAX)
                if chunk:
                    self._apply_chunk(unframer.push(chunk))
        except asyncio.CancelledError:
            raise
        except SessionError:
            self._disconnect()

    def _apply_chunk(self, messages: Iterable[bytes]) -> None:
        """Apply every message in one ingested chunk: fan out each granular event
        and — if any message changed a slow field — emit exactly one snapshot."""
        slow_changed = False
        for message in messages:
            outcome = self._state.apply(message)
            for event in outcome.events:
                self._emit(event)
            slow_changed = slow_changed or outcome.slow_changed
        if slow_changed:
            self._snapshots.send(self._state.snapshot())

    def _emit(self, event: DeviceEvent) -> None:
        self._events.send(event)
        for listener in self._listeners:
            try:
                listener(event)
            except Exception:  # pragma: no cover - a listener must not break ingest
                pass

    def _disconnect(self) -> None:
        """Record the Disconnected transition (a SLOW change)."""
        if self._state.connection is Connection.DISCONNECTED:
            return
        self._state.connection = Connection.DISCONNECTED
        self._emit(Disconnected())
        self._snapshots.send(self._state.snapshot())


def _slot_page(slot: str) -> int:
    page = params.effect_slot_page(slot)
    if page is None:
        raise UnknownSlotError(slot)
    return page
