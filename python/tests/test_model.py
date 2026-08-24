"""The async :class:`libkp.model.DeviceModel` store, driven against a stand-in device.

Every ``FakeDevice`` binds a fresh ephemeral port, and both of the model's
links dial that one port, so a test that lets the control link open pays the
connection ledger's one-second cooldown between the two sockets. That is the
spacing the real device needs, and the point of the ledger, so the tests wait
it out rather than bypass it.
"""

from __future__ import annotations

import asyncio

import pytest
from fake_device import FakeDevice, answer_requests, ext_param, wait_for

from libkp import _generated as gen
from libkp import cbor
from libkp.errors import (
    ChannelOffError,
    ChannelTooSoonError,
    DisconnectedError,
    RequestDisconnectedError,
    RequestTimeoutError,
    RequestUnreadableError,
    SessionError,
    UnknownSlotError,
)
from libkp.model import (
    Backoff,
    ConnectOptions,
    ControlPolicy,
    DeviceModel,
    ReconnectPolicy,
    SyncStrategy,
)
from libkp.nrpn import PAGE_STRINGS, set_single, sysex, u14_split
from libkp.session import CONNECTION_COOLDOWN, PROTOCOL_CBOR_CONTROL, PROTOCOL_MIDI3_STREAM
from libkp.state import (
    Channel,
    ChannelChanged,
    ChannelState,
    Connected,
    Connection,
    ConnectionChanged,
    Disconnected,
    EffectChanged,
    RequestTimedOut,
    Status,
    SyncCompleted,
    TempoBpm,
)

RIG_NAME = sysex(0x00, 0x00, 0x03, PAGE_STRINGS, 1, b"Test Rig\x00")
REV_TYPE = set_single(0x00, 0x00, 0x3D, 0, 179)
TEMPO = set_single(0x00, 0x00, gen.PAGE_RIG_SETTINGS, gen.TEMPO_NUMBER, 7680)

#: No burst, no control link: the model as a bare stream, for tests about the
#: stream alone (what ``connect(sync=False)`` used to give).
QUIET = ConnectOptions(sync=SyncStrategy.OFF, control=ControlPolicy.OFF)
#: No burst, control link by default: for tests about the control link.
NO_SYNC = ConnectOptions(sync=SyncStrategy.OFF)


def meter_message(values: list[int]) -> bytes:
    payload = bytearray()
    for v in values:
        payload.extend(u14_split(v))
    return sysex(0x00, 0x00, 0x02, gen.PAGE_REALTIME, gen.METER_BLOCK_NUMBER, bytes(payload))


def run(coro):
    return asyncio.run(coro)


async def drain(queue: asyncio.Queue) -> list:
    """Everything currently queued, without waiting."""
    out = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


