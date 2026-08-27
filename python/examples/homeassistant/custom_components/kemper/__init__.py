"""The Kemper Profiler integration: one config entry, one device, one session.

Setting an entry up opens exactly one MIDI3 stream to the Profiler and keeps
it. The device tolerates a session; what it does not tolerate is connection
*churn* (``docs/06``, ``docs/11``), so nothing here dials in a loop.

**Where the device is** is decided fresh at every setup. The entry's identity
is the serial the Profiler advertises, not its address: an entry that knows a
serial broadcasts once, and if that serial answers from somewhere else the
entry is updated to the new address (and to the name and firmware version,
which change too) before anything is dialed. Discovery finding nothing — the
port held by Rig Manager, a quiet network, a device on another subnet — is not
an error; the stored address is used as it stands.

**Losing the stream** therefore goes back through the same door instead of
through libkp's own redial: reconnecting to a remembered address would keep
dialing an address the device may have left. The coordinator asks for a reload,
setup rediscovers, and a device that is simply switched off fails with
:class:`ConfigEntryNotReady`, which is Home Assistant's own spaced retry.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import CONF_SERIAL, CONF_SW_VERSION
from .coordinator import KemperConfigEntry, KemperCoordinator
from .discovery import async_find_serial
from .libkp import ConnectOptions, ControlPolicy, DeviceModel, LibKPError
from .libkp.protocol import PORT

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]

#: How the integration connects. The CBOR control channel is deliberately off:
#: the only thing it adds over the stream is the morph position, which nothing
#: here surfaces, and it would cost the device a second socket for as long as
#: Home Assistant runs. No reconnect policy either — see the module docstring:
#: coming back is a reload, so that the address is looked up again first.
CONNECT_CONTROL = ControlPolicy.OFF


async def async_locate(hass: HomeAssistant, entry: KemperConfigEntry) -> str:
    """The address to dial, after asking the network where the serial is.

    Returns the stored host unchanged when the entry predates serial-keying,
    when nothing answers, or when the device is where it was; otherwise the
    entry is updated in place — the address, and the name and version, which a
    firmware update or a rename changes just as quietly.
    """
    host: str = entry.data[CONF_HOST]
    serial: str | None = entry.data.get(CONF_SERIAL)
    if not serial:
        return host

    found = await async_find_serial(serial)
    if found is None:
        return host

    updates = {
        key: value
        for key, value in (
            (CONF_HOST, found.host),
            (CONF_NAME, found.name),
            (CONF_SW_VERSION, found.version),
        )
        if entry.data.get(key) != value
    }
    if not updates:
        return host
    if CONF_HOST in updates:
        _LOGGER.info(
            "Profiler %s answered from %s instead of %s; following it",
            serial,
            found.host,
            host,
        )
    hass.config_entries.async_update_entry(entry, data={**entry.data, **updates})
    return found.host


async def async_setup_entry(hass: HomeAssistant, entry: KemperConfigEntry) -> bool:
    """Find the Profiler, connect to it, and bring its entities up."""
    host = await async_locate(hass, entry)
    options = ConnectOptions(port=entry.data.get(CONF_PORT, PORT), control=CONNECT_CONTROL)
    try:
        model = await DeviceModel.connect(host, options=options)
    except (LibKPError, OSError) as err:
        raise ConfigEntryNotReady(f"could not connect to the Profiler at {host}: {err}") from err

    coordinator = KemperCoordinator(hass, entry, model)
    entry.runtime_data = coordinator
    try:
        await coordinator.async_start()
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        # Whatever went wrong, the socket does not get to outlive the attempt.
        await coordinator.async_shutdown()
        raise

    entry.async_on_unload(entry.add_update_listener(async_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: KemperConfigEntry) -> bool:
    """Tear the entities down and hang up on the device."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_shutdown()
    return unloaded


async def async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply changed options in place — never by reloading the entry.

    A reload would close the session and open another one, which is a real cost
    to the device; the two options that exist only steer the activity detector,
    and it can be retuned while it runs. The same listener sees the address
    updates :func:`async_locate` makes, which need no action at all: they are
    already what the running session was dialed with.
    """
    coordinator: KemperCoordinator | None = getattr(entry, "runtime_data", None)
    if coordinator is not None:
        coordinator.apply_options()
