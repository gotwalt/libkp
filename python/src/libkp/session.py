"""TCP session with a Profiler and the line-based protocol handshake.

Sequence (documented from observed experimentation):

1. TCP connect to the device on :data:`libkp.protocol.PORT` (5727).
2. The **device sends first**: a list of supported protocol GUIDs, one per
   CRLF-terminated line, ending with a line containing just ``"."``.
3. The client writes the chosen protocol name followed by ``"\\r\\n"``.
4. The device replies with a line beginning ``+`` (accept) or ``-`` (reject).
5. For the streaming protocol the client then writes an 8-byte zero preamble and
   the framed stream begins.

The two device replies in that sequence -- the greeting and the answer to the
selection -- are each read at two speeds. A fresh device greets within a few
milliseconds, but one that has served a few sessions has been measured taking
most of a second (777 ms on one occasion) before its first greeting byte, so
that first byte is waited for up to :data:`HANDSHAKE_TIMEOUT`. Once a reply has
begun, its lines arrive back to back, and the short inter-chunk ``idle`` gap
callers pass is enough to know it has ended.

Connection spacing is enforced here, not by callers. The device tolerates
concurrent sessions but not connection *churn*: it refuses to greet, or resets,
a session opened too soon after another socket to it was opened or closed
(``docs/06``, ``docs/11``). A module-level ledger remembers, per ``(ip, port)``,
when a socket to that peer was last opened and last closed, and
:meth:`Session.connect` waits out :data:`CONNECTION_COOLDOWN` from the later of
the two before dialing. Every path that opens a socket -- the model, the CBOR
session, the one-shot snapshot fetch, a test against the real port -- goes
through it, so none of them can churn the device by accident.
"""

from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass, field

from . import _generated as gen
from .errors import (
    ConnectError,
    ConnectionClosedError,
    ProtocolRejectedError,
    SessionError,
    TimeoutErrorLibKP,
)
from .protocol import PORT

__all__ = [
    "PROTOCOL_MIDI3_STREAM",
    "PROTOCOL_REQUEST_RESPONSE",
    "PROTOCOL_CBOR_CONTROL",
    "PROTOCOL_RESERVED",
    "HANDSHAKE_TERMINATOR",
    "SESSION_PREAMBLE",
    "CONNECTION_COOLDOWN",
    "HANDSHAKE_TIMEOUT",
    "HandshakeOutcome",
    "Session",
    "parse_protocol_list",
]

#: The protocol GUID that streams live MIDI data (meters, params, tuner).
PROTOCOL_MIDI3_STREAM: str = gen.PROTOCOL_MIDI3_STREAM
#: Request/response protocol GUID: accepts the handshake but pushes nothing.
PROTOCOL_REQUEST_RESPONSE: str = gen.PROTOCOL_REQUEST_RESPONSE
#: The device's native CBOR control channel — the state-dump snapshot route
#: (:mod:`libkp.cbor`).
PROTOCOL_CBOR_CONTROL: str = gen.PROTOCOL_CBOR_CONTROL
#: Offered by the device but rejects the handshake.
PROTOCOL_RESERVED: str = gen.PROTOCOL_RESERVED

#: Line terminator the client appends to the chosen protocol name.
HANDSHAKE_TERMINATOR: bytes = gen.HANDSHAKE_TERMINATOR.encode("ascii")

#: Zero bytes the client writes to open the streaming session.
SESSION_PREAMBLE: bytes = b"\x00" * gen.SESSION_PREAMBLE_LEN

#: Default connect timeout.
CONNECT_TIMEOUT: float = float(gen.CONNECT_TIMEOUT_SECS)

#: How long, in seconds, :meth:`Session.handshake` waits for the *first* byte
#: of the greeting, and :meth:`Session.select_protocol` for the first byte of
#: the device's answer. This is a different wait from the ``idle`` gap those
#: methods also take: the gap only decides when a reply that has started is
#: over, while this bounds how long the device may take to start one. A device
#: that has served a few sessions can sit for most of a second before greeting,
#: far longer than any inter-chunk gap, and connecting must not fail on that.
HANDSHAKE_TIMEOUT: float = gen.HANDSHAKE_TIMEOUT_MS / 1000.0