async def next_event(queue: asyncio.Queue, kind, timeout: float = 3.0):
    """Wait for the next event of type ``kind``, discarding the rest."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        event = await asyncio.wait_for(queue.get(), max(remaining, 0.001))
        if isinstance(event, kind):
            return event


# ---------------------------------------------------------------------------
# Connect + the stream burst
# ---------------------------------------------------------------------------


def test_refresh_issues_every_request_row_of_the_table():
    """The burst is the ``request = true`` rows: 46 read-only requests, in
    table order, each in the form its address calls for."""

    async def scenario():
        async with FakeDevice(responder=answer_requests) as device:
            model = await DeviceModel.connect(
                "127.0.0.1", device.port, options=ConnectOptions(control=ControlPolicy.OFF)
            )
            events = model.events()
            try:
                await next_event(events, SyncCompleted)
                return device.received, model.state()
            finally:
                await model.close()

    received, state = run(scenario())
    # Six string-tag requests, four rig/amp numerics, a Type + On/Off request
    # per effect slot, the three output volumes, the 15 bank-preview extended
    # strings (5 slots x rig/amp/cabinet), then the two extended-param
    # requests for the current bank and rig slot.
    assert len(received) == 6 + 4 + 16 + 3 + 15 + 2
    assert all(m[6] in (0x43, 0x41, 0x46, 0x47) for m in received), "sync must be read-only"
    assert received[0] == bytes.fromhex("f0002033007f43000001f7")  # Rig Name
    assert [m[6] for m in received[:6]] == [0x43] * 6
    assert [m[6] for m in received[6:29]] == [0x41] * 23
    assert [m[6] for m in received[29:44]] == [0x47] * 15
    assert [m[6] for m in received[44:]] == [0x46, 0x46]
    # Every reply landed in the tree on its way back.
    assert state.rig.name == "X"
    assert state.bank.slots[4].cabinet_name == "X"
    assert (state.current_bank, state.current_rig_slot) == (0, 0)


def test_connect_can_skip_the_initial_sync():
    async def scenario():
        async with FakeDevice() as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=QUIET)
            await asyncio.sleep(0.05)
            await model.close()
            return device.received

    assert run(scenario()) == []


def test_the_burst_completes_even_when_nothing_answers():
    """A silent device costs 46 timeouts, then ``SyncCompleted`` all the same."""

    async def scenario():
        async with FakeDevice() as device:
            model = await DeviceModel.connect(
                "127.0.0.1", device.port, options=ConnectOptions(control=ControlPolicy.OFF)
            )
            events = model.events()
            try:
                seen = []
                while True:
                    event = await asyncio.wait_for(events.get(), 3.0)
                    seen.append(event)
                    if isinstance(event, SyncCompleted):
                        return seen
            finally:
                await model.close()

    seen = run(scenario())
    assert sum(isinstance(e, RequestTimedOut) for e in seen) == 46
    assert seen[-1] == SyncCompleted(Channel.STREAM)


def test_connect_emits_connected_and_a_first_snapshot():
    async def scenario():
        async with FakeDevice() as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=QUIET)
            events = model.events()
            snapshots = model.subscribe()
            try:
                # Connected was emitted before we subscribed; joining sends a
                # fresh snapshot, and then a push drives one more.
                assert model.connected
                assert model.state().connection is Connection.CONNECTED
                joined = await asyncio.wait_for(snapshots.get(), 2.0)
                await device.push(TEMPO)
                snapshot = await asyncio.wait_for(snapshots.get(), 2.0)
                event = await asyncio.wait_for(events.get(), 2.0)
                return joined, snapshot, event
            finally:
                await model.close()

    joined, snapshot, event = run(scenario())
    assert joined.connection is Connection.CONNECTED
    assert joined.channels.stream is ChannelState.OPEN
    assert joined.channels.control is ChannelState.CLOSED
    assert joined.rig.tempo_bpm is None
    assert snapshot.rig.tempo_bpm == 120
    assert event == TempoBpm(120)


def test_close_raises_the_teardown_events_in_order():
    seen = []

    async def scenario():
        async with FakeDevice() as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=QUIET)
            # The connect-time events predate any subscriber; a listener added
            # afterwards sees the close: the stream channel, then the
            # compatibility event, then the transition it belongs to.
            model.add_event_listener(seen.append)
            await model.close()

    run(scenario())
    assert seen == [
        ChannelChanged(Channel.STREAM, ChannelState.CLOSED),
        Disconnected(),
        ConnectionChanged(Connection.DISCONNECTED),
    ]


def test_handshake_tail_is_decoded_before_the_first_read():
    async def scenario():
        async with FakeDevice(tail_messages=[RIG_NAME]) as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=QUIET)
            try:
                await wait_for(lambda: model.state().rig.name == "Test Rig")
                return model.state()
            finally:
                await model.close()

    assert run(scenario()).rig.name == "Test Rig"


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


def test_pushed_messages_update_the_state_tree():
    async def scenario():
        async with FakeDevice() as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=QUIET)
            try:
                await device.push(RIG_NAME)
                await device.push(REV_TYPE)
                await wait_for(lambda: model.state().effects[7].kind == 179)
                return model.state()
            finally:
                await model.close()

    state = run(scenario())
    assert state.rig.name == "Test Rig"
    assert state.effect("REV").type_name == "Easy Reverb"


def test_meter_frames_land_on_the_fast_lane_without_a_snapshot():
    values = [0, 0, 0, 4096, 9000, 0, 12000, 0, 0, 3000, 0]

    async def scenario():
        async with FakeDevice() as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=QUIET)
            snapshots = model.subscribe()
            await drain(snapshots)  # the join snapshot
            events = model.events()
            try:
                await device.push(meter_message(values))
                await wait_for(lambda: model.status().loudness == 3000)
                event = await asyncio.wait_for(events.get(), 2.0)
                return model.status(), snapshots.qsize(), event
            finally:
                await model.close()

    status, snapshot_count, event = run(scenario())
    assert list(status.raw) == values
    assert status.stack_level == 9000
    assert snapshot_count == 0, "a meter frame must not emit a snapshot"
    assert isinstance(event, Status)


def test_event_listeners_receive_granular_deltas():
    async def scenario():
        seen = []
        async with FakeDevice() as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=QUIET)
            model.add_event_listener(seen.append)
            try:
                await device.push(REV_TYPE)
                await wait_for(lambda: EffectChanged(7) in seen)
            finally:
                await model.close()
        return seen

    seen = run(scenario())
    assert EffectChanged(7) in seen


def test_a_raising_listener_cannot_break_ingest():
    async def scenario():
        async with FakeDevice() as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=QUIET)
            model.add_event_listener(lambda _event: (_ for _ in ()).throw(RuntimeError("boom")))
            try:
                await device.push(REV_TYPE)
                await wait_for(lambda: model.state().effects[7].kind == 179)
                return model.state().effects[7].kind
            finally:
                await model.close()

    assert run(scenario()) == 179


def test_subscribe_broadcasts_a_fresh_snapshot_to_everyone_on_join():
    async def scenario():
        async with FakeDevice() as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=QUIET)
            try:
                first = model.subscribe()
                second = model.subscribe()
                return first.qsize(), second.qsize()
            finally:
                await model.close()

    # The first subscriber got its own join and the second's; the second, one.
    assert run(scenario()) == (2, 1)


def test_unsubscribe_stops_delivery():
    async def scenario():
        async with FakeDevice() as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=QUIET)
            snapshots = model.subscribe()
            await drain(snapshots)
            model.unsubscribe(snapshots)
            try:
                await device.push(TEMPO)
                await wait_for(lambda: model.state().rig.tempo_bpm == 120)
                return snapshots.qsize()
            finally:
                await model.close()

    assert run(scenario()) == 0


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


def test_request_param_resolves_with_the_value_and_folds_it():
    def responder(message: bytes) -> list[bytes]:
        if message[6] == 0x41 and message[8:10] == bytes([gen.AMP_PAGE, gen.GAIN_NUMBER]):
            return [set_single(0x00, 0x00, gen.AMP_PAGE, gen.GAIN_NUMBER, 6925)]
        return []

    async def scenario():
        async with FakeDevice(responder=responder) as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=QUIET)
            try:
                value = await model.request_param(gen.AMP_PAGE, gen.GAIN_NUMBER)
                return value, model.state().amp.gain
            finally:
                await model.close()

    assert run(scenario()) == (6925, 6925)


def test_request_string_ext_param_and_render_resolve():
    render_reply = sysex(
        0x00, 0x00, 0x3C, gen.AMP_PAGE, gen.GAIN_NUMBER, bytes([0, 5]) + b"5.2\x00"
    )

    def responder(message: bytes) -> list[bytes]:
        function = message[6]
        if function == 0x43:
            return [RIG_NAME]
        if function == 0x46:
            return [ext_param(gen.CURRENT_BANK_ADDRESS, 7)]
        if function == 0x7C:
            return [render_reply]
        return []

    async def scenario():
        async with FakeDevice(responder=responder) as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=QUIET)
            try:
                name = await model.request_string(PAGE_STRINGS, 1)
                bank = await model.request_ext_param(gen.CURRENT_BANK_ADDRESS)
                text = await model.request_render(gen.AMP_PAGE, gen.GAIN_NUMBER, 5)
                return name, bank, text, model.state().current_bank
            finally:
                await model.close()

    assert run(scenario()) == ("Test Rig", 7, "5.2", 7)


def test_an_unanswered_request_times_out_and_is_never_retried():
    async def scenario():
        loop = asyncio.get_running_loop()
        async with FakeDevice() as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=QUIET)
            events = model.events()
            try:
                started = loop.time()
                with pytest.raises(RequestTimeoutError) as excinfo:
                    await model.request_param(gen.AMP_PAGE, gen.GAIN_NUMBER)
                elapsed = loop.time() - started
                event = await next_event(events, RequestTimedOut)
                await asyncio.sleep(0.05)
                return excinfo.value, elapsed, event, device.received
            finally:
                await model.close()

    error, elapsed, event, received = run(scenario())
    address = gen.AMP_PAGE * 128 + gen.GAIN_NUMBER
    assert error.address == address
    assert elapsed >= gen.REQUEST_TIMEOUT_MS / 1000.0
    assert event == RequestTimedOut(address)
    assert len(received) == 1, "a timed-out request is never retried"


def test_the_morph_is_unreadable_without_a_byte_on_the_wire():
    async def scenario():
        async with FakeDevice() as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=QUIET)
            try:
                with pytest.raises(RequestUnreadableError):
                    await model.request_param(PAGE_STRINGS, gen.MORPH_ADDRESS)
                await asyncio.sleep(0.05)
                return device.received
            finally:
                await model.close()

    assert run(scenario()) == []


def test_at_most_sixteen_requests_are_on_the_wire_at_once():
    """With nothing answering, the lane lets the first 16 out, then the next
    16 only as the first ones time out; nothing is dropped."""

    async def scenario():
        async with FakeDevice() as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=QUIET)
            burst = asyncio.ensure_future(model.refresh())
            try:
                await asyncio.sleep(0.1)
                first = len(device.received)
                await asyncio.sleep(gen.REQUEST_TIMEOUT_MS / 1000.0)
                second = len(device.received)
                return first, second
            finally:
                await model.close()
                with pytest.raises(RequestDisconnectedError):
                    await burst

    assert run(scenario()) == (gen.MAX_IN_FLIGHT_REQUESTS, 2 * gen.MAX_IN_FLIGHT_REQUESTS)


def test_refresh_reports_the_first_timeout_after_the_rest_landed():
    def all_but_the_rig_name(message: bytes) -> list[bytes]:
        if message[6] == 0x43 and message[9] == 1:
            return []
        return answer_requests(message)

    async def scenario():
        async with FakeDevice(responder=all_but_the_rig_name) as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=QUIET)
            try:
                with pytest.raises(RequestTimeoutError) as excinfo:
                    await model.refresh()
                return excinfo.value.address, model.state()
            finally:
                await model.close()

    address, state = run(scenario())
    assert address == 1
    assert state.rig.name is None
    assert state.amp.name == "X"
    assert state.effects[7].on is False


def test_requests_are_refused_once_disconnected():
    async def scenario():
        async with FakeDevice() as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=QUIET)
            await model.close()
            with pytest.raises(RequestDisconnectedError):
                await model.request_param(gen.AMP_PAGE, gen.GAIN_NUMBER)

    run(scenario())


# ---------------------------------------------------------------------------
# The control link
# ---------------------------------------------------------------------------


def test_default_options_open_the_control_link_and_fold_the_dump():
    async def scenario():
        async with FakeDevice(offer_cbor=True) as device:
            model = await DeviceModel.connect("127.0.0.1", device.port)
            events = model.events()
            snapshots = model.subscribe()
            try:
                seen = []
                while True:
                    event = await asyncio.wait_for(events.get(), 3.0)
                    seen.append(event)
                    if event == SyncCompleted(Channel.CONTROL):
                        break
                return seen, await drain(snapshots), model.state(), device
            finally:
                await model.close()

    seen, snapshots, state, device = run(scenario())
    control = [e for e in seen if isinstance(e, ChannelChanged) and e.channel is Channel.CONTROL]
    assert control == [
        ChannelChanged(Channel.CONTROL, ChannelState.CONNECTING),
        ChannelChanged(Channel.CONTROL, ChannelState.OPEN),
    ]
    assert not any(isinstance(e, ConnectionChanged) for e in seen), "never degraded"
    # The dump folded through the one funnel: the morph, the position, a name.
    assert state.connection is Connection.CONNECTED
    assert state.morph == 8192
    assert (state.current_bank, state.current_rig_slot) == (3, 1)
    assert state.rig.name == "Dump Rig"
    # One snapshot for the channel opening, exactly one for the whole dump.
    with_control = [s for s in snapshots if s.channels.control is ChannelState.OPEN]
    assert [s.morph for s in with_control] == [None, 8192]
    # One control socket, on which the trigger was the only thing written.
    assert device.connection_count(PROTOCOL_CBOR_CONTROL) == 1
    assert device.control.received_items == [cbor.state_dump_request()]


def test_a_live_push_during_the_dump_outranks_the_dump():
    """The dump is a copy taken when it was asked for; a value pushed on the
    stream while it streams is newer, so the dump's copy of that address must
    not undo it. (On the control channel itself everything inside the dump
    window is the dump -- the two cannot be told apart on that wire.)"""
    dump = [
        cbor.param_write(gen.PAGE_RIG_SETTINGS * 128 + gen.TEMPO_NUMBER, 100 * gen.TEMPO_BPM_SCALE),
        cbor.Tag(1, [2, gen.DUMP_END_ADDRESS, 0]),
    ]

    async def scenario():
        async with FakeDevice(offer_cbor=True, dump_items=[]) as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=NO_SYNC)
            events = model.events()
            try:
                await wait_for(
                    lambda: model.state().channels.control is ChannelState.OPEN, timeout=3.0
                )
                # The dump phase is open and the fake has served nothing yet: a
                # live tempo lands on the stream, then the dump's stale copy.
                await device.push(TEMPO)
                await wait_for(lambda: model.state().rig.tempo_bpm == 120)
                await device.push_items(dump)
                completed = await next_event(events, SyncCompleted)
                return completed, model.state().rig.tempo_bpm
            finally:
                await model.close()

    assert run(scenario()) == (SyncCompleted(Channel.CONTROL), 120)


def test_the_dump_phase_settles_without_an_end_marker():
    async def scenario():
        async with FakeDevice(offer_cbor=True, dump_items=[]) as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=NO_SYNC)
            events = model.events()
            try:
                loop = asyncio.get_running_loop()
                await next_event(events, ChannelChanged)  # connecting
                opened = await next_event(events, ChannelChanged)
                assert opened == ChannelChanged(Channel.CONTROL, ChannelState.OPEN)
                started = loop.time()
                completed = await next_event(events, SyncCompleted)
                return completed, loop.time() - started
            finally:
                await model.close()

    completed, elapsed = run(scenario())
    assert completed == SyncCompleted(Channel.CONTROL)
    assert elapsed >= gen.DUMP_SETTLE_MS / 1000.0 - 0.05


def test_a_rejected_control_link_degrades_the_connection():
    async def scenario():
        async with FakeDevice() as device:  # offers no control protocol
            model = await DeviceModel.connect("127.0.0.1", device.port, options=NO_SYNC)
            events = model.events()
            try:
                changed = await next_event(events, ConnectionChanged)
                state = model.state()
                # The stream keeps working.
                await device.push(TEMPO)
                await wait_for(lambda: model.state().rig.tempo_bpm == 120)
                return changed, state, model.connected
            finally:
                await model.close()

    changed, state, connected = run(scenario())
    assert changed == ConnectionChanged(Connection.DEGRADED)
    assert state.connection is Connection.DEGRADED
    assert state.channels.control is ChannelState.UNAVAILABLE
    assert state.channels.stream is ChannelState.OPEN
    assert connected


def test_control_policy_off_never_opens_a_second_connection():
    async def scenario():
        async with FakeDevice(offer_cbor=True) as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=QUIET)
            try:
                # Past the ledger's cooldown, when a control open would have landed.
                await asyncio.sleep(CONNECTION_COOLDOWN + 0.2)
                return model.state(), device.connection_count(PROTOCOL_CBOR_CONTROL)
            finally:
                await model.close()

    state, control_connections = run(scenario())
    assert state.connection is Connection.CONNECTED
    assert state.channels.control is ChannelState.CLOSED
    assert control_connections == 0


def test_required_control_fails_connect_with_nothing_left_open():
    async def scenario():
        async with FakeDevice() as device:
            with pytest.raises(SessionError):
                await DeviceModel.connect(
                    "127.0.0.1",
                    device.port,
                    options=ConnectOptions(control=ControlPolicy.REQUIRED, sync=SyncStrategy.OFF),
                )
            await wait_for(lambda: all(c.closed.is_set() for c in device.connections))
            return [c.selected for c in device.connections]

    selected = run(scenario())
    assert selected[0] == PROTOCOL_MIDI3_STREAM
    assert len(selected) == 2


def test_control_eof_is_lost_and_not_reopened():
    async def scenario():
        async with FakeDevice(offer_cbor=True) as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=NO_SYNC)
            events = model.events()
            try:
                # Let the dump land before the device hangs up on the link.
                await wait_for(lambda: model.state().morph == 8192, timeout=3.0)
                await drain(events)
                await device.hangup(PROTOCOL_CBOR_CONTROL)
                lost = await next_event(events, ChannelChanged)
                degraded = await next_event(events, ConnectionChanged)
                await asyncio.sleep(0.2)
                with pytest.raises(ChannelTooSoonError):
                    await model.reopen_control()
                return lost, degraded, model.state(), device.connection_count(PROTOCOL_CBOR_CONTROL)
            finally:
                await model.close()

    lost, degraded, state, control_connections = run(scenario())
    assert lost == ChannelChanged(Channel.CONTROL, ChannelState.LOST)
    assert degraded == ConnectionChanged(Connection.DEGRADED)
    assert state.channels.control is ChannelState.LOST
    assert state.morph == 8192, "the morph stays where it was"
    assert control_connections == 1


def test_reopen_control_is_refused_when_the_policy_is_off():
    async def scenario():
        async with FakeDevice(offer_cbor=True) as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=QUIET)
            try:
                with pytest.raises(ChannelOffError):
                    await model.reopen_control()
            finally:
                await model.close()

    run(scenario())


def test_reopen_control_recovers_a_degraded_connection():
    async def scenario():
        async with FakeDevice(offer_cbor=True, accepts=[PROTOCOL_MIDI3_STREAM]) as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=NO_SYNC)
            try:
                await wait_for(lambda: model.state().connection is Connection.DEGRADED, 3.0)
                # The device starts accepting the control protocol. The reopen
                # gap is thirty seconds by spec; back-date the last open so the
                # reopen is allowed now (the ledger still spaces the socket).
                device.accepts.add(PROTOCOL_CBOR_CONTROL)
                model._last_control_open = float("-inf")
                await model.reopen_control()
                await wait_for(lambda: model.state().morph == 8192, 3.0)
                return model.state()
            finally:
                await model.close()

    state = run(scenario())
    assert state.connection is Connection.CONNECTED
    assert state.channels.control is ChannelState.OPEN


def test_apply_cbor_is_deprecated_but_still_folds():
    async def scenario():
        async with FakeDevice() as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=QUIET)
            try:
                with pytest.warns(DeprecationWarning):
                    model.apply_cbor(gen.MORPH_ADDRESS, 100)
                return model.state().morph
            finally:
                await model.close()

    assert run(scenario()) == 100


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def test_parameter_setters_emit_single_parameter_changes():
    async def scenario():
        async with FakeDevice() as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=QUIET)
            try:
                await model.set_gain(8192)
                await model.set_effect_enabled("rev", True)
                await model.set_effect_mix("DLY", 4096)
                await model.set_tempo_bpm(120)
                await model.set_main_volume(9000)
                await model.set_monitor_volume(3000)
                await model.set_rig_volume(4096)
                await wait_for(lambda: len(device.received) >= 7)
            finally:
                await model.close()
            return device.received

    received = run(scenario())
    assert received[0] == set_single(0x00, 0x7F, gen.AMP_PAGE, gen.GAIN_NUMBER, 8192)
    assert received[1] == set_single(0x00, 0x7F, 0x3D, 3, 1)
    assert received[2] == set_single(0x00, 0x7F, 0x3C, 4, 4096)
    assert received[3] == set_single(0x00, 0x7F, gen.PAGE_RIG_SETTINGS, 0, 7680)
    assert received[4] == set_single(0x00, 0x7F, gen.SYSTEM_PAGE, 0, 9000)
    assert received[5] == set_single(0x00, 0x7F, gen.SYSTEM_PAGE, 2, 3000)
    assert received[6] == set_single(0x00, 0x7F, gen.PAGE_RIG_SETTINGS, 1, 4096)


def test_set_tempo_clamps_to_the_fourteen_bit_maximum():
    async def scenario():
        async with FakeDevice() as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=QUIET)
            try:
                await model.set_tempo_bpm(9999)
                await wait_for(lambda: len(device.received) >= 1)
            finally:
                await model.close()
            return device.received[0]

    assert run(scenario()) == set_single(0x00, 0x7F, gen.PAGE_RIG_SETTINGS, 0, gen.FULL_SCALE)


def test_actions_emit_control_changes():
    async def scenario():
        async with FakeDevice() as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=QUIET)
            try:
                await model.tap_tempo()
                await model.select_rig(3)
                await model.rig_up()
                await model.rig_down()
                await model.bank(4)
                await model.tuner_mode(True)
                await model.freeze(False)
                await wait_for(lambda: len(device.received) >= 7)
            finally:
                await model.close()
            return device.received

    received = run(scenario())
    assert received[:7] == [
        bytes([0xB0, 30, 1]),
        # select_rig / rig_up / rig_down are momentary: press then release.
        bytes([0xB0, 52, 1, 0xB0, 52, 0]),
        bytes([0xB0, 48, 1, 0xB0, 48, 0]),
        bytes([0xB0, 49, 1, 0xB0, 49, 0]),
        bytes([0xB0, 47, 3]),
        bytes([0xB0, 31, 1]),
        bytes([0xB0, 35, 0]),
    ]


def test_select_rig_index_sends_absolute_bank_then_slot():
    """Index 123 is bank 25 slot 4: CC47 value 24, then CC53 press/release."""

    async def scenario():
        async with FakeDevice() as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=QUIET)
            try:
                await model.select_rig_index(123)
                await wait_for(lambda: len(device.received) >= 2)
            finally:
                await model.close()
            return device.received

    received = run(scenario())
    assert received[:2] == [
        bytes([0xB0, 47, 24]),
        bytes([0xB0, 53, 1, 0xB0, 53, 0]),
    ]


def test_unknown_effect_slot_is_rejected():
    async def scenario():
        async with FakeDevice() as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=QUIET)
            try:
                with pytest.raises(UnknownSlotError):
                    await model.set_effect_enabled("nope", True)
            finally:
                await model.close()

    run(scenario())


def test_commands_after_close_are_rejected():
    async def scenario():
        async with FakeDevice() as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=QUIET)
            await model.close()
            with pytest.raises(DisconnectedError):
                await model.tap_tempo()

    run(scenario())


# ---------------------------------------------------------------------------
# Stream loss, reconnect, teardown
# ---------------------------------------------------------------------------


def test_device_hangup_marks_the_model_disconnected():
    async def scenario():
        async with FakeDevice(close_after_handshake=True) as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=QUIET)
            snapshots = model.subscribe()
            try:
                await wait_for(lambda: not model.connected)
                while True:
                    snapshot = await asyncio.wait_for(snapshots.get(), 2.0)
                    if snapshot.connection is Connection.DISCONNECTED:
                        return model.connected, snapshot
            finally:
                await model.close()

    connected, snapshot = run(scenario())
    assert not connected
    assert snapshot.connection is Connection.DISCONNECTED
    assert snapshot.channels.stream is ChannelState.LOST
    assert snapshot.channels.control is ChannelState.CLOSED


def test_stream_loss_with_a_backoff_reconnects_on_the_same_handle():
    options = ConnectOptions(
        sync=SyncStrategy.OFF,
        control=ControlPolicy.OFF,
        reconnect=ReconnectPolicy(stream=Backoff(initial=0.05)),
    )

    async def scenario():
        loop = asyncio.get_running_loop()
        async with FakeDevice() as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=options)
            events = model.events()
            snapshots = model.subscribe()
            try:
                await device.hangup(PROTOCOL_MIDI3_STREAM)
                lost_at = loop.time()
                reconnecting = await next_event(events, ConnectionChanged)
                attempt = model.state().reconnect_attempt
                connected = await next_event(events, Connected)
                reconnected_at = loop.time()
                # Same receivers, same tree: a push on the second life lands.
                await device.push(TEMPO)
                await wait_for(lambda: model.state().rig.tempo_bpm == 120)
                seen = await drain(snapshots)
                return (
                    reconnecting,
                    attempt,
                    connected,
                    reconnected_at - lost_at,
                    model.state(),
                    seen,
                    device.connection_count(PROTOCOL_MIDI3_STREAM),
                )
            finally:
                await model.close()

    reconnecting, attempt, connected, spacing, state, snapshots, lives = run(scenario())
    assert reconnecting == ConnectionChanged(Connection.RECONNECTING)
    assert attempt == 1
    assert connected == Connected()
    # The backoff is 50 ms; the ledger spaces the redial a cooldown from the loss.
    assert spacing >= CONNECTION_COOLDOWN - 0.05
    assert state.connection is Connection.CONNECTED
    assert state.reconnect_attempt == 0
    assert state.rig.tempo_bpm == 120
    assert lives == 2
    assert any(
        s.connection is Connection.RECONNECTING and s.reconnect_attempt == 1 for s in snapshots
    )
    assert not any(s.connection is Connection.DISCONNECTED for s in snapshots)


def test_close_is_idempotent_and_usable_as_a_context_manager():
    async def scenario():
        async with FakeDevice() as device:
            async with await DeviceModel.connect("127.0.0.1", device.port, options=QUIET) as model:
                assert model.connected
            await model.close()
            return model.connected, model.state()

    connected, state = run(scenario())
    assert connected is False
    assert state.connection is Connection.DISCONNECTED
    assert state.channels.stream is ChannelState.CLOSED


def test_close_cancels_a_control_link_still_waiting_its_turn():
    """With the default policy the control open waits in the ledger; a prompt
    close must not have to wait for it, nor leave a socket to open later."""

    async def scenario():
        loop = asyncio.get_running_loop()
        async with FakeDevice(offer_cbor=True) as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=NO_SYNC)
            started = loop.time()
            await model.close()
            elapsed = loop.time() - started
            await asyncio.sleep(CONNECTION_COOLDOWN + 0.2)
            return elapsed, device.connection_count(PROTOCOL_CBOR_CONTROL)

    elapsed, control_connections = run(scenario())
    assert elapsed < CONNECTION_COOLDOWN / 4
    assert control_connections == 0
