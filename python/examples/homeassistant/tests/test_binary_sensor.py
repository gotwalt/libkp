"""The activity detector, at the rate the device really pushes meters.

Every assertion here is about *how many* state writes come out: the meter lane
runs at ~20 Hz, and the whole point of the detector is that Home Assistant
sees two states per playing session and not two per second.
"""

from __future__ import annotations

from datetime import timedelta

from conftest import entity_id, wait_for_state
from fake_device import FakeDevice
from homeassistant.const import EVENT_STATE_CHANGED, STATE_OFF, STATE_ON
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.util import dt as dt_util
from libkp import _generated as gen
from libkp.nrpn import sysex, u14_split
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.kemper.const import DEFAULT_ACTIVITY_WINDOW

#: Rig output level (v6) well above and well below the 2% default threshold.
LOUD = 9000
QUIET = 100


def meter_message(rig_out_level: int) -> bytes:
    """One realtime status frame carrying ``rig_out_level`` in v6."""
    values = [0] * gen.METER_COUNT
    values[6] = rig_out_level
    payload = bytearray()
    for value in values:
        payload.extend(u14_split(value))
    return sysex(0x00, 0x00, 0x02, gen.PAGE_REALTIME, gen.METER_BLOCK_NUMBER, bytes(payload))


def count_changes(hass: HomeAssistant, entity: str) -> list[Event]:
    """Collect every state change of one entity from now on."""
    seen: list[Event] = []

    @callback
    def record(event: Event) -> None:
        if event.data["entity_id"] == entity:
            seen.append(event)

    hass.bus.async_listen(EVENT_STATE_CHANGED, record)
    return seen


async def test_quiet_frames_do_not_count_as_playing(
    hass: HomeAssistant, device: FakeDevice, entry: MockConfigEntry
) -> None:
    """A stream with nothing plugged into it still pushes meters."""
    active = entity_id(hass, "binary_sensor", "active")
    changes = count_changes(hass, active)

    for _ in range(20):
        await device.push(meter_message(QUIET))
    await hass.async_block_till_done()

    assert hass.states.get(active).state == STATE_OFF
    assert changes == []
    assert hass.states.get(entity_id(hass, "sensor", "last_activity")).state == "unknown"


async def test_a_burst_of_frames_is_one_state_write(
    hass: HomeAssistant, device: FakeDevice, entry: MockConfigEntry
) -> None:
    """A hundred frames — five seconds of playing — write the state once."""
    active = entity_id(hass, "binary_sensor", "active")
    last_activity = entity_id(hass, "sensor", "last_activity")
    changes = count_changes(hass, active)
    activity_changes = count_changes(hass, last_activity)

    for _ in range(100):
        await device.push(meter_message(LOUD))
    await wait_for_state(hass, active, STATE_ON)
    await hass.async_block_till_done()

    assert len(changes) == 1
    assert len(activity_changes) == 1
    assert hass.states.get(last_activity).state != "unknown"


async def test_the_window_settles_the_sensor_off(
    hass: HomeAssistant, device: FakeDevice, entry: MockConfigEntry
) -> None:
    """Silence for the whole window turns it off — and that is the second write."""
    active = entity_id(hass, "binary_sensor", "active")
    await device.push(meter_message(LOUD))
    await wait_for_state(hass, active, STATE_ON)

    changes = count_changes(hass, active)
    window = timedelta(minutes=DEFAULT_ACTIVITY_WINDOW)
    async_fire_time_changed(hass, dt_util.utcnow() + window + timedelta(seconds=1))
    await hass.async_block_till_done()

    assert hass.states.get(active).state == STATE_OFF
    assert len(changes) == 1


async def test_playing_again_before_the_window_ends_keeps_it_on(
    hass: HomeAssistant, device: FakeDevice, entry: MockConfigEntry
) -> None:
    """A pause shorter than the window re-arms the timer instead of settling."""
    active = entity_id(hass, "binary_sensor", "active")
    await device.push(meter_message(LOUD))
    await wait_for_state(hass, active, STATE_ON)

    changes = count_changes(hass, active)
    # The timer fires early — the model has heard a note since it was armed.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
    await hass.async_block_till_done()

    assert hass.states.get(active).state == STATE_ON
    assert changes == []


async def test_a_shorter_window_applies_without_a_reconnect(
    hass: HomeAssistant, device: FakeDevice, entry: MockConfigEntry
) -> None:
    """Saving the options form retunes the running detector."""
    from custom_components.kemper.const import CONF_ACTIVITY_THRESHOLD, CONF_ACTIVITY_WINDOW

    active = entity_id(hass, "binary_sensor", "active")
    await device.push(meter_message(LOUD))
    await wait_for_state(hass, active, STATE_ON)

    hass.config_entries.async_update_entry(
        entry, options={CONF_ACTIVITY_WINDOW: 1, CONF_ACTIVITY_THRESHOLD: 2}
    )
    await hass.async_block_till_done()

    detector = entry.runtime_data.activity
    assert detector.window == 60.0
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()
    assert hass.states.get(active).state == STATE_OFF


async def test_a_louder_threshold_ignores_quiet_playing(
    hass: HomeAssistant, device: FakeDevice, entry: MockConfigEntry
) -> None:
    """The threshold is a percentage of full scale, and it is enforced."""
    from custom_components.kemper.const import CONF_ACTIVITY_THRESHOLD, CONF_ACTIVITY_WINDOW

    hass.config_entries.async_update_entry(
        entry, options={CONF_ACTIVITY_WINDOW: 5, CONF_ACTIVITY_THRESHOLD: 90}
    )
    await hass.async_block_till_done()

    active = entity_id(hass, "binary_sensor", "active")
    for _ in range(20):
        await device.push(meter_message(LOUD))
    await hass.async_block_till_done()

    assert hass.states.get(active).state == STATE_OFF
