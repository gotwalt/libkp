"""Fixtures: a Home Assistant that loads the integration, against a fake Profiler.

The device side is libkp's own :class:`fake_device.FakeDevice` — the same
in-process stand-in its test suite drives — so these tests exercise the real
session handshake, the real MIDI3 framing and the real state fold, and mock
nothing below the config entry.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from unittest.mock import patch

import pytest
from fake_device import FakeDevice, answer_requests
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kemper.const import CONF_SERIAL, CONF_SW_VERSION, DOMAIN
from custom_components.kemper.libkp.errors import PortUnavailableError
from custom_components.kemper.libkp.protocol import PORT

#: The serial the fixture entry claims, and so the prefix of every unique id.
SERIAL = "FAKE-SERIAL"
DEVICE_NAME = "Test Profiler"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant see `custom_components/kemper` in every test."""
    return


@pytest.fixture(autouse=True)
def no_broadcast():
    """No test polls the real LAN.

    The discovery port reads as held by another program, which is a state the
    integration is built to shrug off: every path that discovers falls back to
    what the entry already knows. A test that wants replies patches
    ``custom_components.kemper.discovery.async_discover`` instead.
    """
    with patch(
        "custom_components.kemper.discovery.DiscoveryPort.acquire",
        side_effect=PortUnavailableError(PORT, OSError("held by the test suite")),
    ):
        yield


@pytest.fixture
async def device(socket_enabled: None) -> AsyncIterator[FakeDevice]:
    """A Profiler stand-in that answers the model's opening burst.

    ``socket_enabled`` lifts Home Assistant's test-suite ban on real sockets:
    these tests deliberately want one, since a loopback TCP session is exactly
    what the integration does in the field.
    """
    fake = await FakeDevice(responder=answer_requests).start()
    try:
        yield fake
    finally:
        # Hang up first: a server whose handlers are still running never
        # finishes closing, and an entry that failed to unload leaves one.
        await fake.hangup()
        await fake.stop()


def make_entry(device: FakeDevice) -> MockConfigEntry:
    """A config entry pointing at the fake device's ephemeral port."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=DEVICE_NAME,
        unique_id=SERIAL,
        data={
            CONF_HOST: "127.0.0.1",
            CONF_PORT: device.port,
            CONF_NAME: DEVICE_NAME,
            CONF_SERIAL: SERIAL,
            CONF_SW_VERSION: "1.2.3",
        },
    )


@pytest.fixture
async def entry(hass: HomeAssistant, device: FakeDevice) -> AsyncIterator[MockConfigEntry]:
    """A loaded config entry, unloaded again with the test.

    Unloading matters here: it is what closes the session and disarms the
    detector's timer, and Home Assistant's test harness fails a test that
    leaves either behind.
    """
    entry = make_entry(device)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    yield entry
    if entry.state is ConfigEntryState.LOADED:
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


def entity_id(hass: HomeAssistant, platform: str, key: str) -> str:
    """The entity id the registry gave one of the integration's unique ids."""
    found = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{SERIAL}_{key}")
    assert found is not None, f"no {platform} entity registered for {key}"
    return found


async def wait_until(predicate, timeout: float = 5.0) -> None:
    """Wait until ``predicate`` is true; the wire is asynchronous."""
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


async def wait_for_state(
    hass: HomeAssistant, entity: str, value: str, timeout: float = 5.0
) -> None:
    """Wait until ``entity`` reads ``value``; the wire is asynchronous."""
    async with asyncio.timeout(timeout):
        while True:
            state = hass.states.get(entity)
            if state is not None and state.state == value:
                return
            await asyncio.sleep(0.01)
