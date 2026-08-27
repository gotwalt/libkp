"""Diagnostics: the whole state tree, which is the debugging window.

Only five entities are exposed, but the model knows far more than that — the
effect slots, the tempo, the volumes, the tuner, the bank preview, both
channels' states. Dumping the tree here means a bug report carries everything
the device said without any of it having to become an entity first.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant

from .const import CONF_SERIAL
from .coordinator import KemperConfigEntry

TO_REDACT = {CONF_HOST, CONF_SERIAL}


def plain(value: Any) -> Any:
    """A JSON-friendly copy of a state tree: dataclasses to dicts, enums to
    their values, private bookkeeping fields left out."""
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: plain(getattr(value, field.name))
            for field in fields(value)
            if not field.name.startswith("_")
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (list, tuple, set)):
        return [plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): plain(item) for key, item in value.items()}
    return value


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: KemperConfigEntry
) -> dict[str, Any]:
    """Everything this integration knows about one Profiler."""
    coordinator = entry.runtime_data
    detector = coordinator.activity
    last_activity = detector.last_activity
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "activity": {
            "active": detector.active,
            "last_activity": None if last_activity is None else last_activity.isoformat(),
        },
        "state": plain(coordinator.model.state()),
    }
