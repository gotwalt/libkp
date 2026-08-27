"""The base entity: device identity, naming and availability in one place."""

from __future__ import annotations

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import KemperCoordinator
from .libkp.state import Connection

#: The connection states in which what the entities show is live. A degraded
#: connection is still a connection: the stream — everything these entities
#: read — is open, and only the optional control channel is missing.
LIVE = (Connection.CONNECTED, Connection.DEGRADED)


class KemperEntity(CoordinatorEntity[KemperCoordinator]):
    """One reading from one Profiler."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: KemperCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.device_id}_{key}"
        self._attr_device_info = coordinator.device_info

    @property
    def available(self) -> bool:
        """Available while the stream is up — the tree goes stale without it."""
        state = self.coordinator.data
        return super().available and state is not None and state.connection in LIVE
