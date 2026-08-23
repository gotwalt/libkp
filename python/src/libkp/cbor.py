"""The device's native CBOR control channel and its state-dump snapshot.

The ``{774CDB9E-…}`` protocol GUID (:data:`libkp.session.PROTOCOL_CBOR_CONTROL`)
completes the same :mod:`handshake <libkp.session>` and 8-byte preamble as the
MIDI3 stream, then speaks **CBOR** (RFC 8949) rather than MIDI3 frames — a
continuous sequence of bare items with no outer framing (see ``docs/06``). This
module is the reader and writer for that channel:

- Decoded items are plain Python objects (``int``, ``bytes``, ``str``, ``list``)
  plus a small :class:`Tag` for CBOR tags; :class:`Decoder` is a streaming reader
  that buffers partial items across TCP chunk boundaries and tolerates the
  inter-item filler the channel emits.
- :func:`encode` / :func:`to_vec` and :func:`param_write` write the one item this
  library sends — the write that asks the device for its full state.
- :func:`extract_snapshot` reads the device's current bank and rig slot out of
  the resulting dump, and :func:`fetch_state_snapshot` runs the whole exchange
  over a fresh session.

The channel does not volunteer state: a passive session sees only live change
events. Writing ``param_write(STATE_DUMP_TRIGGER_ADDRESS, 1)`` asks for the whole
parameter state, which arrives as a burst of multi-parameter and string items
carrying — among much else — the current bank
(:data:`libkp._generated.CURRENT_BANK_ADDRESS`) and rig slot
(:data:`libkp._generated.CURRENT_RIG_SLOT_ADDRESS`), both 0-based. The write is
non-mutating.
"""

from __future__ import annotations

import asyncio
import contextlib
import struct
from collections import deque
from dataclasses import dataclass, field

from . import _generated as gen
from ._broadcast import Broadcast
from .errors import ConnectionClosedError, SessionError
from .protocol import PORT
from .session import PROTOCOL_CBOR_CONTROL, Session
from .state import DeviceState

__all__ = [
    "Tag",
    "Decoder",
    "encode",
    "to_vec",
    "param_write",
    "state_dump_request",
    "is_sensitive",
    "StateSnapshot",
    "extract_snapshot",
    "numeric_values",
    "fetch_state_snapshot",
    "CborSession",
]

#: Nesting limit — guards the recursive descent against hostile/desynced input.
_MAX_DEPTH = 32

#: Read idle gap driving the snapshot read loop; short so it reacts per packet.
_READ_IDLE = 0.03


@dataclass(frozen=True, slots=True)
class Tag:
    """A CBOR tag: a tag number wrapping one inner value."""

    tag: int
    value: object


class _Undefined:
    """The CBOR ``undefined`` simple value (major type 7, value 23)."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "undefined"


class _Break:
    """The ``0xFF`` break stop-code; only valid inside an indefinite-length item."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "break"


@dataclass(frozen=True, slots=True)
class Simple:
    """A CBOR simple value (major type 7) other than bool/null/undefined."""

    value: int


UNDEFINED = _Undefined()
BREAK = _Break()


class _Incomplete(Exception):
    """Ran out of bytes mid-item — wait for more of the stream."""


class _Invalid(Exception):
    """Not valid CBOR (reserved additional-info, bad UTF-8, nesting too deep)."""


def _as_int(v: object) -> int | None:
    """The item as an integer, for the ``[selector, addr, value]`` shapes.

    ``bool`` is excluded — it is a distinct CBOR type here even though Python
    makes it a subclass of ``int``.
    """
    if isinstance(v, bool):
        return None
    return v if isinstance(v, int) else None


def _as_array(v: object) -> list | None:
    """The elements, if ``v`` is an array (unwrapping one layer of tag first)."""
    if isinstance(v, list):
        return v
    if isinstance(v, Tag):
        return _as_array(v.value)
    return None


def _is_text_head(b: int) -> bool:
    """Heads that open a text string (major type 3), definite or indefinite."""
    return b >> 5 == 3


