"""The diagnostics download: the whole tree, minus the address of the device."""

from __future__ import annotations

from conftest import entity_id, wait_for_state
from fake_device import FakeDevice
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from libkp import _generated as gen
from libkp.nrpn import PAGE_STRINGS, sysex
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kemper.diagnostics import async_get_config_entry_diagnostics


async def test_the_dump_is_json_and_redacted(
    hass: HomeAssistant, device: FakeDevice, entry: MockConfigEntry
) -> None:
    """Enums come out as their values and the host does not come out at all."""
    await device.push(
        sysex(0x00, 0x00, 0x03, PAGE_STRINGS, gen.STRING_RIG_NAME, b"Crunchy Vox\x00")
    )
    await wait_for_state(hass, entity_id(hass, "sensor", "rig_name"), "Crunchy Vox")

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["entry"]["data"][CONF_HOST] == "**REDACTED**"
    assert diagnostics["state"]["connection"] == "connected"
    assert diagnostics["state"]["rig"]["name"] == "Crunchy Vox"
    # The parts that never became entities are here, which is the point.
    assert len(diagnostics["state"]["effects"]) == 8
    assert len(diagnostics["state"]["status"]["raw"]) == gen.METER_COUNT
    assert diagnostics["activity"]["active"] is False
