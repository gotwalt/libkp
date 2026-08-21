"""Typed parameter descriptors — a thin semantic layer over the pure name tables
in :mod:`libkp.params`.

Where :mod:`libkp.params` answers *"what is this address called?"*, the registry
answers *"what kind of value does it hold, and how should a value be shown?"*.

This is an offline lookup with no device or network involvement. It seeds a
**common** set of addresses (amp, amp EQ, cabinet, rig settings, input,
system/global) plus the four shared effect-slot parameters (Type, On/Off, Mix,
Volume). It intentionally does not enumerate every one of the 100+ per-effect
numbers — the full name map for those lives in :func:`libkp.params.param_name`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from . import _generated as gen
from . import params

__all__ = ["ParamKind", "ParamDescriptor", "descriptor", "format_value"]

#: Non-effect pages the registry types.
_COMMON_PAGES = (0x04, 0x09, 0x0A, 0x0B, 0x0C, 0x7F)

#: The only effect-slot numbers the registry types.
_COMMON_EFFECT_NUMBERS = (
    params.EFFECT_PARAM_TYPE,
    params.EFFECT_PARAM_STATE,
    params.EFFECT_PARAM_MIX,
    params.EFFECT_PARAM_VOLUME,
)


class ParamKind(Enum):
    """The kind of value a parameter holds — how to interpret its 14-bit value."""

    #: A fractional value spanning the full 0–16383 range (Gain, Volume…).
    CONTINUOUS = "continuous"
    #: An integer switch: 0 = off, non-zero = on (On/Off, *Enable*, *State*).
    SWITCH = "switch"
    #: An enumerated value that maps to a name via a lookup fn (effect Type).
    ENUM = "enum"


def _derive_kind(name: str) -> ParamKind:
    """Derive a :class:`ParamKind` from a parameter's name."""
    if name == "Type":
        return ParamKind.ENUM
    if "On/Off" in name or "Enable" in name or "State" in name:
        return ParamKind.SWITCH
    return ParamKind.CONTINUOUS


@dataclass(frozen=True, slots=True)
class ParamDescriptor:
    """A typed descriptor for a single parameter address."""

    #: Address page (NRPN MSB).
    page: int
    #: Address number within the page (NRPN LSB).
    number: int
    #: Human-readable name (from :mod:`libkp.params`).
    name: str
    #: How to interpret the value.
    kind: ParamKind
    #: Display unit, if any (reserved; unset for the seeded addresses).
    unit: str | None = None
    #: For :attr:`ParamKind.ENUM`, the value→name lookup; ``None`` otherwise.
    enum_names: Callable[[int], str | None] | None = None


def descriptor(page: int, number: int) -> ParamDescriptor | None:
    """Look up a typed descriptor for an address, or ``None`` if it is outside the
    seeded common set.

    Covered: amp (``$0A``), amp EQ (``$0B``), cabinet (``$0C``), rig settings
    (``$04``), input (``$09``), system/global (``$7F``), and the shared
    effect-slot parameters Type (0), On/Off (3), Mix (4), Volume (6) on the eight
    effect pages.
    """
    if params.is_effect_page(page):
        if number not in _COMMON_EFFECT_NUMBERS:
            return None
        name = params.param_name(page, number)
        if name is None:
            return None
        kind = _derive_kind(name)
        return ParamDescriptor(
            page=page,
            number=number,
            name=name,
            kind=kind,
            enum_names=params.effect_type_name if kind is ParamKind.ENUM else None,
        )

    if page in _COMMON_PAGES:
        name = params.param_name(page, number)
        if name is None:
            return None
        return ParamDescriptor(page=page, number=number, name=name, kind=_derive_kind(name))

    return None


def format_value(desc: ParamDescriptor, value: int) -> str:
    """Format a 14-bit ``value`` for display according to the descriptor's kind.

    - :attr:`ParamKind.SWITCH` → ``"On"`` / ``"Off"``.
    - :attr:`ParamKind.ENUM` → the enum name, or ``"type {value}"`` if unknown.
    - :attr:`ParamKind.CONTINUOUS` → a percentage of full scale, e.g. ``"42.3%"``.

    The percentage is a generic approximation. For an exact, device-accurate
    label (units, curves, note values), request the rendered string from the
    device via :func:`libkp.nrpn.request_rendered_string` (function ``$7C`` →
    ``$3C``).
    """
    if desc.kind is ParamKind.SWITCH:
        return "On" if value != 0 else "Off"
    if desc.kind is ParamKind.ENUM:
        name = desc.enum_names(value) if desc.enum_names is not None else None
        return name if name is not None else f"type {value}"
    return f"{value / gen.FULL_SCALE * 100.0:.1f}%"
