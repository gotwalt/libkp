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
from libkp._link import COMMAND_QUEUE_DEPTH, StreamLink
from libkp.errors import (
    ChannelOffError,
    ChannelTooSoonError,
    DisconnectedError,
    ProtocolRejectedError,
    RequestDisconnectedError,
    RequestTimeoutError,
    RequestUnreadableError,
    RigLoadRequiresNavigatorError,
    SessionError,
    UnknownSlotError,
)
from libkp.midi3 import frame
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
    NavDrop,
    NavigationDropped,
    NavigationSettled,
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

#: The runs that close a dump's two sections; the second ends the dump.
DUMP_END_RUN = cbor.Tag(1, [2, gen.DUMP_END_ADDRESS, 0, 0])


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


def test_a_stream_read_chunk_republishes_the_snapshot_once():
    """Three slow changes in one write: one read, one snapshot carrying all
    three."""
    gain = set_single(0x00, 0x00, gen.AMP_PAGE, gen.GAIN_NUMBER, 1)

    async def scenario():
        async with FakeDevice() as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=QUIET)
            snapshots = model.subscribe()
            await drain(snapshots)
            events = model.events()
            try:
                await device.push_raw(frame(gain) + frame(TEMPO) + frame(REV_TYPE))
                await next_event(events, EffectChanged)
                return await drain(snapshots)
            finally:
                await model.close()

    published = run(scenario())
    assert len(published) == 1
    assert published[0].amp.gain == 1
    assert published[0].rig.tempo_bpm == 120
    assert published[0].effects[7].kind == 179


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
                    await model.request_param(gen.PAGE_MORPH, gen.MORPH_NUMBER)
                await asyncio.sleep(0.05)
                return device.received
            finally:
                await model.close()

    assert run(scenario()) == []


def test_the_morph_is_unreadable_before_the_stream_is_consulted():
    """Unreadable is a property of the address, so it is the answer whether
    or not the stream is up: the same request while disconnected says so,
    not ``RequestDisconnectedError``."""

    async def scenario():
        async with FakeDevice() as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=QUIET)
            await model.close()
            with pytest.raises(RequestUnreadableError):
                await model.request_param(gen.PAGE_MORPH, gen.MORPH_NUMBER)
            with pytest.raises(RequestDisconnectedError):
                await model.request_param(gen.AMP_PAGE, gen.GAIN_NUMBER)

    run(scenario())


def test_a_reply_wider_than_fourteen_bits_is_unreadable():
    """A ``$01`` reply is 14 bits; only a value from the control wire resolving
    the same address could be wider, and it is not the stream's answer."""
    gain = gen.AMP_PAGE * 128 + gen.GAIN_NUMBER

    async def scenario():
        async with FakeDevice(offer_cbor=True) as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=NO_SYNC)
            events = model.events()
            try:
                await next_event(events, SyncCompleted)
                request = asyncio.ensure_future(model.request_param(gen.AMP_PAGE, gen.GAIN_NUMBER))
                await wait_for(lambda: len(device.received) == 1)
                await device.push_items([cbor.param_write(gain, gen.FULL_SCALE + 1)])
                with pytest.raises(RequestUnreadableError) as excinfo:
                    await request
                return excinfo.value.address
            finally:
                await model.close()

    assert run(scenario()) == gain


def test_an_unsolicited_push_at_the_address_resolves_a_request():
    """The fake answers nothing; a push at the requested address is equally
    current, and resolves the request."""

    async def scenario():
        async with FakeDevice() as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=QUIET)
            try:
                request = asyncio.ensure_future(model.request_param(gen.AMP_PAGE, gen.GAIN_NUMBER))
                await wait_for(lambda: len(device.received) == 1)
                await device.push(set_single(0x00, 0x00, gen.AMP_PAGE, gen.GAIN_NUMBER, 42))
                return await request
            finally:
                await model.close()

    assert run(scenario()) == 42


