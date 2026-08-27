"""Sensors: what is loaded on the Profiler, and when it last made a sound.

The three name sensors are a table of :class:`SensorEntityDescription` rows
with a ``value_fn`` over the state tree, so the tempo, the volumes, the morph
position or an effect slot's type is one row each whenever they are wanted.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import KemperConfigEntry, KemperCoordinator
from .entity import KemperEntity
from .libkp.state import DeviceState


@dataclass(frozen=True, kw_only=True)
class KemperSensorEntityDescription(SensorEntityDescription):
    """A sensor described by where it reads from in the state tree."""

    value_fn: Callable[[DeviceState], str | None]


SENSORS: tuple[KemperSensorEntityDescription, ...] = (
    KemperSensorEntityDescription(
        key="rig_name",
        translation_key="rig_name",
        value_fn=lambda state: state.rig.name,
    ),
    KemperSensorEntityDescription(
        key="amp_name",
        translation_key="amp_name",
        value_fn=lambda state: state.amp.name,
    ),
    KemperSensorEntityDescription(
        key="cabinet_name",
        translation_key="cabinet_name",
        value_fn=lambda state: state.cabinet.name,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KemperConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Profiler's sensors."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [
        KemperSensor(coordinator, description) for description in SENSORS
    ]
    entities.append(KemperLastActivitySensor(coordinator))
    async_add_entities(entities)


class KemperSensor(KemperEntity, SensorEntity):
    """One value read straight out of the state tree."""

    entity_description: KemperSensorEntityDescription

    def __init__(
        self, coordinator: KemperCoordinator, description: KemperSensorEntityDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> str | None:
        """The described value, or ``None`` while the device has not said."""
        state = self.coordinator.data
        return None if state is None else self.entity_description.value_fn(state)


class KemperLastActivitySensor(KemperEntity, SensorEntity):
    """When signal was last heard, written on the detector's transitions only."""

    _attr_translation_key = "last_activity"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: KemperCoordinator) -> None:
        super().__init__(coordinator, "last_activity")

    async def async_added_to_hass(self) -> None:
        """Follow the detector as well as the coordinator."""
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.activity.add_listener(self.async_write_ha_state))

    @property
    def native_value(self) -> datetime | None:
        """The last crossing the detector settled on, or ``None`` before one."""
        return self.coordinator.activity.last_activity
