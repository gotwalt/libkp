"""The push coordinator: one :class:`DeviceModel`, one state tree, one device.

libkp's model is already a store — it holds the device state and hands out a
fresh snapshot whenever *slow* state changes, coalesced to at most one per
ingested chunk. So there is nothing to poll here and no update interval: the
coordinator is a :class:`DataUpdateCoordinator` whose data arrives from a
background task that does nothing but drain the model's snapshot queue.

That task is also where a lost stream is noticed. libkp can redial one on its
own, and this integration deliberately does not ask it to: the model would
redial the address it was given, and the whole point of keying an entry by the
Profiler's serial is that the address is the part that changes. So a loss ends
the session and asks Home Assistant to reload the entry, which starts again
from discovery. A session that ends almost as soon as it began is treated as a
device that is not really there and the reload waits
:data:`RELOAD_DELAY_SECONDS`, so nothing can spin setup in a loop.

The fast lane (meters, beat pulse, tuner deviance) never reaches this class.
It is read only by :class:`~.activity.ActivityDetector`, which turns it into
two state writes per playing session.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_NAME
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .activity import ActivityDetector
from .const import (
    CONF_ACTIVITY_THRESHOLD,
    CONF_ACTIVITY_WINDOW,
    CONF_SW_VERSION,
    DEFAULT_ACTIVITY_THRESHOLD,
    DEFAULT_ACTIVITY_WINDOW,
    DEFAULT_NAME,
    DOMAIN,
    MANUFACTURER,
    MODEL,
)
from .libkp.model import DeviceModel
from .libkp.state import Connection, DeviceState

_LOGGER = logging.getLogger(__name__)

#: A session that ends sooner than this after it opened says the device is not
#: really available, whatever its handshake said.
SHORT_SESSION_SECONDS = 60.0
#: How long such a session waits before the entry is reloaded. A healthy
#: session that ends — the amp switched off after a rehearsal — reloads at once.
RELOAD_DELAY_SECONDS = 30.0

#: The entry, typed by what :attr:`ConfigEntry.runtime_data` holds.
type KemperConfigEntry = ConfigEntry[KemperCoordinator]


def activity_window(entry: ConfigEntry) -> float:
    """The configured quiet window, in seconds (the form asks for minutes)."""
    return float(entry.options.get(CONF_ACTIVITY_WINDOW, DEFAULT_ACTIVITY_WINDOW)) * 60.0


def activity_threshold(entry: ConfigEntry) -> float:
    """The configured level threshold, in percent of the meter full scale."""
    return float(entry.options.get(CONF_ACTIVITY_THRESHOLD, DEFAULT_ACTIVITY_THRESHOLD))


class KemperCoordinator(DataUpdateCoordinator[DeviceState]):
    """Publishes the model's slow-lane snapshots to the entity layer."""

    config_entry: KemperConfigEntry

    def __init__(self, hass: HomeAssistant, entry: KemperConfigEntry, model: DeviceModel) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.data[CONF_HOST]}",
            update_interval=None,
        )
        self.model = model
        self.activity = ActivityDetector(
            hass,
            model,
            window=activity_window(entry),
            threshold=activity_threshold(entry),
        )
        self._task: asyncio.Task[None] | None = None
        self._reload_timer: CALLBACK_TYPE | None = None
        self._opened = dt_util.utcnow()
        #: Set once the entry is being torn down, so the disconnection the
        #: teardown itself causes is not mistaken for the device going away.
        self._closing = False

    @property
    def reload_pending(self) -> bool:
        """Whether a lost stream is waiting to reload the entry."""
        return self._reload_timer is not None

    @property
    def device_id(self) -> str:
        """The device-registry identifier: the serial when discovery knew it,
        else the host, else the entry — stable across restarts either way."""
        entry = self.config_entry
        return entry.unique_id or entry.entry_id

    @property
    def device_info(self) -> DeviceInfo:
        """One device per config entry: the Profiler itself."""
        entry = self.config_entry
        return DeviceInfo(
            identifiers={(DOMAIN, self.device_id)},
            manufacturer=MANUFACTURER,
            model=MODEL,
            name=entry.data.get(CONF_NAME) or DEFAULT_NAME,
            sw_version=entry.data.get(CONF_SW_VERSION),
        )

    async def async_start(self) -> None:
        """Seed the first snapshot, attach the detector, start listening."""
        self._opened = dt_util.utcnow()
        self.async_set_updated_data(self.model.state())
        self.activity.start()
        self._task = self.config_entry.async_create_background_task(
            self.hass, self._listen(), name=f"{DOMAIN} {self.config_entry.data[CONF_HOST]} state"
        )

    async def _listen(self) -> None:
        """Drain the model's store; every snapshot is an entity update.

        The loop ends when the device goes away, which is the one thing a
        snapshot can say that this class acts on rather than passes along.
        """
        queue = self.model.subscribe()
        try:
            while True:
                state = await queue.get()
                self.async_set_updated_data(state)
                if state.connection is Connection.DISCONNECTED:
                    self._schedule_reload()
                    return
        finally:
            self.model.unsubscribe(queue)

    @callback
    def _schedule_reload(self) -> None:
        """Ask for a reload, so the way back starts at discovery."""
        if self._closing or self._reload_timer is not None:
            return
        session = (dt_util.utcnow() - self._opened).total_seconds()
        delay = 0.0 if session >= SHORT_SESSION_SECONDS else RELOAD_DELAY_SECONDS
        _LOGGER.info(
            "Lost the stream to the Profiler after %.0f s; reloading in %.0f s to find it again",
            session,
            delay,
        )
        self._reload_timer = async_call_later(self.hass, delay, self._reload)

    @callback
    def _reload(self, _now: object) -> None:
        self._reload_timer = None
        self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)

    async def async_shutdown(self) -> None:
        """Stop listening and hang up. The device sees one clean disconnect."""
        self._closing = True
        if self._reload_timer is not None:
            self._reload_timer()
            self._reload_timer = None
        await super().async_shutdown()
        self.activity.stop()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self.model.close()

    def apply_options(self) -> None:
        """Re-read the options the detector uses, without touching the socket."""
        entry = self.config_entry
        self.activity.update_options(
            window=activity_window(entry), threshold=activity_threshold(entry)
        )