def test_a_sensitive_reply_is_redacted_on_the_request_path():
    """The dump volunteers the WiFi credentials in the clear; a request's
    reply hands out the placeholder, exactly as the fold and the snapshot
    tooling do."""
    secret = gen.SENSITIVE_ADDRESSES[1]

    async def scenario():
        async with FakeDevice(offer_cbor=True) as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=NO_SYNC)
            events = model.events()
            try:
                await next_event(events, SyncCompleted)
                request = asyncio.ensure_future(model.request_ext_string(secret))
                await wait_for(lambda: len(device.received) == 1)
                await device.push_items([cbor.Tag(1, [4, secret, "hunter2"])])
                return await request
            finally:
                await model.close()

    assert run(scenario()) == gen.REDACTED_PLACEHOLDER


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
    assert state.output.main_volume == 8192, "the system section folded too"
    # One snapshot for the channel opening, exactly one for the whole dump.
    with_control = [s for s in snapshots if s.channels.control is ChannelState.OPEN]
    assert [s.morph for s in with_control] == [None, 8192]
    # One control socket, on which the trigger was the only thing written.
    assert device.connection_count(PROTOCOL_CBOR_CONTROL) == 1
    assert device.control.received_items == [cbor.state_dump_request()]


def test_the_dump_ends_at_the_second_end_run():
    """A dump has two sections, each closed by a run based at
    ``DUMP_END_ADDRESS``: the first alone ends nothing -- the rig section
    that follows still folds as the dump, under a live push's authority --
    and the second ends the phase at once, well before the settle time."""
    tempo = gen.PAGE_RIG_SETTINGS * 128 + gen.TEMPO_NUMBER
    system_section = [
        cbor.param_write(gen.SYSTEM_PAGE * 128 + gen.MAIN_VOLUME_NUMBER, 1),
        DUMP_END_RUN,
    ]
    rig_section = [
        cbor.Tag(1, [2, gen.CURRENT_BANK_ADDRESS - 1, 0, 3, 1]),
        cbor.param_write(tempo, 100 * gen.TEMPO_BPM_SCALE),
        DUMP_END_RUN,
    ]

    async def scenario():
        loop = asyncio.get_running_loop()
        async with FakeDevice(offer_cbor=True, dump_items=[]) as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=NO_SYNC)
            events = model.events()
            try:
                await wait_for(lambda: model.state().channels.control is ChannelState.OPEN, 3.0)
                opened = loop.time()
                await device.push_items(system_section)
                await wait_for(lambda: model.state().output.main_volume == 1)
                await asyncio.sleep(0.1)
                after_first = [e for e in await drain(events) if isinstance(e, SyncCompleted)]
                # Still the dump: a live tempo outranks the rig section's copy.
                await device.push(TEMPO)
                await wait_for(lambda: model.state().rig.tempo_bpm == 120)
                await device.push_items(rig_section)
                completed = await next_event(events, SyncCompleted)
                return after_first, completed, loop.time() - opened, model.state()
            finally:
                await model.close()

    after_first, completed, elapsed, state = run(scenario())
    assert after_first == [], "one end run alone must not end the dump"
    assert completed == SyncCompleted(Channel.CONTROL)
    assert elapsed < gen.DUMP_SETTLE_MS / 1000.0 - 0.2, "the second run ended it, not the settle"
    assert state.rig.tempo_bpm == 120
    assert (state.current_bank, state.current_rig_slot) == (3, 1)


def test_the_control_handshake_tail_folds_as_the_dump():
    """Items riding on the acceptance line are the dump's first items: they
    are held until the trigger is written and the dump begun, so the end runs
    among them count -- the phase ends on the tail alone."""
    tail = [cbor.Tag(1, [2, gen.CURRENT_BANK_ADDRESS - 1, 0, 3, 1]), DUMP_END_RUN, DUMP_END_RUN]

    async def scenario():
        loop = asyncio.get_running_loop()
        async with FakeDevice(offer_cbor=True, tail_items=tail, dump_items=[]) as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=NO_SYNC)
            events = model.events()
            try:
                await next_event(events, ChannelChanged)  # connecting
                opened = await next_event(events, ChannelChanged)
                opened_at = loop.time()
                completed = await next_event(events, SyncCompleted)
                return opened, completed, loop.time() - opened_at, model.state(), device
            finally:
                await model.close()

    opened, completed, elapsed, state, device = run(scenario())
    assert opened == ChannelChanged(Channel.CONTROL, ChannelState.OPEN)
    assert completed == SyncCompleted(Channel.CONTROL)
    assert elapsed < gen.DUMP_SETTLE_MS / 1000.0 - 0.2, "the tail's end runs ended it"
    assert (state.current_bank, state.current_rig_slot) == (3, 1)
    assert device.control.received_items == [cbor.state_dump_request()]