class Decoder:
    """A streaming CBOR reader: feed it TCP chunks, take whole items back.

    The channel pads between top-level items with runs of the filler byte
    :data:`libkp._generated.CBOR_FILLER_BYTE` (``0xC0``) — a ``tag(0)`` head with
    no content, which is not well-formed CBOR on its own. Parsed naively it
    swallows the following item, so the decoder skips it. A genuine ``tag(0)`` is
    an RFC 8949 date/time whose content must be a text string, so a ``0xC0`` is
    treated as filler only when the next head is *not* a text head, leaving real
    datetimes intact.
    """

    __slots__ = ("_buf", "_filler")

    def __init__(self) -> None:
        self._buf = bytearray()
        self._filler = 0

    def push(self, data: bytes) -> list:
        """Feed raw stream bytes; return every item completed by this input.

        A byte that cannot start a valid item is dropped to resync (the same
        strategy :class:`libkp.midi3.Unframer` uses), so a mid-stream join or a
        wrong-protocol guess degrades to noise rather than a permanent stall.
        """
        self._buf.extend(data)
        out: list = []
        off = 0
        while True:
            # ``_skip_filler`` returning None means a trailing filler byte we
            # cannot classify until more bytes arrive — stop and keep it buffered.
            nxt = self._skip_filler(off)
            if nxt is None:
                break
            off = nxt
            try:
                value, used = _parse_item(self._buf, off, 0)
            except _Incomplete:
                break
            except _Invalid:
                off += 1  # resync
                continue
            off += used
            out.append(value)
        del self._buf[:off]
        return out

    def pending(self) -> int:
        """Bytes buffered but not yet forming a complete item."""
        return len(self._buf)

    def filler_bytes(self) -> int:
        """How many filler bytes have been skipped over this stream's life."""
        return self._filler

    def _skip_filler(self, off: int) -> int | None:
        """Step ``off`` past any inter-item filler. ``None`` means the buffer ends
        on a filler byte whose role cannot be decided until the next byte arrives.
        """
        while off < len(self._buf) and self._buf[off] == gen.CBOR_FILLER_BYTE:
            if off + 1 >= len(self._buf):
                return None
            if _is_text_head(self._buf[off + 1]):
                return off  # real datetime tag
            off += 1
            self._filler += 1
        return off


def _parse_item(b: bytes, off: int, depth: int) -> tuple[object, int]:
    """Parse one item from ``b`` at ``off``. Returns the item and bytes consumed."""
    if depth > _MAX_DEPTH:
        raise _Invalid
    if off >= len(b):
        raise _Incomplete
    head = b[off]
    major = head >> 5
    ai = head & 0x1F

    # Additional info 31 = indefinite length (or the break stop-code).
    if ai == 31:
        if major == 2:
            return _parse_indefinite_chunks(b, off, depth, bytes_mode=True)
        if major == 3:
            return _parse_indefinite_chunks(b, off, depth, bytes_mode=False)
        if major == 4:
            return _parse_indefinite_array(b, off, depth)
        if major == 5:
            return _parse_indefinite_map(b, off, depth)
        if major == 7:
            return BREAK, 1
        raise _Invalid

    arg, head_len = _parse_argument(b, off, ai)
    rest = off + head_len

    if major == 0:
        return arg, head_len
    if major == 1:
        return -1 - arg, head_len
    if major in (2, 3):
        if rest + arg > len(b):
            raise _Incomplete
        raw = bytes(b[rest : rest + arg])
        if major == 2:
            return raw, head_len + arg
        try:
            return raw.decode("utf-8"), head_len + arg
        except UnicodeDecodeError as exc:
            raise _Invalid from exc
    if major == 4:
        items: list = []
        pos = rest
        for _ in range(arg):
            v, used = _parse_item(b, pos, depth + 1)
            pos += used
            items.append(v)
        return items, pos - off
    if major == 5:
        pairs: list = []
        pos = rest
        for _ in range(arg):
            k, used = _parse_item(b, pos, depth + 1)
            pos += used
            v, used = _parse_item(b, pos, depth + 1)
            pos += used
            pairs.append((k, v))
        return {"map": pairs}, pos - off
    if major == 6:
        inner, used = _parse_item(b, rest, depth + 1)
        return Tag(arg, inner), head_len + used
    if major == 7:
        return _parse_simple(ai, arg, head_len)
    raise _Invalid


def _parse_argument(b: bytes, off: int, ai: int) -> tuple[int, int]:
    """Read the head byte's argument. Returns ``(value, total head length)``."""
    if ai <= 23:
        return ai, 1
    width = {24: 1, 25: 2, 26: 4, 27: 8}.get(ai)
    if width is None:  # 28..=30 are reserved
        raise _Invalid
    if off + 1 + width > len(b):
        raise _Incomplete
    arg = 0
    for x in b[off + 1 : off + 1 + width]:
        arg = (arg << 8) | x
    return arg, 1 + width


