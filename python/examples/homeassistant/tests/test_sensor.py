"""The name sensors, driven by what the device actually pushes.

The messages come from libkp's own message builders, so what these tests put
on the wire is byte-for-byte what a Profiler puts there when a rig is loaded.
"""

from __future__ import annotations

from conftest import entity_id, wait_for_state
from fake_device import FakeDevice
from homeassistant.core import HomeAssistant
from libkp import _generated as gen
from libkp.nrpn import PAGE_STRINGS, sysex
from pytest_homeassistant_custom_component.common import MockConfigEntry

#: The page-0 string tags a rig change pushes, by their spec names.
RIG_NAME = gen.STRING_RIG_NAME
AMP_NAME = gen.STRING_AMP_NAME
CABINET_NAME = gen.STRING_CABINET_NAME


def string_tag(number: int, text: str) -> bytes:
    """A ``$03`` String Parameter push, as a rig change sends."""
    return sysex(0x00, 0x00, 0x03, PAGE_STRINGS, number, text.encode("ascii") + b"\x00")


async def test_the_names_follow_the_device(
    hass: HomeAssistant, device: FakeDevice, entry: MockConfigEntry
) -> None:
    """Load a rig on the device and the three sensors say what it is."""
    await device.push(string_tag(RIG_NAME, "Crunchy Vox"))
    await device.push(string_tag(AMP_NAME, "Vintage Twin"))
    await device.push(string_tag(CABINET_NAME, "2x12 Alnico"))

    await wait_for_state(hass, entity_id(hass, "sensor", "rig_name"), "Crunchy Vox")
    await wait_for_state(hass, entity_id(hass, "sensor", "amp_name"), "Vintage Twin")
    await wait_for_state(hass, entity_id(hass, "sensor", "cabinet_name"), "2x12 Alnico")


async def test_a_second_rig_replaces_the_first(
    hass: HomeAssistant, device: FakeDevice, entry: MockConfigEntry
) -> None:
    """Nothing is cached across rigs: the last push wins."""
    await device.push(string_tag(RIG_NAME, "Crunchy Vox"))
    await wait_for_state(hass, entity_id(hass, "sensor", "rig_name"), "Crunchy Vox")

    await device.push(string_tag(RIG_NAME, "Clean Twin"))
    await wait_for_state(hass, entity_id(hass, "sensor", "rig_name"), "Clean Twin")


async def test_losing_the_stream_makes_the_sensors_unavailable(
    hass: HomeAssistant, device: FakeDevice, entry: MockConfigEntry
) -> None:
    """Availability follows the connection, not the last value seen."""
    await device.push(string_tag(RIG_NAME, "Crunchy Vox"))
    rig = entity_id(hass, "sensor", "rig_name")
    await wait_for_state(hass, rig, "Crunchy Vox")

    await device.hangup()

    await wait_for_state(hass, rig, "unavailable")
    await wait_for_state(hass, entity_id(hass, "sensor", "last_activity"), "unavailable")
