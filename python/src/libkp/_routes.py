"""The routing table's lookup and the field switch behind it.

:data:`libkp._generated.STATE_ROUTES` is data only: one row per tracked flat
address saying which tree field it writes and how the value decodes. The two
things the generator deliberately does not emit live here — finding the row for
an address, and the hand-written switch that turns a :class:`Field` into an
attribute of :class:`~libkp.state.DeviceState`. Python has no exhaustiveness
check over the enum, so ``tests/test_state.py`` walks every ``Field`` through
:func:`write` and :func:`read` instead.

Nothing in this module decides *whether* a value lands; that is
:meth:`libkp.state.DeviceState.apply_update`. This is only where a value goes
once it has.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import _generated as gen
from ._generated import Field, Route

if TYPE_CHECKING:
    from .state import DeviceState

__all__ = ["lookup", "span", "read", "write"]

#: The table keyed by flat address, built on first use. The generated tuple is
#: address-sorted, but a dict answers the per-message lookup in one hop and
#: costs nothing until the first update arrives.
_BY_ADDRESS: dict[int, Route] = {}
#: How many rows share each field: the span of a block folded as a unit.
_SPAN_BY_FIELD: dict[Field, int] = {}


def _index() -> None:
    if _BY_ADDRESS:
        return
    for route in gen.STATE_ROUTES:
        _BY_ADDRESS[route.address] = route
        _SPAN_BY_FIELD[route.field] = _SPAN_BY_FIELD.get(route.field, 0) + 1


def lookup(address: int) -> Route | None:
    """The route whose address is ``address``, or ``None`` if the tree does not
    track it."""
    _index()
    return _BY_ADDRESS.get(address)


def span(route: Route) -> int:
    """How many consecutive addresses ``route``'s field covers: 1 for a scalar,
    the block length for a field expanded per element (the meter frame)."""
    _index()
    return _SPAN_BY_FIELD[route.field]


def read(state: DeviceState, route: Route) -> object:
    """The value the tree currently holds for ``route`` — what an update is
    compared against before it is stored."""
    field, slot = route.field, route.slot
    if field is Field.RIG_NAME:
        return state.rig.name
    if field is Field.RIG_AUTHOR:
        return state.rig.author
    if field is Field.RIG_DATE:
        return state.rig.date
    if field is Field.RIG_COMMENT:
        return state.rig.comment
    if field is Field.AMP_NAME:
        return state.amp.name
    if field is Field.CABINET_NAME:
        return state.cabinet.name
    if field is Field.MORPH_BUTTON:
        # Momentary: the tree keeps nothing, so there is never a match.
        return None
    if field is Field.MORPH_POSITION:
        return state.morph
    if field is Field.TEMPO_BPM:
        return state.rig.tempo_bpm
    if field is Field.RIG_VOLUME:
        return state.rig.volume
    if field is Field.AMP_ON:
        return state.amp.on
    if field is Field.AMP_GAIN:
        return state.amp.gain
    if field is Field.CABINET_ON:
        return state.cabinet.on
    if field is Field.EFFECT_TYPE:
        return state.effects[slot].kind
    if field is Field.EFFECT_ON:
        return state.effects[slot].on
    if field is Field.EFFECT_MIX:
        return state.effects[slot].mix
    if field is Field.BEAT_PULSE:
        # Momentary, like the morph button.
        return None
    if field is Field.TUNER_DEVIANCE:
        return state.tuner.deviance
    if field is Field.STATUS:
        return state.status
    if field is Field.TUNER_NOTE:
        return state.tuner.note
    if field is Field.MAIN_VOLUME:
        return state.output.main_volume
    if field is Field.HEADPHONE_VOLUME:
        return state.output.headphone_volume
    if field is Field.MONITOR_VOLUME:
        return state.output.monitor_volume
    if field is Field.BANK_RIG_NAME:
        return state.bank.slots[slot].rig_name
    if field is Field.BANK_AMP_NAME:
        return state.bank.slots[slot].amp_name
    if field is Field.BANK_CABINET_NAME:
        return state.bank.slots[slot].cabinet_name
    if field is Field.CURRENT_BANK:
        return state.current_bank
    if field is Field.CURRENT_RIG_SLOT:
        return state.current_rig_slot
    raise ValueError(f"no read for {field}")


def write(state: DeviceState, route: Route, value: object) -> None:
    """Store an already-decoded ``value`` into the field ``route`` names.

    ``value`` is whatever :meth:`~libkp.state.DeviceState.apply_update` made of
    the wire value for the row's ``kind``: an ``int``, a ``bool``, a ``str`` or a
    :class:`~libkp.state.RealtimeStatus`. The momentaries (the morph button, the
    beat pulse) have no home in the tree and store nothing.
    """
    field, slot = route.field, route.slot
    if field is Field.RIG_NAME:
        state.rig.name = value
    elif field is Field.RIG_AUTHOR:
        state.rig.author = value
    elif field is Field.RIG_DATE:
        state.rig.date = value
    elif field is Field.RIG_COMMENT:
        state.rig.comment = value
    elif field is Field.AMP_NAME:
        state.amp.name = value
    elif field is Field.CABINET_NAME:
        state.cabinet.name = value
    elif field is Field.MORPH_BUTTON:
        pass
    elif field is Field.MORPH_POSITION:
        state.morph = value
    elif field is Field.TEMPO_BPM:
        state.rig.tempo_bpm = value
    elif field is Field.RIG_VOLUME:
        state.rig.volume = value
    elif field is Field.AMP_ON:
        state.amp.on = value
    elif field is Field.AMP_GAIN:
        state.amp.gain = value
    elif field is Field.CABINET_ON:
        state.cabinet.on = value
    elif field is Field.EFFECT_TYPE:
        state.effects[slot].kind = value
    elif field is Field.EFFECT_ON:
        state.effects[slot].on = value
    elif field is Field.EFFECT_MIX:
        state.effects[slot].mix = value
    elif field is Field.BEAT_PULSE:
        pass
    elif field is Field.TUNER_DEVIANCE:
        state.tuner.deviance = value
    elif field is Field.STATUS:
        state.status = value
    elif field is Field.TUNER_NOTE:
        state.tuner.note = value
    elif field is Field.MAIN_VOLUME:
        state.output.main_volume = value
    elif field is Field.HEADPHONE_VOLUME:
        state.output.headphone_volume = value
    elif field is Field.MONITOR_VOLUME:
        state.output.monitor_volume = value
    elif field is Field.BANK_RIG_NAME:
        state.bank.slots[slot].rig_name = value
    elif field is Field.BANK_AMP_NAME:
        state.bank.slots[slot].amp_name = value
    elif field is Field.BANK_CABINET_NAME:
        state.bank.slots[slot].cabinet_name = value
    elif field is Field.CURRENT_BANK:
        state.current_bank = value
    elif field is Field.CURRENT_RIG_SLOT:
        state.current_rig_slot = value
    else:
        raise ValueError(f"no write for {field}")
