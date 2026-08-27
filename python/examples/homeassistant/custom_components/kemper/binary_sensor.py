"""The ``active`` binary sensor: is anything actually coming out of the rig.

It reads nothing itself — :class:`~.activity.ActivityDetector` owns the meter
lane and tells this entity when the answer changes, which is twice per playing
session.
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import KemperConfigEntry, KemperCoordinator
from .entity import KemperEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KemperConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Profiler's binary sensors."""
    async_add_entities([KemperActiveBinarySensor(entry.runtime_data)])


class KemperActiveBinarySensor(KemperEntity, BinarySensorEntity):
    """On while the rig has passed signal inside the configured window."""

    _attr_translation_key = "active"

    def __init__(self, coordinator: KemperCoordinator) -> None:
        super().__init__(coordinator, "active")

    async def async_added_to_hass(self) -> None:
        """Follow the detector as well as the coordinator."""
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.activity.add_listener(self.async_write_ha_state))

    @property
    def is_on(self) -> bool:
        """Whether the detector currently reads as playing."""
        return self.coordinator.activity.active