def test_a_live_push_during_the_dump_outranks_the_dump():
    """The dump is a copy taken when it was asked for; a value pushed on the
    stream while it streams is newer, so the dump's copy of that address must
    not undo it. (On the control channel itself everything inside the dump
    window is the dump -- the two cannot be told apart on that wire.)"""
    dump = [
        DUMP_END_RUN,
        cbor.param_write(gen.PAGE_RIG_SETTINGS * 128 + gen.TEMPO_NUMBER, 100 * gen.TEMPO_BPM_SCALE),
        DUMP_END_RUN,
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
    """A greeting without the control protocol is a rejection before any
    selection is written: the fake's second connection saw no selection line,
    and no second stream was opened in its place."""

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
                await wait_for(lambda: len(device.connections) == 2)
                assert device.connections[1].selected is None
                assert device.connection_count(PROTOCOL_MIDI3_STREAM) == 1
                return changed, state, model.connected
            finally:
                await model.close()

    changed, state, connected = run(scenario())
    assert changed == ConnectionChanged(Connection.DEGRADED)
    assert state.connection is Connection.DEGRADED
    assert state.channels.control is ChannelState.UNAVAILABLE
    assert state.channels.stream is ChannelState.OPEN
    assert connected


def test_a_control_protocol_not_offered_is_refused_without_a_selection():
    """A greeting that offers only the stream would take a selection of it;
    the control link must not make one -- a control link on some other
    protocol is not a control link -- so it refuses with the reason, writes
    nothing, and the model has exactly one stream."""

    async def scenario():
        async with FakeDevice(offered=[PROTOCOL_MIDI3_STREAM]) as device:
            with pytest.raises(ProtocolRejectedError) as excinfo:
                await cbor.ControlLink.open("127.0.0.1", device.port, lambda _: None, lambda: None)
            model = await DeviceModel.connect("127.0.0.1", device.port, options=NO_SYNC)
            try:
                await wait_for(lambda: model.state().connection is Connection.DEGRADED, 3.0)
                return excinfo.value, model.state(), device
            finally:
                await model.close()

    error, state, device = run(scenario())
    assert error.name == PROTOCOL_CBOR_CONTROL
    assert error.detail == "not offered in the greeting"
    assert state.channels.control is ChannelState.UNAVAILABLE
    assert device.connection_count(PROTOCOL_MIDI3_STREAM) == 1
    assert [c.selected for c in device.connections] == [None, PROTOCOL_MIDI3_STREAM, None]


def test_a_control_protocol_offered_but_refused_is_unavailable():
    """The ``-NO`` variant: the protocol is offered, the selection is written,
    and the device turns it down."""

    async def scenario():
        async with FakeDevice(offer_cbor=True, accepts=[PROTOCOL_MIDI3_STREAM]) as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=NO_SYNC)
            events = model.events()
            try:
                changed = await next_event(events, ConnectionChanged)
                return changed, model.state(), [c.selected for c in device.connections]
            finally:
                await model.close()

    changed, state, selected = run(scenario())
    assert changed == ConnectionChanged(Connection.DEGRADED)
    assert state.channels == state.channels.__class__(ChannelState.OPEN, ChannelState.UNAVAILABLE)
    assert selected == [PROTOCOL_MIDI3_STREAM, PROTOCOL_CBOR_CONTROL]


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
    # The second socket saw the greeting and hung up without selecting.
    assert selected == [PROTOCOL_MIDI3_STREAM, None]


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


