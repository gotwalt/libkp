"""The config flow: discovery, the manual fallback, and the options form."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fake_device import FakeDevice
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_DEVICE, CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kemper.config_flow import MANUAL
from custom_components.kemper.const import (
    CONF_ACTIVITY_THRESHOLD,
    CONF_ACTIVITY_WINDOW,
    CONF_SERIAL,
    CONF_SW_VERSION,
    DOMAIN,
)
from custom_components.kemper.discovery import Found

FOUND = Found(host="10.0.0.5", name="Studio Profiler", serial="SER123", version="10.5.2")


@pytest.fixture
def no_setup():
    """Stop a created entry from dialing a device the test does not have."""
    with patch(
        "custom_components.kemper.async_setup_entry", AsyncMock(return_value=True)
    ) as mocked:
        yield mocked


async def start(hass: HomeAssistant, found: list[Found]) -> dict:
    """Run the user step with a canned discovery result."""
    with patch("custom_components.kemper.config_flow.async_discover", return_value=found):
        return await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})


async def test_a_discovered_profiler_needs_no_session(
    hass: HomeAssistant, no_setup: AsyncMock
) -> None:
    """Discovery already carries the name, serial and version — take them."""
    result = await start(hass, [FOUND])
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "pick"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DEVICE: FOUND.host}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Studio Profiler"
    assert result["data"][CONF_HOST] == "10.0.0.5"
    assert result["result"].unique_id == "SER123"


async def test_a_profiler_is_only_added_once(hass: HomeAssistant, no_setup: AsyncMock) -> None:
    """The serial is the identity, so the same device cannot be added twice."""
    existing = MockConfigEntry(domain=DOMAIN, unique_id="SER123", data={CONF_HOST: "10.0.0.9"})
    existing.add_to_hass(hass)

    result = await start(hass, [FOUND])
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DEVICE: FOUND.host}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    # The address is refreshed on the way out: devices move between leases.
    assert existing.data[CONF_HOST] == "10.0.0.5"


async def test_no_replies_falls_through_to_the_form(hass: HomeAssistant) -> None:
    """A held discovery port or a quiet network is not an error."""
    result = await start(hass, [])
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "manual"


async def test_the_manual_form_is_reachable_from_the_list(hass: HomeAssistant) -> None:
    """A Profiler on another subnet never answers the broadcast."""
    result = await start(hass, [FOUND])
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DEVICE: MANUAL}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "manual"


async def test_a_manual_host_is_checked_with_one_session(
    hass: HomeAssistant, device: FakeDevice, no_setup: AsyncMock
) -> None:
    """The check is a real handshake against the fake device, opened once."""
    result = await start(hass, [])
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: device.port}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_PORT] == device.port
    assert result["result"].unique_id == "127.0.0.1"
    assert len(device.connections) == 1


async def test_a_host_that_does_not_answer_says_so(hass: HomeAssistant) -> None:
    """A refused connection is a form error, not a traceback."""
    result = await start(hass, [])
    with patch("custom_components.kemper.config_flow.async_check", side_effect=OSError("refused")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "10.0.0.7", CONF_PORT: 5727}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_an_unexpected_failure_says_unknown(hass: HomeAssistant) -> None:
    """Anything the stack can raise leaves the form usable."""
    result = await start(hass, [])
    with patch("custom_components.kemper.config_flow.async_check", side_effect=ValueError("odd")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "10.0.0.7", CONF_PORT: 5727}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}


async def test_the_options_form_retunes_the_detector(
    hass: HomeAssistant, device: FakeDevice, entry: MockConfigEntry
) -> None:
    """Saving options reaches the running detector, and only it."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_ACTIVITY_WINDOW: 10, CONF_ACTIVITY_THRESHOLD: 5}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_ACTIVITY_WINDOW] == 10
    detector = entry.runtime_data.activity
    assert detector.window == 600.0
    assert detector.threshold == 819


async def test_a_manual_host_adopts_its_serial(
    hass: HomeAssistant, device: FakeDevice, no_setup: AsyncMock
) -> None:
    """A hand-typed address is keyed by the serial the device answers with."""
    identified = Found(host="127.0.0.1", name="Studio Profiler", serial="SER123", version="10.5.3")
    result = await start(hass, [])
    with patch("custom_components.kemper.discovery.async_discover", return_value=[identified]):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: device.port}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Studio Profiler"
    assert result["result"].unique_id == "SER123"
    assert result["data"][CONF_SERIAL] == "SER123"
    assert result["data"][CONF_SW_VERSION] == "10.5.3"


async def test_a_silent_device_is_still_keyed_by_its_host(
    hass: HomeAssistant, device: FakeDevice, no_setup: AsyncMock
) -> None:
    """With the discovery port held, the host is all the identity there is."""
    result = await start(hass, [])
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: device.port}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == "127.0.0.1"
    assert result["data"][CONF_SERIAL] is None


async def test_re_adding_a_moved_device_by_hand_updates_the_entry(
    hass: HomeAssistant, device: FakeDevice, no_setup: AsyncMock
) -> None:
    """Typing in the new address of a known Profiler moves it, not clones it."""
    existing = MockConfigEntry(
        domain=DOMAIN, unique_id="SER123", data={CONF_HOST: "10.0.0.9", CONF_PORT: 5727}
    )
    existing.add_to_hass(hass)
    identified = Found(host="127.0.0.1", name="Studio Profiler", serial="SER123", version="10.5.3")

    result = await start(hass, [])
    with patch("custom_components.kemper.discovery.async_discover", return_value=[identified]):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: device.port}
        )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert existing.data[CONF_HOST] == "127.0.0.1"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
