"""Parameter-name lookups — a pure, offline ``page/number -> human name`` layer.

Every table consulted here lives in the generated :mod:`libkp._generated`
module, which is emitted from the shared spec. The names are transcribed from
the **Kemper MIDI Parameter Documentation**, extended with addresses credited to
**PySwitch** (the Fixed FX page, the tuner/tempo addresses, and the numeric
morph-state parameter on page 0). The realtime meter field identities on page
``0x7C`` come from observed experimentation.
"""

from __future__ import annotations

from . import _generated as gen

__all__ = [
    "EFFECT_SLOTS",
    "EFFECT_SLOT_NAMES",
    "EFFECT_PAGES",
    "EFFECT_PARAM_TYPE",
    "EFFECT_PARAM_STATE",
    "EFFECT_PARAM_MIX",
    "EFFECT_PARAM_VOLUME",
    "function_name",
    "page_name",
    "param_name",
    "is_effect_page",
    "effect_slot_page",
    "effect_slot_name",
    "effect_slot_index",
    "effect_type_name",
    "string_tag_name",
    "page0_numeric_name",
    "describe",
    "describe_numeric",
]

#: The eight effect slots in signal-chain order: ``(short name, address page)``.
EFFECT_SLOTS: tuple[tuple[str, int], ...] = tuple((name, page) for name, page in gen.EFFECT_SLOTS)

#: Just the slot short names, in signal-chain order.
EFFECT_SLOT_NAMES: tuple[str, ...] = tuple(name for name, _ in EFFECT_SLOTS)

#: Just the slot address pages, in signal-chain order.
EFFECT_PAGES: tuple[int, ...] = tuple(page for _, page in EFFECT_SLOTS)

#: Effect-slot parameter number: the effect Type (value → :func:`effect_type_name`).
EFFECT_PARAM_TYPE: int = gen.EFFECT_PARAM_TYPE
#: Effect-slot parameter number: On/Off state (1 = on, 0 = off).
EFFECT_PARAM_STATE: int = gen.EFFECT_PARAM_STATE
#: Effect-slot parameter number: dry/wet Mix (0–16383).
EFFECT_PARAM_MIX: int = gen.EFFECT_PARAM_MIX
#: Effect-slot parameter number: output Volume (0–16383).
EFFECT_PARAM_VOLUME: int = gen.EFFECT_PARAM_VOLUME

_EFFECT_PAGE_SET = frozenset(EFFECT_PAGES)
_SLOT_INDEX = {name.upper(): i for i, name in enumerate(EFFECT_SLOT_NAMES)}
_PAGE_INDEX = {page: i for i, page in enumerate(EFFECT_PAGES)}


def function_name(function: int) -> str | None:
    """Name of a SysEx function code, or ``None`` if unknown."""
    return gen.FUNCTION_NAMES.get(function)


def page_name(page: int) -> str | None:
    """Name of an address page (section), or ``None`` if undocumented."""
    return gen.PAGE_NAMES.get(page)


def is_effect_page(page: int) -> bool:
    """True if ``page`` is one of the eight effect-module pages (A..REV)."""
    return page in _EFFECT_PAGE_SET


def param_name(page: int, number: int) -> str | None:
    """Look up a parameter name by page/number. All effect modules share one map.

    Page 0 resolves as a **string tag**; for numeric-function traffic on page 0
    use :func:`page0_numeric_name` instead.
    """
    if page == gen.PAGE_STRINGS:
        return string_tag_name(number)
    if is_effect_page(page):
        return gen.EFFECT_PARAMS.get(number)
    return gen.NON_EFFECT_PARAMS.get((page, number))


def effect_slot_page(name: str) -> int | None:
    """Resolve a slot name (case-insensitive: ``"a"``, ``"REV"``, ``"dly"``…) to
    its address page."""
    idx = _SLOT_INDEX.get(name.upper())
    return None if idx is None else EFFECT_PAGES[idx]


def effect_slot_name(page: int) -> str | None:
    """Resolve an effect page to its slot short name."""
    idx = _PAGE_INDEX.get(page)
    return None if idx is None else EFFECT_SLOT_NAMES[idx]


def effect_slot_index(page: int) -> int | None:
    """Index of the effect slot on ``page`` within :data:`EFFECT_SLOTS`, if any."""
    return _PAGE_INDEX.get(page)


def effect_type_name(value: int) -> str | None:
    """Effect Type value (effect-module number 0) → name."""
    return gen.EFFECT_TYPES.get(value)


def string_tag_name(number: int) -> str | None:
    """String-tag names on page 0 — functions ``$03``/``$43`` only."""
    return gen.STRING_TAGS.get(number)


def page0_numeric_name(number: int) -> str | None:
    """Page 0 is dual-use: string tags via ``$03``/``$43``, but *numeric* params
    via ``$01``/``$41`` (credited to PySwitch, which reads morph state this way)."""
    return gen.PAGE0_NUMERIC.get(number)


def describe(page: int, number: int) -> str:
    """Render a ``"Page: Param"`` label for a page/number pair, falling back to hex."""
    pname = page_name(page)
    if pname is None:
        return f"page 0x{page:02X} #{number} (0x{number:02X})"
    param = param_name(page, number)
    if param is None:
        return f"{pname}: #{number} (0x{number:02X})"
    return f"{pname}: {param}"


def describe_numeric(page: int, number: int) -> str:
    """Like :func:`describe`, but for numeric-function messages (``$01``/``$02``/
    ``$41``), where page 0 addresses numeric parameters instead of string tags."""
    if page == gen.PAGE_STRINGS:
        name = page0_numeric_name(number)
        if name is None:
            return f"Page 0: #{number} (0x{number:02X})"
        return f"Page 0: {name}"
    return describe(page, number)