def test_reopen_control_leaves_an_open_link_alone():
    """Inside the gap and with the link open, a reopen is neither refused nor
    acted on: the link asked for is there, so the call returns at once and
    the socket is never dropped to dial another."""

    async def scenario():
        async with FakeDevice(offer_cbor=True) as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=NO_SYNC)
            try:
                await wait_for(lambda: model.state().morph == 8192, 3.0)
                seen = []
                model.add_event_listener(seen.append)
                assert await model.reopen_control() is None
                await asyncio.sleep(0.1)
                return list(seen), model.state(), device.connection_count(PROTOCOL_CBOR_CONTROL)
            finally:
                await model.close()

    seen, state, control_connections = run(scenario())
    assert not any(isinstance(e, ChannelChanged) for e in seen)
    assert state.channels.control is ChannelState.OPEN
    assert control_connections == 1


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
                await model.bank(4)
                await model.tuner_mode(True)
                await model.freeze(False)
                await model.send_raw(bytes([0xB0, 30, 1]))
                await wait_for(lambda: len(device.received) >= 5)
            finally:
                await model.close()
            return device.received

    received = run(scenario())
    assert received[:5] == [
        bytes([0xB0, 30, 1]),
        # The bank preselect loads nothing, so it is not the Navigator's.
        bytes([0xB0, 47, 3]),
        bytes([0xB0, 31, 1]),
        bytes([0xB0, 35, 0]),
        bytes([0xB0, 30, 1]),
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
# The Navigator
# ---------------------------------------------------------------------------

#: The device landing on bank 2, slot 4: flat index 14.
POSITION_14 = [ext_param(gen.CURRENT_BANK_ADDRESS, 2), ext_param(gen.CURRENT_RIG_SLOT_ADDRESS, 4)]


def rig_load(index: int) -> list[bytes]:
    """The pair the Navigator puts on the wire for a flat index: the absolute
    bank preselect (CC47, its value a 7-bit data byte), then the slot load's
    press and release."""
    bank, slot = divmod(index, gen.BANK_SLOTS)
    return [
        bytes([0xB0, 47, bank & 0x7F]),
        bytes([0xB0, 50 + slot, 1, 0xB0, 50 + slot, 0]),
    ]


def test_navigate_to_sends_absolute_bank_then_slot():
    """Index 123 is bank 25 slot 4: CC47 value 24, then CC53 press/release."""

    async def scenario():
        async with FakeDevice() as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=QUIET)
            try:
                model.navigate_to(123)
                await wait_for(lambda: len(device.received) >= 2)
                return device.received, model.state()
            finally:
                await model.close()

    received, state = run(scenario())
    assert received[:2] == rig_load(123)
    assert state.navigation.aim == 123
    assert state.navigation.in_flight is True
    assert state.aimed_rig_index == 123


def test_a_burst_of_aims_costs_exactly_two_loads_a_settle_apart():
    """Three taps before the first move settles: the first goes out at once,
    the last when the settle elapses, and the one in between never."""

    async def scenario():
        loop = asyncio.get_running_loop()
        async with FakeDevice() as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=QUIET)
            snapshots = model.subscribe()
            await drain(snapshots)
            try:
                model.navigate_to(14)
                model.navigate_to(15)
                model.navigate_to(16)
                await wait_for(lambda: len(device.received) >= 2)
                first_at = loop.time()
                aimed = model.state()
                await wait_for(lambda: len(device.received) >= 4)
                second_at = loop.time()
                # Nothing more follows: the aim in between was never sent.
                await asyncio.sleep(0.2)
                return device.received, second_at - first_at, aimed, await drain(snapshots)
            finally:
                await model.close()

    received, spacing, aimed, snapshots = run(scenario())
    assert received == rig_load(14) + rig_load(16)
    assert spacing >= gen.RIG_LOAD_SETTLE_MS / 1000.0 - 0.02
    # The aim moved freely while the first move was in flight.
    assert aimed.navigation.aim == 16
    assert aimed.navigation.in_flight is True
    assert aimed.aimed_rig_index == 16
    # The navigation is part of the snapshot: aiming and settling are slow changes.
    assert [(s.navigation.aim, s.navigation.in_flight) for s in snapshots][:1] == [(14, True)]
    assert any(s.navigation.aim == 16 and s.navigation.in_flight for s in snapshots)


def test_a_matching_position_settles_the_aim():
    async def scenario():
        async with FakeDevice() as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=QUIET)
            events = model.events()
            try:
                model.navigate_to(14)
                await wait_for(lambda: len(device.received) >= 2)
                for message in POSITION_14:
                    await device.push(message)
                settled = await next_event(events, NavigationSettled)
                return settled, model.state()
            finally:
                await model.close()

    settled, state = run(scenario())
    assert settled == NavigationSettled(14)
    assert state.navigation.aim is None
    assert state.current_rig_index == 14
    assert state.aimed_rig_index == 14


def test_a_position_from_the_control_link_settles_the_aim():
    async def scenario():
        async with FakeDevice(offer_cbor=True) as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=NO_SYNC)
            events = model.events()
            try:
                # Let the dump land first: it reports bank 3 / slot 1, which is
                # nobody's aim yet.
                await next_event(events, SyncCompleted)
                assert model.state().current_rig_index == 16
                model.navigate_to(14)
                await wait_for(lambda: len(device.received) >= 2)
                await device.push_items(
                    [
                        cbor.param_write(gen.CURRENT_BANK_ADDRESS, 2),
                        cbor.param_write(gen.CURRENT_RIG_SLOT_ADDRESS, 4),
                    ]
                )
                settled = await next_event(events, NavigationSettled)
                return settled, model.state()
            finally:
                await model.close()

    settled, state = run(scenario())
    assert settled == NavigationSettled(14)
    assert state.navigation.aim is None
    assert state.current_rig_index == 14