def _parse_simple(ai: int, arg: int, head_len: int) -> tuple[object, int]:
    """Major type 7: booleans, null, undefined, simple values and floats."""
    if ai == 20:
        return False, head_len
    if ai == 21:
        return True, head_len
    if ai == 22:
        return None, head_len
    if ai == 23:
        return UNDEFINED, head_len
    if ai <= 19 or ai == 24:
        return Simple(arg), head_len
    if ai == 25:
        return _f16_to_float(arg), head_len
    if ai == 26:
        return struct.unpack(">f", struct.pack(">I", arg))[0], head_len
    if ai == 27:
        return struct.unpack(">d", struct.pack(">Q", arg))[0], head_len
    raise _Invalid


def _parse_indefinite_chunks(
    b: bytes, off: int, depth: int, *, bytes_mode: bool
) -> tuple[object, int]:
    """Indefinite-length byte/text string: definite chunks until the break code."""
    pos = off + 1
    acc = bytearray()
    while True:
        if pos >= len(b):
            raise _Incomplete
        if b[pos] == 0xFF:
            pos += 1
            break
        chunk, used = _parse_item(b, pos, depth + 1)
        if bytes_mode and isinstance(chunk, bytes):
            acc.extend(chunk)
        elif not bytes_mode and isinstance(chunk, str):
            acc.extend(chunk.encode("utf-8"))
        else:
            raise _Invalid  # chunks must match the outer type
        pos += used
    if bytes_mode:
        return bytes(acc), pos - off
    try:
        return acc.decode("utf-8"), pos - off
    except UnicodeDecodeError as exc:
        raise _Invalid from exc


def _parse_indefinite_array(b: bytes, off: int, depth: int) -> tuple[object, int]:
    pos = off + 1
    items: list = []
    while True:
        if pos >= len(b):
            raise _Incomplete
        if b[pos] == 0xFF:
            pos += 1
            break
        v, used = _parse_item(b, pos, depth + 1)
        pos += used
        items.append(v)
    return items, pos - off


def _parse_indefinite_map(b: bytes, off: int, depth: int) -> tuple[object, int]:
    pos = off + 1
    pairs: list = []
    while True:
        if pos >= len(b):
            raise _Incomplete
        if b[pos] == 0xFF:
            pos += 1
            break
        k, used = _parse_item(b, pos, depth + 1)
        pos += used
        v, used = _parse_item(b, pos, depth + 1)
        pos += used
        pairs.append((k, v))
    return {"map": pairs}, pos - off


def _f16_to_float(bits: int) -> float:
    """IEEE-754 half precision → float (CBOR additional info 25)."""
    sign = -1.0 if bits & 0x8000 else 1.0
    exp = (bits >> 10) & 0x1F
    frac = float(bits & 0x03FF)
    if exp == 0:
        mag = frac * 2.0**-24  # subnormal
    elif exp == 31:
        mag = float("inf") if frac == 0.0 else float("nan")
    else:
        mag = (frac / 1024.0 + 1.0) * 2.0 ** (exp - 15)
    return sign * mag


# --------------------------------------------------------------------------
# Encoding
# --------------------------------------------------------------------------


def _write_head(major: int, arg: int, out: bytearray) -> None:
    """Write a CBOR head: the 3-bit major type plus the shortest argument encoding
    that fits, which is what the device itself emits."""
    m = major << 5
    if arg <= 23:
        out.append(m | arg)
    elif arg <= 0xFF:
        out.append(m | 24)
        out.append(arg)
    elif arg <= 0xFFFF:
        out.append(m | 25)
        out.extend(struct.pack(">H", arg))
    elif arg <= 0xFFFF_FFFF:
        out.append(m | 26)
        out.extend(struct.pack(">I", arg))
    else:
        out.append(m | 27)
        out.extend(struct.pack(">Q", arg))