#: Minimum quiet gap, in seconds, between any two sockets to the same peer:
#: one closing and the next opening, or one opening and another opening beside
#: it. The device refuses to greet — or resets — a session opened too soon after
#: either, so :meth:`Session.connect` holds every open until this much has
#: passed since the peer was last touched (see :data:`_LAST_TOUCH`). Callers
#: that open more than one session — the CBOR
#: :func:`~libkp.cbor.fetch_state_snapshot` then a MIDI3
#: :class:`~libkp.model.DeviceModel` — need not space them themselves; the
#: ledger does it. See ``docs/06``.
CONNECTION_COOLDOWN: float = gen.CONNECTION_COOLDOWN_MS / 1000.0

_GREETING_MAX = 256


@dataclass(slots=True)
class _PeerTouch:
    """When a socket to one peer was last opened and last closed, in event-loop
    time (:meth:`asyncio.AbstractEventLoop.time`, the monotonic clock, so the
    stamps stay meaningful across loops)."""

    last_open: float = float("-inf")
    last_close: float = float("-inf")

    @property
    def clear_at(self) -> float:
        """The earliest moment a new socket to this peer may be dialed."""
        return max(self.last_open, self.last_close) + CONNECTION_COOLDOWN


#: The connection ledger: ``(ip, port)`` → the last open and close of a socket
#: to that peer. Module-level on purpose: the hazard is the *device* seeing two
#: sockets too close together, whoever opened them, so the record has to be
#: shared by every :class:`Session` in the process rather than held per caller.
#: Keyed by port as well as host so that tests against ephemeral-port fakes do
#: not pay a cooldown for each other; only a reused port costs one.
_LAST_TOUCH: dict[tuple[str, int], _PeerTouch] = {}


async def _wait_turn(peer: tuple[str, int]) -> None:
    """Sleep until ``peer`` is clear of its cooldown, then claim it.

    The claim — stamping ``last_open`` — is made when the dial *begins*, not when
    it succeeds: two callers racing for the same peer must be serialised, and
    stamping only on success would let one expired cooldown release both. A dial
    that then fails keeps its stamp, since the device saw the attempt either
    way. Sleeping and re-checking in a loop is what makes the claim exclusive:
    a waiter that wakes to find another caller stamped in the meantime simply
    waits out that caller's cooldown too. The entry is looked up afresh on every
    pass rather than held across the sleep, because a peer quiet for a whole
    cooldown may be pruned and re-created underneath a sleeper.
    """
    loop = asyncio.get_running_loop()
    # Forget peers that have been quiet for a cooldown; nothing about them is
    # still useful, and tests open a great many one-off fakes.
    now = loop.time()
    for stale in [key for key, touch in _LAST_TOUCH.items() if touch.clear_at <= now]:
        del _LAST_TOUCH[stale]
    while True:
        touch = _LAST_TOUCH.setdefault(peer, _PeerTouch())
        remaining = touch.clear_at - loop.time()
        if remaining <= 0:
            break
        await asyncio.sleep(remaining)
    # No await between the check above and this stamp, so the claim is atomic
    # within the loop.
    touch.last_open = loop.time()


def parse_protocol_list(data: bytes) -> list[str]:
    """Parse the greeting's offered protocol list.

    The device sends one protocol identifier (a GUID) per CRLF-terminated line,
    ending with a line containing just ``"."``.
    """
    out: list[str] = []
    for line in data.decode("utf-8", "replace").replace("\r", "\n").split("\n"):
        text = line.strip()
        if not text:
            continue
        if text == gen.HANDSHAKE_LIST_END:
            break  # end-of-list marker
        out.append(text)
    return out