def test_a_mismatched_position_keeps_the_aim():
    async def scenario():
        async with FakeDevice() as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=QUIET)
            seen = []
            model.add_event_listener(seen.append)
            try:
                model.navigate_to(639)
                await wait_for(lambda: len(device.received) >= 2)
                for message in POSITION_14:
                    await device.push(message)
                await wait_for(lambda: model.state().current_rig_index == 14)
                return model.state(), seen
            finally:
                await model.close()

    state, seen = run(scenario())
    assert state.navigation.aim == 639
    assert state.aimed_rig_index == 639
    assert not any(isinstance(e, NavigationSettled | NavigationDropped) for e in seen)


def test_an_aim_the_device_never_confirms_is_dropped_after_the_window():
    """Past the last rig the device stays put; the aim outlives the settle by
    the pending window, then goes, and the same index is sendable again. 639
    is the last index the wire can name (bank 127, slot 5)."""

    async def scenario():
        loop = asyncio.get_running_loop()
        async with FakeDevice() as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=QUIET)
            events = model.events()
            try:
                model.navigate_to(639)
                started = loop.time()
                await wait_for(lambda: len(device.received) >= 2)
                dropped = await next_event(events, NavigationDropped)
                elapsed = loop.time() - started
                state = model.state()
                model.navigate_to(639)
                await wait_for(lambda: len(device.received) >= 4)
                return dropped, elapsed, state, device.received
            finally:
                await model.close()

    dropped, elapsed, state, received = run(scenario())
    assert dropped == NavigationDropped(639, NavDrop.UNCONFIRMED)
    assert elapsed >= (gen.RIG_LOAD_SETTLE_MS + gen.PENDING_WINDOW_MS) / 1000.0 - 0.02
    assert state.navigation.aim is None
    assert state.navigation.in_flight is False
    assert received == rig_load(639) + rig_load(639)


def test_an_index_the_wire_cannot_name_is_dropped_at_once():
    """The bank preselect is a 7-bit CC value: an aim whose bank does not fit
    (index >= 128 * BANK_SLOTS) is dropped immediately with nothing on the
    wire -- masking the bank would silently load a real but wrong rig."""

    async def scenario():
        async with FakeDevice() as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=QUIET)
            events = model.events()
            try:
                model.navigate_to(128 * gen.BANK_SLOTS)
                dropped = await next_event(events, NavigationDropped)
                await asyncio.sleep(0.05)
                return dropped, model.state(), device.received
            finally:
                await model.close()

    dropped, state, received = run(scenario())
    assert dropped == NavigationDropped(128 * gen.BANK_SLOTS, NavDrop.UNCONFIRMED)
    assert state.navigation.aim is None
    assert received == []


def test_an_aim_sent_once_is_not_sent_again_while_it_stands():
    async def scenario():
        async with FakeDevice() as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=QUIET)
            try:
                model.navigate_to(14)
                model.navigate_to(14)
                await wait_for(lambda: len(device.received) >= 2)
                await asyncio.sleep(gen.RIG_LOAD_SETTLE_MS / 1000.0 + 0.1)
                model.navigate_to(14)
                await asyncio.sleep(0.1)
                return device.received
            finally:
                await model.close()

    assert run(scenario()) == rig_load(14)


def test_steps_and_slots_aim_from_the_aimed_position():
    async def scenario():
        async with FakeDevice() as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=QUIET)
            try:
                # Nothing is known yet, so there is nothing to step from.
                model.step_rig(1)
                model.step_bank(True)
                model.select_slot(2)
                await asyncio.sleep(0.05)
                assert device.received == []
                for message in POSITION_14:
                    await device.push(message)
                await wait_for(lambda: model.state().current_rig_index == 14)
                model.step_rig(0)  # lands where the aim is: nothing to send
                await asyncio.sleep(0.05)
                assert device.received == []
                model.step_rig(1)  # 15: sent at once
                model.step_rig(1)  # 16: in flight, so only the aim moves
                model.step_bank(True)  # 21
                model.select_slot(2)  # slot 2 of bank 4: 16
                model.select_slot(6)  # out of range: ignored
                model.step_bank(False)  # 11
                model.step_rig(-100)  # floored at 0
                await wait_for(lambda: len(device.received) >= 2)
                aimed = model.state().aimed_rig_index
                await wait_for(lambda: len(device.received) >= 4)
                return device.received, aimed
            finally:
                await model.close()

    received, aimed = run(scenario())
    assert aimed == 0
    assert received == rig_load(15) + rig_load(0)


