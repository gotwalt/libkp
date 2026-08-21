"""Kemper NRPN-over-SysEx message helpers.

Layout::

    F0 00 20 33 <product> <device> <function> <instance=00> <page> <number> <values…> F7

The message grammar, the function codes, and the address scheme follow the
Kemper MIDI Parameter Documentation; the bidirectional beacon (function
``0x7E``) is credited to PySwitch. The beacon asks the Profiler to start
streaming a selected parameter set and to send a status "sense" message roughly
every 500 ms; it must be re-sent within the lease to stay alive.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import _generated as gen

__all__ = [
    "MANUFACTURER_ID",
    "PRODUCT_PROFILER",
    "PRODUCT_PLAYER",
    "DEVICE_OMNI",
    "FULL_SCALE",
    "PAGE_STRINGS",
    "STRING_RIG_NAME",
    "FUNCTION_SINGLE_PARAM",
    "FUNCTION_MULTI_PARAM",
    "FUNCTION_STRING_PARAM",
    "FUNCTION_EXT_PARAM",
    "FUNCTION_EXT_STRING_PARAM",
    "FUNCTION_RENDERED_STRING_REPLY",
    "FUNCTION_REQUEST_SINGLE",
    "FUNCTION_REQUEST_MULTI",
    "FUNCTION_REQUEST_STRING",
    "FUNCTION_REQUEST_RENDERED_STRING",
    "FUNCTION_BEACON",
    "NrpnHeader",
    "u14",
    "u14_split",
    "sysex",
    "beacon",
    "beacon_flags",
    "request_string",
    "request_single",
    "request_multi",
    "request_rendered_string",
    "set_single",
    "control_change",
    "program_change",
    "multi_values",
    "ext_decode",
    "ext_encode",
    "parse_extended_string",
    "parse_rendered_string",
]

#: Kemper MIDI manufacturer ID.
MANUFACTURER_ID: bytes = bytes(gen.MANUFACTURER_ID)

#: Product type: original Profiler.
PRODUCT_PROFILER: int = gen.PRODUCT_PROFILER
#: Product type: Profiler Player (and Player-class units).
PRODUCT_PLAYER: int = gen.PRODUCT_PLAYER
#: Omni device id (all devices).
DEVICE_OMNI: int = gen.DEVICE_OMNI
#: The maximum 14-bit NRPN value.
FULL_SCALE: int = gen.FULL_SCALE

#: Address page holding the string tags (rig/amp/cab names).
PAGE_STRINGS: int = gen.PAGE_STRINGS
#: String-tag number for the current Rig Name (page 0).
STRING_RIG_NAME: int = gen.STRING_RIG_NAME

# SysEx function codes (Kemper MIDI Parameter Documentation, Table 5).
FUNCTION_SINGLE_PARAM: int = gen.FN_SINGLE_PARAM
FUNCTION_MULTI_PARAM: int = gen.FN_MULTI_PARAM
FUNCTION_STRING_PARAM: int = gen.FN_STRING_PARAM
FUNCTION_EXT_PARAM: int = gen.FN_EXT_PARAM
FUNCTION_EXT_STRING_PARAM: int = gen.FN_EXT_STRING_PARAM
FUNCTION_RENDERED_STRING_REPLY: int = gen.FN_RENDERED_STRING_REPLY
FUNCTION_REQUEST_SINGLE: int = gen.FN_REQUEST_SINGLE
FUNCTION_REQUEST_MULTI: int = gen.FN_REQUEST_MULTI
FUNCTION_REQUEST_STRING: int = gen.FN_REQUEST_STRING
FUNCTION_REQUEST_RENDERED_STRING: int = gen.FN_REQUEST_RENDERED_STRING
FUNCTION_BEACON: int = gen.FN_BEACON

#: Minimum length of a Kemper SysEx that carries the fixed 10-byte header.
_HEADER_LEN = 10


def u14(msb: int, lsb: int) -> int:
    """Combine an MSB/LSB pair of 7-bit bytes into a 14-bit value (0–16383)."""
    return ((msb & 0x7F) << 7) | (lsb & 0x7F)


def u14_split(value: int) -> tuple[int, int]:
    """Split a 14-bit value into ``(MSB, LSB)`` 7-bit bytes."""
    return (value >> 7) & 0x7F, value & 0x7F


def sysex(
    product: int,
    device: int,
    function: int,
    page: int,
    number: int,
    values: bytes | bytearray | list[int] = b"",
) -> bytes:
    """Build a Kemper SysEx message with the standard header.

    ``F0 00 20 33 <product> <device> <function> <instance=0> <page> <number>
    <values…> F7``

    Every header byte is a 7-bit SysEx data byte. An out-of-range one raises
    :class:`ValueError` rather than being masked: masking would silently
    retarget the message at a different address (``page=0x80`` would become
    page 0, the string/morph page) instead of surfacing the mistake.
    """
    header = (product, device, function, page, number)
    if any(not 0 <= byte <= 0x7F for byte in header):
        names = ("product", "device", "function", "page", "number")
        bad = ", ".join(
            f"{n}={v}" for n, v in zip(names, header, strict=True) if not 0 <= v <= 0x7F
        )
        raise ValueError(f"SysEx header bytes must be 7-bit (0-127): {bad}")
    out = bytearray([0xF0])
    out.extend(MANUFACTURER_ID)
    out.extend([product, device, function, 0x00, page, number])
    out.extend(bytes(values))
    out.append(0xF7)
    return bytes(out)


def beacon_flags(init: bool, tuner: bool) -> int:
    """Beacon flag byte: bit0 init, bit1 sysex, bit2 echo, bit3 nofe, bit4 noctr,
    bit5 tunemode."""
    flags = gen.BEACON_FLAG_SYSEX  # sysex — always on
    if init:
        flags |= gen.BEACON_FLAG_INIT
    if tuner:
        flags |= gen.BEACON_FLAG_TUNEMODE
    return flags


def beacon(
    init: bool,
    tuner: bool,
    lease_secs: int,
    param_set: int = gen.BEACON_DEFAULT_PARAM_SET,
    product: int = PRODUCT_PROFILER,
) -> bytes:
    """Build the bidirectional beacon SysEx (raw MIDI, ``F0…F7``).

    ``init`` is set on the first beacon of a session, ``tuner`` requests tuner
    data in the stream, and ``lease_secs`` is the keep-alive lease (encoded in
    2-second steps; re-send within half of it).
    """
    out = bytearray([0xF0])
    out.extend(MANUFACTURER_ID)
    out.extend(
        [
            product & 0x7F,
            DEVICE_OMNI,
            gen.BEACON_FUNCTION,
            0x00,  # instance
            gen.BEACON_SUBCOMMAND,
            param_set & 0x7F,
            beacon_flags(init, tuner),
            (lease_secs // 2) & 0x7F,
        ]
    )
    out.append(0xF7)
    return bytes(out)


def request_string(product: int, device: int, page: int, number: int) -> bytes:
    """Request a string parameter (function ``$43``); the device replies with ``$03``.

    Read-only — it does not change device state.
    """
    return sysex(product, device, FUNCTION_REQUEST_STRING, page, number)


def request_single(product: int, device: int, page: int, number: int) -> bytes:
    """Request a single numeric parameter (function ``$41``); reply arrives as ``$01``.

    Read-only — a nonexistent address is silently ignored by the device.
    """
    return sysex(product, device, FUNCTION_REQUEST_SINGLE, page, number)


def request_multi(product: int, device: int, page: int, number: int) -> bytes:
    """Request all numeric parameters of a unit (function ``$42``).

    The reply arrives as a ``$02`` Multi Parameter Change; decode its value block
    with :func:`multi_values`. The request must address the *first* controller
    number of the unit or the device ignores it.
    """
    return sysex(product, device, FUNCTION_REQUEST_MULTI, page, number)


def set_single(product: int, device: int, page: int, number: int, value: int) -> bytes:
    """Set a single numeric parameter (function ``$01``, Single Parameter Change).

    **Mutating.** ``value`` is 14-bit; for a switch parameter use 1 (on) / 0 (off).
    """
    msb, lsb = u14_split(value)
    return sysex(product, device, FUNCTION_SINGLE_PARAM, page, number, bytes([msb, lsb]))


def request_rendered_string(product: int, device: int, page: int, number: int, value: int) -> bytes:
    """Request a parameter value rendered to a string (function ``$7C``).

    The device replies with a ``$3C`` message carrying the rendered ASCII (e.g.
    ``<0.0>``). Read-only, but costly in device CPU. Layout:
    ``<fn=7C> <flags=00> <page> <number> <valMSB> <valLSB>`` — the flags byte
    occupies the instance slot of the standard header.
    """
    msb, lsb = u14_split(value)
    return sysex(product, device, FUNCTION_REQUEST_RENDERED_STRING, page, number, bytes([msb, lsb]))


def control_change(channel: int, controller: int, value: int) -> bytes:
    """A 3-byte MIDI Control Change on ``channel`` (0–15)."""
    return bytes([gen.CONTROL_CHANGE_STATUS | (channel & 0x0F), controller & 0x7F, value & 0x7F])


def program_change(channel: int, program: int) -> bytes:
    """A 2-byte MIDI Program Change on ``channel`` (0–15): ``[0xC0|ch, program]``."""
    return bytes([gen.PROGRAM_CHANGE_STATUS | (channel & 0x0F), program & 0x7F])


@dataclass(frozen=True, slots=True)
class NrpnHeader:
    """A parsed Kemper NRPN/SysEx message header."""

    product: int
    device: int
    function: int
    instance: int
    page: int
    number: int

    @classmethod
    def parse(cls, msg: bytes) -> tuple[NrpnHeader, bytes] | None:
        """Parse the fixed header of a Kemper SysEx.

        Returns ``(header, value_bytes)``, where the value bytes are everything
        between the header and the trailing ``0xF7``, or ``None`` if ``msg`` is
        not a Kemper SysEx.
        """
        if len(msg) < 11 or msg[0] != 0xF0 or msg[1:4] != MANUFACTURER_ID:
            return None
        header = cls(
            product=msg[4],
            device=msg[5],
            function=msg[6],
            instance=msg[7],
            page=msg[8],
            number=msg[9],
        )
        return header, bytes(msg[_HEADER_LEN : len(msg) - 1])


def multi_values(number: int, vals: bytes) -> list[tuple[int, int]]:
    """Decode a ``$02`` Multi Parameter Change value block.

    The values apply to **consecutive addresses** starting at ``number``, each a
    14-bit MSB/LSB pair. Returns ``(number + i, value)`` for each pair; a
    trailing odd byte is ignored.
    """
    out: list[tuple[int, int]] = []
    for i in range(len(vals) // 2):
        pair = vals[2 * i : 2 * i + 2]
        out.append(((number + i) & 0xFF, u14(pair[0], pair[1])))
    return out


def ext_decode(data: bytes) -> int:
    """Decode the "extended" encoding: big-endian, 7 data bits per byte.

    Works on any slice length; a 5-byte input yields a 35-bit value. This is the
    shared decode for function ``$06`` (ext-param) and ``$07`` (ext-string)
    addresses and values.
    """
    acc = 0
    for b in data:
        acc = (acc << 7) | (b & 0x7F)
    return acc


def ext_encode(value: int, n: int) -> bytes:
    """Inverse of :func:`ext_decode`: encode ``value`` big-endian into ``n`` bytes."""
    return bytes((value >> (7 * (n - 1 - i))) & 0x7F for i in range(n))


def _ascii_until_nul(data: bytes) -> str:
    end = data.find(0)
    if end >= 0:
        data = data[:end]
    return "".join(chr(b) for b in data)


def parse_extended_string(msg: bytes) -> tuple[int, str] | None:
    """Parse a function-``$07`` Extended String Parameter message.

    ``F0 00 20 33 <prod> <dev> 07 <inst> <5-byte address> <ascii…> 00 F7``.
    Returns ``(address, text)``, where the address decodes via the 5×7 extended
    scheme (:func:`ext_decode`) and the text runs up to the first NUL.

    For a normal page the address equals ``page * 128 + number``, so an extended
    string on page 0 carries the same string-tag numbers as function ``$03``.
    """
    if (
        len(msg) < 14
        or msg[0] != 0xF0
        or msg[1:4] != MANUFACTURER_ID
        or msg[6] != FUNCTION_EXT_STRING_PARAM
        or msg[-1] != 0xF7
    ):
        return None
    address = ext_decode(msg[8:13])
    return address, _ascii_until_nul(msg[13 : len(msg) - 1])


def parse_rendered_string(msg: bytes) -> tuple[int, int, int, str] | None:
    """Parse a ``$3C`` Rendered String reply — the response to
    :func:`request_rendered_string`.

    The reply mirrors the ``$7C`` request header, then carries the value pair and
    the rendered ASCII::

        <fn=3C> <flags> <page> <number> <valMSB> <valLSB> <ascii…> 00

    Returns ``(page, number, value, text)``, or ``None`` if it isn't a ``$3C``
    reply or lacks the value pair.
    """
    parsed = NrpnHeader.parse(msg)
    if parsed is None:
        return None
    header, vals = parsed
    if header.function != FUNCTION_RENDERED_STRING_REPLY or len(vals) < 2:
        return None
    return header.page, header.number, u14(vals[0], vals[1]), _ascii_until_nul(vals[2:])