def encode(v: object, out: bytearray) -> None:
    """Encode a value as CBOR, appending to ``out``.

    :data:`BREAK` encodes as the bare ``0xFF`` stop code, which is only
    well-formed inside an indefinite-length item; this module never produces one,
    so encoding a stray ``BREAK`` yields bytes the device would reject.
    """
    if isinstance(v, bool):
        out.append(0xF5 if v else 0xF4)
    elif isinstance(v, int):
        if v < 0:
            _write_head(1, -1 - v, out)  # major 1 encodes -1-n as the argument n
        else:
            _write_head(0, v, out)
    elif isinstance(v, (bytes, bytearray)):
        _write_head(2, len(v), out)
        out.extend(v)
    elif isinstance(v, str):
        raw = v.encode("utf-8")
        _write_head(3, len(raw), out)
        out.extend(raw)
    elif isinstance(v, list):
        _write_head(4, len(v), out)
        for item in v:
            encode(item, out)
    elif isinstance(v, dict) and "map" in v:
        pairs = v["map"]
        _write_head(5, len(pairs), out)
        for k, val in pairs:
            encode(k, out)
            encode(val, out)
    elif isinstance(v, Tag):
        _write_head(6, v.tag, out)
        encode(v.value, out)
    elif v is None:
        out.append(0xF6)
    elif isinstance(v, _Undefined):
        out.append(0xF7)
    elif isinstance(v, Simple):
        _write_head(7, v.value, out)
    elif isinstance(v, float):
        out.append(0xFB)
        out.extend(struct.pack(">d", v))
    elif isinstance(v, _Break):
        out.append(0xFF)
    else:  # pragma: no cover - defensive
        raise TypeError(f"cannot encode {type(v)!r} as CBOR")


def to_vec(v: object) -> bytes:
    """Encode a value as a fresh ``bytes`` object."""
    out = bytearray()
    encode(v, out)
    return bytes(out)


def param_write(addr: int, value: int) -> Tag:
    """Build a single-parameter write, ``tag(1)([1, addr, value])`` — the shape the
    channel uses to set a parameter (``docs/06``).

    This is a **write**: the device applies it and rebroadcasts it to every other
    open session. The one this library sends — ``param_write(
    STATE_DUMP_TRIGGER_ADDRESS, 1)`` — is non-mutating and asks the device for its
    full state.
    """
    return Tag(gen.CBOR_ITEM_TAG, [gen.CBOR_SELECTOR_SINGLE, addr, value])


def state_dump_request() -> Tag:
    """The item that asks the device for its full parameter state."""
    return param_write(gen.STATE_DUMP_TRIGGER_ADDRESS, gen.STATE_DUMP_TRIGGER_VALUE)


# --------------------------------------------------------------------------
# Snapshot
# --------------------------------------------------------------------------


def is_sensitive(addr: int) -> bool:
    """Is ``addr`` one whose string value is a device secret (WiFi credentials)?

    The state dump volunteers these in the clear; a reader must never surface
    them. See :data:`libkp._generated.SENSITIVE_ADDRESSES`.
    """
    return addr in gen.SENSITIVE_ADDRESSES


@dataclass(slots=True)
class StateSnapshot:
    """The device's current position and the values carried alongside it, read
    out of a state dump. Both indices are 0-based; any field is ``None`` if the
    dump did not carry it."""

    #: Current bank, 0-based (:data:`libkp._generated.CURRENT_BANK_ADDRESS`).
    current_bank: int | None = None
    #: Current rig slot within the bank, 0-based
    #: (:data:`libkp._generated.CURRENT_RIG_SLOT_ADDRESS`).
    current_rig_slot: int | None = None
    #: The morph position (0 = base, 16383 = fully morphed), at
    #: :data:`libkp._generated.MORPH_ADDRESS`.
    #:
    #: This dump is the only way a client that is not holding a CBOR session open
    #: can learn it: the position is never sent on the MIDI3 stream, and answers
    #: neither a ``$41`` nor a ``$46`` request. It is a live value, so it is true
    #: as of the read and stale the moment anyone morphs.
    morph: int | None = None
    #: String parameters the dump carried, as ``(address, text)`` in document
    #: order, with any sensitive value redacted. Useful for the current rig name
    #: (address 1) and the bank name.
    strings: list[tuple[int, str]] = field(default_factory=list)

    def is_complete(self) -> bool:
        """True once every value this snapshot reads is known -- the point at
        which the reader may stop before the dump has finished streaming.

        The morph counts: it arrives later in the dump than the two indices, so
        stopping at those would truncate the read just short of it and leave
        :attr:`morph` ``None`` on a device that reported it perfectly well. Every
        dump observed carries all three, at base as readily as morphed.
        """
        return (
            self.current_bank is not None
            and self.current_rig_slot is not None
            and self.morph is not None
        )

    def string(self, addr: int) -> str | None:
        """The string parameter at ``addr``, if the dump carried one."""
        for a, text in self.strings:
            if a == addr:
                return text
        return None