def test_rig_loads_are_refused_outside_the_navigator():
    """Every door but the Navigator refuses a load before a byte is written."""
    from libkp import control

    async def scenario():
        async with FakeDevice() as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=QUIET)
            try:
                refused = [
                    control.LoadSlot(3),
                    control.Up(),
                    control.Down(),
                    control.ProgramChange(5),
                    control.BankSelect(0, 1),
                ]
                for item in refused:
                    with pytest.raises(RigLoadRequiresNavigatorError):
                        await model.send_control(item)
                for raw in (
                    bytes([0xC0, 5]),
                    bytes([0xC3, 0]),
                    bytes([0xB0, 50, 1, 0xB0, 50, 0]),
                    bytes([0xB0, 48, 1]),
                    bytes([0xB0, 30, 1, 0xB0, 54, 1]),
                ):
                    with pytest.raises(RigLoadRequiresNavigatorError):
                        await model.send_raw(raw)
                await asyncio.sleep(0.05)
                return device.received
            finally:
                await model.close()

    assert run(scenario()) == []


def test_the_raw_scan_examines_every_status_byte():
    """The refusal is a whole-buffer scan, not a parse: a load hidden inside
    a malformed SysEx is refused the same way everywhere, and a data byte or a
    ``0xF7`` is never mistaken for one."""
    from libkp.model import _refuse_rig_loads

    for raw in (
        bytes([0xF0, 0x00, 0xC0, 0x05, 0xF7]),
        bytes([0xF0, 0xB0, 50, 1, 0xF7]),
        bytes([0x00, 0xCF]),
    ):
        with pytest.raises(RigLoadRequiresNavigatorError):
            _refuse_rig_loads(raw)
    for raw in (bytes([0xB0, 47, 3]), bytes([0xF7]), bytes([0xB0]), bytes([50, 1])):
        _refuse_rig_loads(raw)


def test_an_aim_while_disconnected_is_dropped_at_once():
    async def scenario():
        async with FakeDevice() as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=QUIET)
            seen = []
            model.add_event_listener(seen.append)
            await model.close()
            model.navigate_to(3)
            return seen, model.state()

    seen, state = run(scenario())
    assert NavigationDropped(3, NavDrop.UNCONFIRMED) in seen
    assert state.navigation.aim is None


def test_close_cancels_the_navigator_and_clears_the_aim_without_an_event():
    async def scenario():
        async with FakeDevice() as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=QUIET)
            seen = []
            model.add_event_listener(seen.append)
            model.navigate_to(14)
            await wait_for(lambda: len(device.received) >= 2)
            await model.close()
            cleared = model.state()
            # Past the settle and the window: the cancelled timers stay quiet.
            await asyncio.sleep((gen.RIG_LOAD_SETTLE_MS + gen.PENDING_WINDOW_MS) / 1000.0 + 0.1)
            return cleared, seen

    cleared, seen = run(scenario())
    assert cleared.navigation.aim is None
    assert cleared.navigation.in_flight is False
    assert not any(isinstance(e, NavigationSettled | NavigationDropped) for e in seen)


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


