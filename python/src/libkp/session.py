"""TCP session with a Profiler and the line-based protocol handshake.

Sequence (documented from observed experimentation):

1. TCP connect to the device on :data:`libkp.protocol.PORT` (5727).
2. The **device sends first**: a list of supported protocol GUIDs, one per
   CRLF-terminated line, ending with a line containing just ``"."``.
3. The client writes the chosen protocol name followed by ``"\\r\\n"``.
4. The device replies with a line beginning ``+`` (accept) or ``-`` (reject).
5. For the streaming protocol the client then writes an 8-byte zero preamble and
   the framed stream begins.
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
    "HandshakeOutcome",
    "Session",
    "parse_protocol_list",
]

#: The protocol GUID that streams live MIDI data (meters, params, tuner).
PROTOCOL_MIDI3_STREAM: str = gen.PROTOCOL_MIDI3_STREAM
#: Request/response protocol GUID: accepts the handshake but pushes nothing.
PROTOCOL_REQUEST_RESPONSE: str = gen.PROTOCOL_REQUEST_RESPONSE
#: Native CBOR control channel — documented, not implemented here.
PROTOCOL_CBOR_CONTROL: str = gen.PROTOCOL_CBOR_CONTROL
#: Offered by the device but rejects the handshake.
PROTOCOL_RESERVED: str = gen.PROTOCOL_RESERVED

#: Line terminator the client appends to the chosen protocol name.
HANDSHAKE_TERMINATOR: bytes = gen.HANDSHAKE_TERMINATOR.encode("ascii")

#: Zero bytes the client writes to open the streaming session.
SESSION_PREAMBLE: bytes = b"\x00" * gen.SESSION_PREAMBLE_LEN

#: Default connect timeout.
CONNECT_TIMEOUT: float = float(gen.CONNECT_TIMEOUT_SECS)

_GREETING_MAX = 256


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
    """An open TCP session with a Profiler."""

    __slots__ = ("_reader", "_writer", "_peer")

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        peer: tuple[str, int],
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._peer = peer

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    async def connect(
        cls, ip: str, port: int = PORT, timeout: float = CONNECT_TIMEOUT
    ) -> "Session":
        """Connect to ``ip:port`` with an explicit connect timeout."""
        peer = (ip, port)
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(ip, port), timeout
            )
        except asyncio.TimeoutError as exc:
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
        """Close the socket, ignoring an already-broken connection."""
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except (OSError, ConnectionError):  # pragma: no cover - teardown races
            pass

    async def __aenter__(self) -> "Session":
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
            except asyncio.TimeoutError:
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
        except asyncio.TimeoutError:
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

    async def select_protocol(self, name: str, resp_idle: float) -> bytes:
        """Send ``name`` + ``"\\r\\n"`` and read the device's response line.

        Raises :class:`ProtocolRejectedError` if the response begins with ``-``.
        """
        await self.write_all(name.encode("ascii") + HANDSHAKE_TERMINATOR)
        resp = await self.read_available(resp_idle, _GREETING_MAX)
        if resp[:1] == gen.HANDSHAKE_REJECT_PREFIX.encode("ascii"):
            raise ProtocolRejectedError(name, resp.decode("utf-8", "replace").strip())
        return resp

    async def handshake(
        self, preferred: list[str] | tuple[str, ...] = (PROTOCOL_MIDI3_STREAM,), idle: float = 0.03
    ) -> HandshakeOutcome:
        """Full handshake: read the greeting, pick the first ``preferred`` protocol
        the device offers (falling back to its first offered), and select it."""
        greeting = await self.read_available(idle, _GREETING_MAX)
        offered = parse_protocol_list(greeting)

        selected = next((p for p in preferred if p in offered), None)
        if selected is None:
            selected = offered[0] if offered else None
        if selected is None:
            raise TimeoutErrorLibKP("greeting", idle)

        response = await self.select_protocol(selected, idle)
        return HandshakeOutcome(
            greeting=greeting, offered=offered, selected=selected, response=response
        )
