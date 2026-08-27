"""The config flow: find the Profiler if we can, ask for it if we cannot.

Discovery comes first because the Profiler answers a UDP broadcast with its
name, serial and firmware version — everything the device registry wants —
without costing it a TCP session. The port is exclusive (one process at a
time), so Rig Manager or a running meters example holding it is an ordinary
outcome, not an error: the flow falls through to a host/port form.

A manually entered host is checked with exactly **one** session, opened and
closed, and then asked who it is with a short directed poll — so a hand-added
Profiler is keyed by its serial too, and survives moving to another address.
Only a device that answers no poll at all is keyed by its host. There is no
retry loop anywhere in this file; the device does not tolerate connection
churn (``docs/06``).
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_DEVICE, CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)

from .const import (
    CONF_ACTIVITY_THRESHOLD,
    CONF_ACTIVITY_WINDOW,
    CONF_SERIAL,
    CONF_SW_VERSION,
    DEFAULT_ACTIVITY_THRESHOLD,
    DEFAULT_ACTIVITY_WINDOW,
    DEFAULT_NAME,
    DOMAIN,
)
from .discovery import Found, async_discover, async_identify
from .libkp import ConnectOptions, ControlPolicy, DeviceModel, LibKPError, SyncStrategy
from .libkp.protocol import PORT

#: The sentinel option that leaves the device list for the manual form.
MANUAL = "manual"


async def async_check(host: str, port: int) -> None:
    """Prove a host is a Profiler: one session, opened and closed.

    Nothing is requested on it — the point is the handshake, and the burst of
    reads belongs to the entry that goes on to hold the session.
    """
    model = await DeviceModel.connect(
        host,
        options=ConnectOptions(port=port, control=ControlPolicy.OFF, sync=SyncStrategy.OFF),
    )
    await model.close()


class KemperConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add one Profiler."""

    VERSION = 1

    def __init__(self) -> None:
        self._found: list[Found] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Look for Profilers, then pick one or type one in."""
        self._found = await async_discover()
        if not self._found:
            return await self.async_step_manual()
        return await self.async_step_pick()

    async def async_step_pick(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Choose among the Profilers that answered."""
        if user_input is not None:
            chosen = user_input[CONF_DEVICE]
            if chosen == MANUAL:
                return await self.async_step_manual()
            found = next(device for device in self._found if device.host == chosen)
            await self.async_set_unique_id(found.serial or found.host)
            self._abort_if_unique_id_configured(updates={CONF_HOST: found.host})
            return self.async_create_entry(
                title=found.name,
                data={
                    CONF_HOST: found.host,
                    CONF_PORT: PORT,
                    CONF_NAME: found.name,
                    CONF_SERIAL: found.serial,
                    CONF_SW_VERSION: found.version,
                },
            )

        options = [
            SelectOptionDict(value=device.host, label=f"{device.name} ({device.host})")
            for device in self._found
        ]
        options.append(SelectOptionDict(value=MANUAL, label="Enter a host manually"))
        return self.async_show_form(
            step_id="pick",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DEVICE): SelectSelector(
                        SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST)
                    )
                }
            ),
        )

    async def async_step_manual(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Type in a host that discovery could not find."""
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = int(user_input[CONF_PORT])
            try:
                await async_check(host, port)
            except LibKPError, OSError:
                errors["base"] = "cannot_connect"
            except Exception:  # the form must survive anything the stack raises
                errors["base"] = "unknown"
            else:
                # It is a Profiler; now ask it who it is, so the entry is keyed
                # by the serial rather than by an address that can change.
                found = await async_identify(host) or Found(
                    host=host, name=DEFAULT_NAME, serial=None, version=None
                )
                await self.async_set_unique_id(found.serial or host)
                self._abort_if_unique_id_configured(updates={CONF_HOST: host})
                return self.async_create_entry(
                    title=found.name,
                    data={
                        CONF_HOST: host,
                        CONF_PORT: port,
                        CONF_NAME: found.name,
                        CONF_SERIAL: found.serial,
                        CONF_SW_VERSION: found.version,
                    },
                )

        suggested = user_input or {CONF_PORT: PORT}
        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST, default=suggested.get(CONF_HOST, "")): TextSelector(),
                    vol.Required(CONF_PORT, default=suggested.get(CONF_PORT, PORT)): vol.All(
                        vol.Coerce(int), vol.Range(min=1, max=65535)
                    ),
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> KemperOptionsFlow:
        """The two knobs the activity detector has."""
        return KemperOptionsFlow()


class KemperOptionsFlow(OptionsFlow):
    """How loud, and for how long, counts as playing.

    Saving these does **not** reload the entry: the integration applies them to
    the running detector, so tuning them never costs the device a session.
    """

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show and store the detector's window and threshold."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ACTIVITY_WINDOW,
                        default=options.get(CONF_ACTIVITY_WINDOW, DEFAULT_ACTIVITY_WINDOW),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=1,
                            max=120,
                            step=1,
                            unit_of_measurement="min",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Required(
                        CONF_ACTIVITY_THRESHOLD,
                        default=options.get(CONF_ACTIVITY_THRESHOLD, DEFAULT_ACTIVITY_THRESHOLD),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=0,
                            max=100,
                            step=0.5,
                            unit_of_measurement="%",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                }
            ),
        )