def test_a_stream_loss_closes_the_control_link_too():
    """Both sockets drop together: the control link is ``CLOSED``, never
    ``LOST`` (it was not the one that went), and the whole loss is one
    snapshot, published once both sockets are closed and the connection has
    moved -- never one with the stream gone and the connection still up."""

    async def scenario():
        async with FakeDevice(offer_cbor=True) as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=NO_SYNC)
            try:
                await wait_for(lambda: model.state().channels.control is ChannelState.OPEN, 3.0)
                seen = []
                model.add_event_listener(seen.append)
                snapshots = model.subscribe()
                await drain(snapshots)
                await device.hangup(PROTOCOL_MIDI3_STREAM)
                await wait_for(lambda: not model.connected)
                await wait_for(lambda: device.control.closed.is_set())
                return seen, await drain(snapshots), model.state()
            finally:
                await model.close()

    seen, snapshots, state = run(scenario())
    assert ChannelChanged(Channel.CONTROL, ChannelState.CLOSED) in seen
    assert ChannelChanged(Channel.CONTROL, ChannelState.LOST) not in seen
    assert (state.channels.stream, state.channels.control) == (
        ChannelState.LOST,
        ChannelState.CLOSED,
    )
    assert state.connection is Connection.DISCONNECTED
    assert len(snapshots) == 1, "one snapshot for the whole loss"
    assert snapshots[0].connection is Connection.DISCONNECTED
    assert (snapshots[0].channels.stream, snapshots[0].channels.control) == (
        ChannelState.LOST,
        ChannelState.CLOSED,
    )


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
                # A value in the tree before the loss is kept across it.
                await device.push(RIG_NAME)
                await wait_for(lambda: model.state().rig.name == "Test Rig")
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
    assert state.rig.name == "Test Rig", "tree values are kept, not reset"
    assert lives == 2
    assert any(
        s.connection is Connection.RECONNECTING and s.reconnect_attempt == 1 for s in snapshots
    )
    assert not any(s.connection is Connection.DISCONNECTED for s in snapshots)


def test_a_refused_redial_counts_a_second_attempt_with_a_doubled_backoff():
    """The fake refuses the first redial: the model reports attempt 2 and
    waits twice the initial backoff before the next -- longer than the
    ledger's cooldown here, so the doubling is what sets the spacing."""
    initial = 0.6
    assert 2 * initial > CONNECTION_COOLDOWN
    options = ConnectOptions(
        sync=SyncStrategy.OFF,
        control=ControlPolicy.OFF,
        reconnect=ReconnectPolicy(stream=Backoff(initial=initial)),
    )

    async def scenario():
        loop = asyncio.get_running_loop()
        async with FakeDevice() as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=options)
            events = model.events()
            try:
                device.pause_accepting()
                await device.hangup(PROTOCOL_MIDI3_STREAM)
                first = await next_event(events, ConnectionChanged)
                attempt_1 = model.state().reconnect_attempt
                # The refused dial: a second attempt is counted.
                await wait_for(lambda: model.state().reconnect_attempt == 2, 3.0)
                second_at = loop.time()
                device.resume_accepting()
                seen = [first]
                while True:
                    event = await asyncio.wait_for(events.get(), 3.0)
                    seen.append(event)
                    if isinstance(event, Connected):
                        break
                return seen, attempt_1, loop.time() - second_at, device.refused, model.state()
            finally:
                await model.close()

    seen, attempt_1, second_wait, refused, state = run(scenario())
    assert seen[0] == ConnectionChanged(Connection.RECONNECTING)
    assert attempt_1 == 1
    assert refused == 1
    assert seen.count(ConnectionChanged(Connection.RECONNECTING)) == 2
    assert not any(isinstance(e, Disconnected) for e in seen)
    assert second_wait >= 2 * initial - 0.02
    assert state.connection is Connection.CONNECTED
    assert state.reconnect_attempt == 0


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


def test_one_socket_loss_spawns_one_recovery():
    """The ingest and writer tasks can both report the same loss in the same
    tick; the second report must find itself stale rather than spawn a second
    recovery for ``close()`` to lose track of."""

    async def scenario():
        async with FakeDevice() as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, options=QUIET)
            try:
                epoch = model._epoch
                model._on_stream_lost(epoch)
                supervisor = model._supervisor
                model._on_stream_lost(epoch)
                assert model._supervisor is supervisor, "one recovery task"
                await wait_for(lambda: model.state().connection is Connection.DISCONNECTED)
            finally:
                await model.close()

    run(scenario())


def test_the_rig_load_pair_is_queued_whole_or_not_at_all():
    """With one slot left, the pair is refused and nothing is queued: an
    orphaned bank preselect would leave the device armed for a load that
    never followed."""

    async def scenario():
        link = StreamLink(None, b"")
        for _ in range(COMMAND_QUEUE_DEPTH - 1):
            link.send_nowait(b"x")
        with pytest.raises(asyncio.QueueFull):
            link.send_pair_nowait(b"a", b"b")
        assert link._commands.qsize() == COMMAND_QUEUE_DEPTH - 1, "neither was queued"
        link.send_nowait(b"y")  # room for one
        with pytest.raises(asyncio.QueueFull):
            link.send_nowait(b"z")

    run(scenario())
