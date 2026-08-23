"""The async :class:`libkp.model.DeviceModel` store, driven against a stand-in device."""

from __future__ import annotations

import asyncio

import pytest
from fake_device import FakeDevice, wait_for

from libkp import _generated as gen
from libkp.errors import DisconnectedError, UnknownSlotError
from libkp.model import DeviceModel
from libkp.nrpn import PAGE_STRINGS, set_single, sysex, u14_split
from libkp.state import Connection, CurrentPosition, EffectChanged, Status, TempoBpm

RIG_NAME = sysex(0x00, 0x00, 0x03, PAGE_STRINGS, 1, b"Test Rig\x00")
REV_TYPE = set_single(0x00, 0x00, 0x3D, 0, 179)
TEMPO = set_single(0x00, 0x00, gen.PAGE_RIG_SETTINGS, gen.TEMPO_NUMBER, 7680)


def meter_message(values: list[int]) -> bytes:
    payload = bytearray()
    for v in values:
        payload.extend(u14_split(v))
    return sysex(0x00, 0x00, 0x02, gen.PAGE_REALTIME, gen.METER_BLOCK_NUMBER, bytes(payload))


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Connect + initial sync
# ---------------------------------------------------------------------------


def test_connect_performs_the_read_only_initial_sync():
    async def scenario():
        async with FakeDevice() as device:
            model = await DeviceModel.connect("127.0.0.1", device.port)
            try:
                await wait_for(lambda: len(device.received) >= 39)
            finally:
                await model.close()
            return device.received

    received = run(scenario())
    # Six string-tag requests, a Type + On/Off request per effect slot, the 15
    # bank-preview extended-string requests (5 slots x rig/amp/cabinet), then the
    # two extended-param requests for the current bank and rig slot.
    assert len(received) == 6 + 16 + 15 + 2
    assert all(m[6] in (0x43, 0x41, 0x46, 0x47) for m in received), "sync must be read-only"
    assert received[0] == bytes.fromhex("f0002033007f43000001f7")  # Rig Name
    assert received[6][6] == 0x41
    assert received[22][6] == 0x47  # first bank-preview ext-string request
    assert received[37][6] == 0x46  # current bank
    assert received[38][6] == 0x46  # current rig slot


def test_connect_can_skip_the_initial_sync():
    async def scenario():
        async with FakeDevice() as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, sync=False)
            await asyncio.sleep(0.05)
            await model.close()
            return device.received

    assert run(scenario()) == []


def test_connect_emits_connected_and_a_first_snapshot():
    async def scenario():
        async with FakeDevice() as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, sync=False)
            events = model.events()
            snapshots = model.subscribe()
            try:
                # Connected was emitted before we subscribed, so drive one more.
                assert model.connected
                assert model.state().connection is Connection.CONNECTED
                await device.push(TEMPO)
                snapshot = await asyncio.wait_for(snapshots.get(), 2.0)
                event = await asyncio.wait_for(events.get(), 2.0)
                return snapshot, event
            finally:
                await model.close()

    snapshot, event = run(scenario())
    assert snapshot.rig.tempo_bpm == 120
    assert event == TempoBpm(120)


def test_handshake_tail_is_decoded_before_the_first_read():
    async def scenario():
        async with FakeDevice(tail_messages=[RIG_NAME]) as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, sync=False)
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
            model = await DeviceModel.connect("127.0.0.1", device.port, sync=False)
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
            model = await DeviceModel.connect("127.0.0.1", device.port, sync=False)
            snapshots = model.subscribe()
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
            model = await DeviceModel.connect("127.0.0.1", device.port, sync=False)
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
            model = await DeviceModel.connect("127.0.0.1", device.port, sync=False)
            model.add_event_listener(lambda _event: (_ for _ in ()).throw(RuntimeError("boom")))
            try:
                await device.push(REV_TYPE)
                await wait_for(lambda: model.state().effects[7].kind == 179)
                return model.state().effects[7].kind
            finally:
                await model.close()

    assert run(scenario()) == 179


def test_unsubscribe_stops_delivery():
    async def scenario():
        async with FakeDevice() as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, sync=False)
            snapshots = model.subscribe()
            model.unsubscribe(snapshots)
            try:
                await device.push(TEMPO)
                await wait_for(lambda: model.state().rig.tempo_bpm == 120)
                return snapshots.qsize()
            finally:
                await model.close()

    assert run(scenario()) == 0


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def test_parameter_setters_emit_single_parameter_changes():
    async def scenario():
        async with FakeDevice() as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, sync=False)
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
            model = await DeviceModel.connect("127.0.0.1", device.port, sync=False)
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
            model = await DeviceModel.connect("127.0.0.1", device.port, sync=False)
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
            model = await DeviceModel.connect("127.0.0.1", device.port, sync=False)
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
            model = await DeviceModel.connect("127.0.0.1", device.port, sync=False)
            try:
                with pytest.raises(UnknownSlotError):
                    await model.set_effect_enabled("nope", True)
            finally:
                await model.close()

    run(scenario())


def test_commands_after_close_are_rejected():
    async def scenario():
        async with FakeDevice() as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, sync=False)
            await model.close()
            with pytest.raises(DisconnectedError):
                await model.tap_tempo()

    run(scenario())


# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------


def test_device_hangup_marks_the_model_disconnected():
    async def scenario():
        async with FakeDevice(close_after_handshake=True) as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, sync=False)
            snapshots = model.subscribe()
            try:
                await wait_for(lambda: not model.connected)
                snapshot = await asyncio.wait_for(snapshots.get(), 2.0)
                return model.connected, snapshot.connection
            finally:
                await model.close()

    connected, connection = run(scenario())
    assert not connected
    assert connection is Connection.DISCONNECTED


def test_close_is_idempotent_and_usable_as_a_context_manager():
    async def scenario():
        async with FakeDevice() as device:
            async with await DeviceModel.connect("127.0.0.1", device.port, sync=False) as model:
                assert model.connected
            await model.close()
            return model.connected

    assert run(scenario()) is False


def test_set_current_position_folds_into_state_and_emits():
    async def scenario():
        async with FakeDevice() as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, sync=False)
            events = model.events()
            snapshots = model.subscribe()
            try:
                model.set_current_position(1, 2)
                snapshot = await asyncio.wait_for(snapshots.get(), 2.0)
                event = await asyncio.wait_for(events.get(), 2.0)
                return model.state(), snapshot, event
            finally:
                await model.close()

    state, snapshot, event = run(scenario())
    assert (state.current_bank, state.current_rig_slot) == (1, 2)
    assert (snapshot.current_bank, snapshot.current_rig_slot) == (1, 2)
    assert event == CurrentPosition(bank=1, slot=2)


def test_set_current_position_only_overwrites_known_indices():
    async def scenario():
        async with FakeDevice() as device:
            model = await DeviceModel.connect("127.0.0.1", device.port, sync=False)
            try:
                model.set_current_position(3, 4)
                model.set_current_position(None, 0)  # leave the bank untouched
                return model.state()
            finally:
                await model.close()

    state = run(scenario())
    assert (state.current_bank, state.current_rig_slot) == (3, 0)