@dataclass(slots=True)
class HandshakeOutcome:
    """Result of the protocol-selection handshake."""

    #: Raw greeting bytes the device sent on connect.
    greeting: bytes = b""
    #: Protocol names parsed from the greeting.
    offered: list[str] = field(default_factory=list)
    #: The protocol name we selected and sent.
    selected: str = ""
    #: Raw device response to our selection (first byte ``+``/``-``).
    response: bytes = b""

    @property
    def accepted(self) -> bool:
        """True if the device's response opened with the accept prefix."""
        return self.response[:1] == gen.HANDSHAKE_ACCEPT_PREFIX.encode("ascii")

    def response_tail(self) -> bytes:
        """Stream bytes that arrived piggybacked after the ack line.

        The device often sends the first burst of session data in the same packet
        as the acceptance; feed this to the unframer before reading more.
        """
        pos = self.response.find(HANDSHAKE_TERMINATOR)
        return b"" if pos < 0 else self.response[pos + len(HANDSHAKE_TERMINATOR) :]


class Session:
    """An open TCP session with a Profiler.

    Opening one is paced by the connection ledger (:data:`_LAST_TOUCH`):
    :meth:`connect` will not dial a peer until :data:`CONNECTION_COOLDOWN` has
    passed since a socket to that peer was last opened or closed, and
    :meth:`close` stamps the ledger so the next open waits its turn. Sessions to
    *different* peers never wait on each other.
    """

    __slots__ = ("_reader", "_writer", "_peer", "_closed")

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        peer: tuple[str, int],
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._peer = peer
        self._closed = False

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    async def connect(cls, ip: str, port: int = PORT, timeout: float = CONNECT_TIMEOUT) -> Session:
        """Connect to ``ip:port`` with an explicit connect timeout.

        Waits first, if it must, for the peer's :data:`CONNECTION_COOLDOWN` to
        clear; ``timeout`` covers only the dial itself, not that wait.
        """
        peer = (ip, port)
        await _wait_turn(peer)
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
        except TimeoutError as exc:
            raise TimeoutErrorLibKP("connect", timeout) from exc
        except OSError as exc:
            raise ConnectError(peer, exc) from exc

        # The device is latency-sensitive for live control; disable Nagle.
        sock = writer.get_extra_info("socket")
        if sock is not None:
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:  # pragma: no cover - platform dependent
                pass
        return cls(reader, writer, peer)

    @property
    def peer(self) -> tuple[str, int]:
        """The connected device address."""
        return self._peer

    async def close(self) -> None:
        """Close the socket, ignoring an already-broken connection, and stamp the
        ledger so the next open to this peer waits out the cooldown.

        Closing twice is harmless and stamps only once; the cooldown runs from
        the moment the socket actually went away.
        """
        if self._closed:
            return
        self._closed = True
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except (OSError, ConnectionError):  # pragma: no cover - teardown races
            pass
        finally:
            touch = _LAST_TOUCH.setdefault(self._peer, _PeerTouch())
            touch.last_close = asyncio.get_running_loop().time()

    async def __aenter__(self) -> Session:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    # -- reads -------------------------------------------------------------

    async def read_available(self, idle: float, max_bytes: int = 64 * 1024) -> bytes:
        """Read whatever the device sends until an ``idle`` gap with no data.

        Also stops at ``max_bytes`` or EOF. Raises :class:`ConnectionClosedError`
        if the peer closed before any data arrived on this call.
        """
        buf = bytearray()
        while True:
            try:
                chunk = await asyncio.wait_for(self._reader.read(4096), idle)
            except TimeoutError:
                break  # idle gap — nothing more for now
            except OSError as exc:
                raise SessionError(f"i/o error during read: {exc}") from exc
            if not chunk:
                if not buf:
                    raise ConnectionClosedError()
                break
            buf.extend(chunk)
            if len(buf) >= max_bytes:
                break
        return bytes(buf)

    async def read_once(self, wait: float, max_bytes: int = 64 * 1024) -> bytes:
        """Do a **single** read with a timeout, returning the bytes read.

        Unlike :meth:`read_available`, this returns as soon as any data arrives
        (or the timeout elapses, yielding ``b""``). Use it to drive a render loop
        that must react to every packet even while the device streams
        continuously.
        """
        try:
            chunk = await asyncio.wait_for(self._reader.read(max_bytes), wait)
        except TimeoutError:
            return b""
        except OSError as exc:
            raise SessionError(f"i/o error during read: {exc}") from exc
        if not chunk:
            raise ConnectionClosedError()
        return chunk

    # -- writes ------------------------------------------------------------

    async def write_all(self, data: bytes) -> None:
        """Write all ``data`` to the device."""
        try:
            self._writer.write(data)
            await self._writer.drain()
        except (OSError, ConnectionError) as exc:
            raise SessionError(f"i/o error during write: {exc}") from exc

    async def write_session_preamble(self) -> None:
        """Write the zero preamble that opens the streaming session."""
        await self.write_all(SESSION_PREAMBLE)

    # -- handshake ---------------------------------------------------------

    async def _read_reply(self, phase: str, timeout: float, idle: float) -> bytes:
        """Read one handshake reply: wait up to ``timeout`` for its first byte,
        then keep collecting until an ``idle`` gap with no data.

        Built from the two read primitives rather than a third: :meth:`read_once`
        is the wait for the device to *start* answering, and :meth:`read_available`
        then gathers the rest of a reply that is already under way. Raises
        :class:`TimeoutErrorLibKP` for ``phase``, reporting the full ``timeout``,
        if the first byte never comes; a peer that hangs up instead raises
        :class:`ConnectionClosedError` from the first read, while a hang-up after
        the reply began just ends it, as it would any read.
        """
        first = await self.read_once(timeout, _GREETING_MAX)
        if not first:
            raise TimeoutErrorLibKP(phase, timeout)
        if len(first) >= _GREETING_MAX:
            return first
        try:
            rest = await self.read_available(idle, _GREETING_MAX - len(first))
        except ConnectionClosedError:
            rest = b""
        return first + rest

    async def select_protocol(
        self, name: str, resp_idle: float, timeout: float = HANDSHAKE_TIMEOUT
    ) -> bytes:
        """Send ``name`` + ``"\\r\\n"`` and read the device's response line.

        Waits up to ``timeout`` for the response to begin and then until a
        ``resp_idle`` gap for it to end. Raises :class:`ProtocolRejectedError`
        if the response begins with ``-`` and :class:`TimeoutErrorLibKP` for the
        ``"protocol selection"`` phase if no response comes at all.
        """
        await self.write_all(name.encode("ascii") + HANDSHAKE_TERMINATOR)
        resp = await self._read_reply("protocol selection", timeout, resp_idle)
        if resp[:1] == gen.HANDSHAKE_REJECT_PREFIX.encode("ascii"):
            raise ProtocolRejectedError(name, resp.decode("utf-8", "replace").strip())
        return resp

    async def handshake(
        self,
        preferred: list[str] | tuple[str, ...] = (PROTOCOL_MIDI3_STREAM,),
        idle: float = 0.03,
        timeout: float = HANDSHAKE_TIMEOUT,
    ) -> HandshakeOutcome:
        """Full handshake: read the greeting, pick the first ``preferred`` protocol
        the device offers (falling back to its first offered), and select it.

        ``timeout`` bounds the wait for the first byte of the greeting, and again
        of the selection response; ``idle`` is the quiet gap that ends each once
        it has begun. Raises :class:`TimeoutErrorLibKP` for the ``"greeting"``
        phase -- reporting ``timeout``, the wait actually made -- when the device
        never greets, or greets with no protocol to choose.
        """
        greeting = await self._read_reply("greeting", timeout, idle)
        offered = parse_protocol_list(greeting)

        selected = next((p for p in preferred if p in offered), None)
        if selected is None:
            selected = offered[0] if offered else None
        if selected is None:
            raise TimeoutErrorLibKP("greeting", timeout)

        response = await self.select_protocol(selected, idle, timeout)
        return HandshakeOutcome(
            greeting=greeting, offered=offered, selected=selected, response=response
        )