def extract_snapshot(items: list) -> StateSnapshot:
    """Read the current bank, rig slot, morph and string parameters out of decoded
    dump items.

    The items fold into a scratch :class:`~libkp.state.DeviceState` through the
    same entry points a live session uses, so the dump is read by exactly the
    routing the tree is held to: the position rows' range checks, the morph's,
    and nothing hand-written here. The strings are the one thing the tree cannot
    answer -- the dump names addresses the tree has no field for, the bank name
    among them -- so they are kept as the walk found them, in document order,
    with any secret redacted before it can be seen.
    """
    scratch = DeviceState()
    snap = StateSnapshot()

    def numeric(addr: int, value: int) -> None:
        scratch.apply_cbor(addr, value)

    def text(addr: int, value: str) -> None:
        scratch.apply_cbor_text(addr, value)
        snap.strings.append((addr, gen.REDACTED_PLACEHOLDER if is_sensitive(addr) else value))

    _walk(items, numeric, text)
    snap.current_bank = scratch.current_bank
    snap.current_rig_slot = scratch.current_rig_slot
    snap.morph = scratch.morph
    return snap


def numeric_values(items: list) -> list[tuple[int, int]]:
    """Every numeric ``(address, value)`` pair the items carry, in document order.

    The dump and a session's live pushes are the same shapes, so this is what a
    :class:`CborSession` folds into a state tree as values move. An address that
    does not fit 32 bits, or a value that does not fit a signed 64-bit word, is
    malformed rather than a pair to wrap into a plausible-looking one, and is
    skipped -- the same bound every implementation applies.
    """
    out: list[tuple[int, int]] = []

    def numeric(addr: int, value: int) -> None:
        if 0 <= addr <= _U32_MAX and _I64_MIN <= value <= _I64_MAX:
            out.append((addr, value))

    _walk(items, numeric, lambda addr, value: None)
    return out


#: The widest address and value a numeric pair may carry: CBOR integers are
#: unbounded in Python, but the channel's addresses are 32-bit and its values
#: signed 64-bit, and the other implementations reject anything wider.
_U32_MAX = 0xFFFF_FFFF
_I64_MIN = -(1 << 63)
_I64_MAX = (1 << 63) - 1


def _walk(items: list, numeric, text) -> None:
    """Walk decoded items, handing each numeric and string element to a callback.

    Scans the value-bearing shapes: a single ``[1, addr, value]``, a
    consecutive-run ``[2, base, v0, v1, …]`` where the whole run is walked because
    the target address can fall anywhere inside it (at session open the position
    arrives inside one run), and a string ``[4, addr, text]``. A leading negative
    source-flag word, if present, is skipped exactly as :func:`param_write` never
    emits one.
    """
    for item in items:
        fields = _as_array(item)
        if fields is None:
            continue
        # Skip a leading negative source-flags word.
        first = _as_int(fields[0]) if fields else None
        rest = fields[1:] if first is not None and first < 0 else fields
        selector = _as_int(rest[0]) if rest else None
        if selector is None:
            continue
        addr = _as_int(rest[1]) if len(rest) > 1 else None
        if selector == gen.CBOR_SELECTOR_SINGLE:
            value = _as_int(rest[2]) if len(rest) > 2 else None
            if addr is not None and value is not None:
                numeric(addr, value)
        elif selector == gen.CBOR_SELECTOR_MULTI:
            if addr is not None:
                for i, raw in enumerate(rest[2:]):
                    v = _as_int(raw)
                    if v is not None:
                        numeric(addr + i, v)
        elif selector == gen.CBOR_SELECTOR_STRING:
            value = rest[2] if len(rest) > 2 else None
            if addr is not None and isinstance(value, str) and value:
                text(addr, value)


#: Default time to keep reading the dump before giving up on the indices.
DEFAULT_TIMEOUT = 3.0


async def fetch_state_snapshot(
    ip: str, *, port: int = PORT, timeout: float = DEFAULT_TIMEOUT
) -> StateSnapshot:
    """Open a fresh CBOR session to ``ip``, trigger the state dump, and read back
    the current bank, rig slot and morph position.

    This opens its **own** short-lived connection, independent of any
    :class:`libkp.model.DeviceModel`, and closes it on return. The device crashes
    under connection churn, so run this sparingly. Returns as soon as every value
    it reads is known (see :meth:`StateSnapshot.is_complete`) or ``timeout``
    elapses.

    For a *live* view, and to run alongside a streaming session rather than
    before it, use :class:`CborSession`.
    """
    session = await Session.connect(ip, port)
    try:
        outcome = await session.handshake([PROTOCOL_CBOR_CONTROL], _READ_IDLE)
        await session.write_session_preamble()

        decoder = Decoder()
        items = decoder.push(outcome.response_tail())

        # Ask for the full state, then read until both indices land or the
        # deadline passes.
        await session.write_all(to_vec(state_dump_request()))

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not extract_snapshot(items).is_complete():
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                chunk = await session.read_once(min(_READ_IDLE, remaining), 64 * 1024)
            except ConnectionClosedError:
                break
            if chunk:
                items.extend(decoder.push(chunk))
        return extract_snapshot(items)
    finally:
        await session.close()


