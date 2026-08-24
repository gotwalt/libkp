"""An in-process stand-in for a Profiler, for exercising the async layers.

It speaks just enough of the transport to drive :class:`libkp.session.Session`,
:class:`libkp.model.DeviceModel` and the CBOR tooling: any number of concurrent
connections, each with the greeting, the protocol selection ack (``+`` only for
a protocol the fake offers, ``-`` otherwise -- the reserved GUID is offered and
rejected, as on the device), the preamble, then the protocol's own traffic:

- **MIDI3** (:data:`~libkp.session.PROTOCOL_MIDI3_STREAM`): framed MIDI in both
  directions. Received messages are unframed and recorded per connection; a
  ``responder`` can answer them (:func:`answer_requests` answers every request
  form with a zero or a placeholder string), and :meth:`FakeDevice.push` sends
  arbitrary messages.
- **CBOR** (:data:`~libkp.session.PROTOCOL_CBOR_CONTROL`): decoded items are
  recorded per connection; the dump trigger is answered with ``dump_items``
  (by default :data:`DEFAULT_DUMP`, a short dump in the real one's two-section
  shape, each section closed by a run at
  :data:`~libkp._generated.DUMP_END_ADDRESS`), and
  :meth:`FakeDevice.push_items` sends more at any time.

Either kind of connection, or all of them, can be hung up. The greeting can
be held back for a while (``greeting_delay``), as a device that has served a
few sessions does, or withheld altogether (``greet=False``) to exercise the
handshake's timeout; the selection's answer likewise (``selection_delay``,
``ack=False``). :meth:`FakeDevice.push_raw` writes pre-framed bytes in one
segment, so several messages can land in one read, and
:meth:`FakeDevice.pause_accepting` / :meth:`FakeDevice.resume_accepting`
refuse and then re-admit fresh connections, for a reconnect that must try
more than once.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable

from libkp import _generated as gen
from libkp import cbor, midi3, nrpn
from libkp.session import (
    PROTOCOL_CBOR_CONTROL,
    PROTOCOL_MIDI3_STREAM,
    PROTOCOL_RESERVED,
    SESSION_PREAMBLE,
)

#: The dump a CBOR connection serves by default, in the real dump's two-section
#: shape: a system section closed by a run based at
#: :data:`~libkp._generated.DUMP_END_ADDRESS`, then the rig section, which
#: opens with the position as one run and carries the rig name and the morph,
#: closed by the second such run -- the one that ends the dump.
DEFAULT_DUMP: list = [
    cbor.param_write(gen.SYSTEM_PAGE * 128 + gen.MAIN_VOLUME_NUMBER, 8192),
    cbor.Tag(1, [2, gen.DUMP_END_ADDRESS, 0, 0]),
    cbor.Tag(1, [2, gen.CURRENT_BANK_ADDRESS - 1, 0, 3, 1]),
    cbor.Tag(1, [4, gen.STRING_RIG_NAME, "Dump Rig"]),
    cbor.param_write(gen.MORPH_ADDRESS, 8192),
    cbor.Tag(1, [2, gen.DUMP_END_ADDRESS, 0, 0]),
]


def answer_requests(message: bytes) -> list[bytes]:
    """A ``responder`` that answers every request form the model sends with a
    placeholder: ``$41`` → ``$01`` value 0, ``$43`` → ``$03`` ``"X"``, ``$46``
    → ``$06`` value 0, ``$47`` → ``$07`` ``"X"``. Anything else draws nothing.
    """
    parsed = nrpn.NrpnHeader.parse(message)
    if parsed is None:
        return []
    header, _values = parsed
    function = header.function
    if function == nrpn.FUNCTION_REQUEST_SINGLE:
        return [nrpn.set_single(0x00, 0x00, header.page, header.number, 0)]
    if function == nrpn.FUNCTION_REQUEST_STRING:
        return [
            nrpn.sysex(0x00, 0x00, nrpn.FUNCTION_STRING_PARAM, header.page, header.number, b"X\x00")
        ]
    if function == nrpn.FUNCTION_REQUEST_EXT_PARAM:
        address = nrpn.ext_decode(message[8:13])
        return [ext_param(address, 0)]
    if function == nrpn.FUNCTION_REQUEST_EXT_STRING:
        address = nrpn.ext_decode(message[8:13])
        return [ext_string(address, "X")]
    return []


def ext_param(address: int, value: int) -> bytes:
    """A ``$06`` Extended Parameter message."""
    out = bytearray([0xF0])
    out.extend(nrpn.MANUFACTURER_ID)
    out.extend([0x00, 0x00, nrpn.FUNCTION_EXT_PARAM, 0x00])
    out.extend(nrpn.ext_encode(address, 5))
    out.extend(nrpn.ext_encode(value, 5))
    out.append(0xF7)
    return bytes(out)


def ext_string(address: int, text: str) -> bytes:
    """A ``$07`` Extended String Parameter message."""
    out = bytearray([0xF0])
    out.extend(nrpn.MANUFACTURER_ID)
    out.extend([0x00, 0x00, nrpn.FUNCTION_EXT_STRING_PARAM, 0x00])
    out.extend(nrpn.ext_encode(address, 5))
    out.extend(text.encode("ascii"))
    out.extend([0x00, 0xF7])
    return bytes(out)


class FakeConnection:
    """One accepted socket and what happened on it."""

    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self._writer = writer
        #: The protocol name the client selected, once it has.
        self.selected: str | None = None
        #: Raw MIDI messages the client sent (unframed), on a MIDI3 connection.
        self.received: list[bytes] = []
        #: Decoded CBOR items the client sent, on a CBOR connection.
        self.received_items: list = []
        #: Set once the client wrote the session preamble.
        self.saw_preamble = asyncio.Event()
        #: Set once the socket is closed, by either side.
        self.closed = asyncio.Event()

    async def push(self, message: bytes) -> None:
        """Send one raw MIDI message, framed."""
        await self.push_raw(midi3.frame(message))

    async def push_raw(self, data: bytes) -> None:
        """Write ``data`` as it is, in one segment: several framed messages
        back to back land in one read on the other side."""
        self._writer.write(data)
        await self._writer.drain()

    async def push_items(self, items: Iterable) -> None:
        """Send CBOR items, encoded back to back in one write."""
        out = bytearray()
        for item in items:
            cbor.encode(item, out)
        self._writer.write(bytes(out))
        await self._writer.drain()

    async def hangup(self) -> None:
        """Close the socket from the device side."""
        if self.closed.is_set():
            return
        self._writer.close()
        with _suppress_teardown():
            await self._writer.wait_closed()
        self.closed.set()


class FakeDevice:
    """A localhost TCP server that mimics the device side of the protocols."""

    def __init__(
        self,
        *,
        offered: list[str] | None = None,
        accept: bool = True,
        accepts: Iterable[str] | None = None,
        offer_cbor: bool = False,
        tail_messages: list[bytes] | None = None,
        tail_items: list | None = None,
        push_messages: list[bytes] | None = None,
        close_after_handshake: bool = False,
        responder: Callable[[bytes], list[bytes]] | None = None,
        dump_items: list | None = None,
        greeting_delay: float = 0.0,
        greet: bool = True,
        selection_delay: float = 0.0,
        ack: bool = True,
    ) -> None:
        """
        ``offered`` is the greeting (by default the reserved GUID and the MIDI3
        stream; ``offer_cbor`` appends the CBOR control GUID). ``accepts`` is
        the set the fake answers ``+`` to: by default everything offered except
        the reserved GUID, which the device offers and rejects. ``accept=False``
        rejects every selection. ``tail_messages`` (MIDI, on the stream) and
        ``tail_items`` (CBOR, on the control channel) ride in the same segment
        as the acceptance line, as the device's first burst does.
        ``responder`` answers each received MIDI
        message with the messages it returns. ``dump_items`` is what a CBOR
        connection serves when the dump trigger arrives (default
        :data:`DEFAULT_DUMP`; ``[]`` serves nothing). ``greeting_delay`` is how
        many seconds the fake sits on a fresh connection before greeting, and
        ``greet=False`` never greets at all, holding the socket open until the
        client gives up. ``selection_delay`` and ``ack=False`` do the same to
        the answer to the selection: the fake greets, reads the selection, and
        then sits on the answer, or never gives one.
        """
        if offered is None:
            offered = [PROTOCOL_RESERVED, PROTOCOL_MIDI3_STREAM]
            if offer_cbor:
                offered = [*offered, PROTOCOL_CBOR_CONTROL]
        self.offered = offered
        if not accept:
            self.accepts: set[str] = set()
        elif accepts is not None:
            self.accepts = set(accepts)
        else:
            self.accepts = {name for name in offered if name != PROTOCOL_RESERVED}
        self.tail_messages = tail_messages or []
        self.tail_items = tail_items or []
        self.push_messages = push_messages or []
        self.close_after_handshake = close_after_handshake
        self.responder = responder
        self.dump_items = DEFAULT_DUMP if dump_items is None else dump_items
        self.greeting_delay = greeting_delay
        self.greet = greet
        self.selection_delay = selection_delay
        self.ack = ack

        #: Every connection accepted so far, in order.
        self.connections: list[FakeConnection] = []
        #: Set once any connection has written the session preamble.
        self.saw_preamble = asyncio.Event()
        #: How many connections were refused while accepting was paused.
        self.refused = 0

        self._server: asyncio.Server | None = None
        self._accepting = True

    # -- conveniences over the connections ----------------------------------

    def _latest(self, protocol: str) -> FakeConnection | None:
        for connection in reversed(self.connections):
            if connection.selected == protocol:
                return connection
        return None

    @property
    def stream(self) -> FakeConnection | None:
        """The most recent MIDI3 connection, if any."""
        return self._latest(PROTOCOL_MIDI3_STREAM)

    @property
    def control(self) -> FakeConnection | None:
        """The most recent CBOR connection, if any."""
        return self._latest(PROTOCOL_CBOR_CONTROL)

    @property
    def received(self) -> list[bytes]:
        """What the most recent MIDI3 connection received (empty before one)."""
        stream = self.stream
        return [] if stream is None else stream.received

    @property
    def selected(self) -> str | None:
        """The protocol the most recent connection selected."""
        return self.connections[-1].selected if self.connections else None

    def connection_count(self, protocol: str) -> int:
        """How many connections selected ``protocol``."""
        return sum(1 for c in self.connections if c.selected == protocol)

    async def push(self, message: bytes) -> None:
        """Send one raw MIDI message to the MIDI3 client, framed."""
        stream = self.stream
        assert stream is not None, "no stream client connected"
        await stream.push(message)

    async def push_raw(self, data: bytes) -> None:
        """Write pre-framed bytes to the MIDI3 client in one segment."""
        stream = self.stream
        assert stream is not None, "no stream client connected"
        await stream.push_raw(data)

    async def push_items(self, items: Iterable) -> None:
        """Send CBOR items to the CBOR client."""
        control = self.control
        assert control is not None, "no control client connected"
        await control.push_items(items)

    def pause_accepting(self) -> None:
        """Refuse fresh connections: each is closed the moment it is accepted,
        before any greeting, as a device that is not ready does. The listener
        stays bound, so the port is the same when accepting resumes."""
        self._accepting = False

    def resume_accepting(self) -> None:
        """Admit fresh connections again."""
        self._accepting = True

    async def hangup(self, protocol: str | None = None) -> None:
        """Close the most recent connection of ``protocol``, or every
        connection when none is named."""
        if protocol is None:
            for connection in list(self.connections):
                await connection.hangup()
            return
        connection = self._latest(protocol)
        if connection is not None:
            await connection.hangup()

    # -- lifecycle ------------------------------------------------------------

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.sockets[0].getsockname()[1]

    async def start(self) -> FakeDevice:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        return self

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        await self.hangup()

    async def __aenter__(self) -> FakeDevice:
        return await self.start()

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()

    # -- one connection ------------------------------------------------------

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if not self._accepting:
            self.refused += 1
            writer.close()
            with _suppress_teardown():
                await writer.wait_closed()
            return
        connection = FakeConnection(writer)
        self.connections.append(connection)
        try:
            await self._handshake(connection, reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError):  # pragma: no cover
            pass
        finally:
            writer.close()
            connection.closed.set()

    async def _handshake(
        self,
        connection: FakeConnection,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        if not self.greet:
            # Say nothing, ever; the connection ends when the client hangs up.
            while await reader.read(4096):
                pass
            return
        if self.greeting_delay:
            await asyncio.sleep(self.greeting_delay)
        greeting = "".join(f"{name}\r\n" for name in self.offered) + ".\r\n"
        writer.write(greeting.encode("ascii"))
        await writer.drain()

        line = await reader.readline()
        if not line:
            # The client hung up without selecting: nothing was selected.
            return
        selected = line.decode("ascii").strip()
        connection.selected = selected
        if not self.ack:
            # Say nothing more, ever; the connection ends when the client hangs up.
            while await reader.read(4096):
                pass
            return
        if self.selection_delay:
            await asyncio.sleep(self.selection_delay)
        if selected not in self.accepts:
            writer.write(b"-NO\r\n")
            await writer.drain()
            return

        response = bytearray(f"+{selected}\r\n".encode("ascii"))
        if selected == PROTOCOL_MIDI3_STREAM:
            for message in self.tail_messages:
                response.extend(midi3.frame(message))
        elif selected == PROTOCOL_CBOR_CONTROL:
            for item in self.tail_items:
                cbor.encode(item, response)
        writer.write(bytes(response))
        await writer.drain()

        preamble = await reader.readexactly(len(SESSION_PREAMBLE))
        if preamble == SESSION_PREAMBLE:
            connection.saw_preamble.set()
            self.saw_preamble.set()

        if self.close_after_handshake:
            return

        if selected == PROTOCOL_CBOR_CONTROL:
            await self._serve_cbor(connection, reader, writer)
        else:
            await self._serve_midi3(connection, reader, writer)

    async def _serve_midi3(
        self,
        connection: FakeConnection,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        for message in self.push_messages:
            writer.write(midi3.frame(message))
        await writer.drain()

        unframer = midi3.Unframer()
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                break
            messages = unframer.push(chunk)
            connection.received.extend(messages)
            if self.responder is not None:
                replies = [reply for message in messages for reply in self.responder(message)]
                if replies:
                    writer.write(b"".join(midi3.frame(reply) for reply in replies))
                    await writer.drain()

    async def _serve_cbor(
        self,
        connection: FakeConnection,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        decoder = cbor.Decoder()
        trigger = cbor.state_dump_request()
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                break
            items = decoder.push(chunk)
            connection.received_items.extend(items)
            if trigger in items and self.dump_items:
                await connection.push_items(self.dump_items)


class _suppress_teardown:
    """Ignore the errors a socket closing from both sides at once can raise."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        return exc_type is not None and issubclass(exc_type, (OSError, ConnectionError))


async def wait_for(predicate, timeout: float = 2.0, interval: float = 0.005) -> bool:
    """Poll ``predicate`` until it is true or ``timeout`` elapses."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()