class CborSession:
    """A **live** session on the native CBOR channel: it opens, asks for the state
    dump, and then streams every value the device pushes until it is closed.

    This is the counterpart to :func:`fetch_state_snapshot`, which reads one dump
    and hangs up. Hold this open instead when you want to *watch* the values the
    MIDI3 stream never carries -- the morph position above all, which is pushed
    here at about 40 Hz while a morph ramps and is unreachable by any request on
    the streaming session.

    **It may run alongside a** :class:`~libkp.model.DeviceModel`. The device's
    fragility is about connection *churn*, not concurrency: two read-only sessions
    coexist happily, and the pair is how a client gets both the meter lane and the
    morph. Space the two connections by
    :data:`~libkp.session.CONNECTION_COOLDOWN` when opening them, and do not
    reopen either in a tight loop.

    Read-only. The one thing it writes is the state-dump trigger, which is a flag
    the device already carries -- see ``docs/06``.
    """

    #: How long a read waits before looping. Short, so :meth:`close` takes effect
    #: promptly.
    READ_IDLE = 0.25
    #: How many pre-subscription values to hold. The state dump is a couple of
    #: thousand; this keeps the most recent of them rather than growing without
    #: bound if nobody ever subscribes.
    BACKLOG_LIMIT = 4096

    def __init__(self, session: Session) -> None:
        self._session = session
        self._decoder = Decoder()
        self._updates = Broadcast()
        self._task: asyncio.Task | None = None
        self._closed = False
        #: Values decoded before anyone subscribed, replayed to the first
        #: subscriber. :meth:`connect` returns only after the dump has been asked
        #: for, so the opening burst can land before the caller has had a chance
        #: to call :meth:`updates` -- and that burst is the only place several
        #: values, the morph among them, appear until something moves them.
        self._backlog: deque[tuple[int, int]] = deque(maxlen=self.BACKLOG_LIMIT)

    @classmethod
    async def connect(cls, ip: str, port: int = PORT) -> CborSession:
        """Connect to ``ip:port``, open the CBOR protocol, ask for the state dump,
        and start streaming.

        Returns once the session is established; values arrive on
        :meth:`updates`. Subscribe *before* awaiting them, or the dump's own burst
        is missed.
        """
        session = await Session.connect(ip, port)
        # Everything up to the spawned task is inside the guard: a socket left
        # open with no owner is exactly the churn this device dies of.
        try:
            outcome = await session.handshake([PROTOCOL_CBOR_CONTROL], _READ_IDLE)
            await session.write_session_preamble()
            self = cls(session)
            self._emit(self._decoder.push(outcome.response_tail()))
            # Writing one item asks for the whole state; the reply is the burst
            # the stream opens with.
            await session.write_all(to_vec(state_dump_request()))
            loop = asyncio.get_running_loop()
            self._task = loop.create_task(self._ingest())
            return self
        except BaseException:
            await session.close()
            raise

    def updates(self) -> asyncio.Queue:
        """A queue of ``(address, value)`` pairs, in arrival order.

        The first subscriber also receives whatever arrived before it subscribed,
        so the state dump is not lost to the gap between :meth:`connect` and this
        call.
        """
        queue = self._updates.subscribe()
        while self._backlog:
            queue.put_nowait(self._backlog.popleft())
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """Drop a queue returned by :meth:`updates`."""
        self._updates.unsubscribe(queue)

    async def close(self) -> None:
        """Close the socket and finish the stream."""
        if self._closed:
            return
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self._session.close()

    async def __aenter__(self) -> CborSession:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def _ingest(self) -> None:
        try:
            while True:
                try:
                    chunk = await self._session.read_once(self.READ_IDLE, 64 * 1024)
                except SessionError:
                    return
                if chunk:
                    self._emit(self._decoder.push(chunk))
        except asyncio.CancelledError:
            raise

    def _emit(self, items: list) -> None:
        for pair in numeric_values(items):
            if self._updates.empty():
                self._backlog.append(pair)
            else:
                self._updates.send(pair)
