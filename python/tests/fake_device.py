"""A minimal in-process stand-in for a Profiler, for exercising the async layers.

It speaks just enough of the transport to drive :class:`libkp.session.Session`
and :class:`libkp.model.DeviceModel`: the greeting, the protocol selection ack
(optionally with a piggybacked stream tail), then framed MIDI in both
directions.
"""

from __future__ import annotations

import asyncio

from libkp import midi3
from libkp.session import PROTOCOL_MIDI3_STREAM, PROTOCOL_RESERVED


class FakeDevice:
    """A localhost TCP server that mimics the device side of the handshake."""

    def __init__(
        self,
        *,
        offered: list[str] | None = None,
        accept: bool = True,
        tail_messages: list[bytes] | None = None,
        push_messages: list[bytes] | None = None,
        close_after_handshake: bool = False,
    ) -> None:
        self.offered = (
            offered if offered is not None else [PROTOCOL_RESERVED, PROTOCOL_MIDI3_STREAM]
        )
        self.accept = accept
        self.tail_messages = tail_messages or []
        self.push_messages = push_messages or []
        self.close_after_handshake = close_after_handshake

        #: Raw MIDI messages the client sent us (unframed).
        self.received: list[bytes] = []
        #: The protocol name the client selected.
        self.selected: str | None = None
        #: Whether the client wrote the session preamble.
        self.saw_preamble = asyncio.Event()

        self._server: asyncio.Server | None = None
        self._unframer = midi3.Unframer()
        self._writer: asyncio.StreamWriter | None = None

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

    async def __aenter__(self) -> FakeDevice:
        return await self.start()

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()

    async def push(self, message: bytes) -> None:
        """Send one raw MIDI message to the connected client, framed."""
        assert self._writer is not None, "no client connected"
        self._writer.write(midi3.frame(message))
        await self._writer.drain()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writer = writer
        greeting = "".join(f"{name}\r\n" for name in self.offered) + ".\r\n"
        writer.write(greeting.encode("ascii"))
        await writer.drain()

        line = await reader.readline()
        self.selected = line.decode("ascii").strip()
        if not self.accept:
            writer.write(b"-NO\r\n")
            await writer.drain()
            writer.close()
            return

        response = bytearray(f"+{self.selected}\r\n".encode("ascii"))
        for message in self.tail_messages:
            response.extend(midi3.frame(message))
        writer.write(bytes(response))
        await writer.drain()

        preamble = await reader.readexactly(8)
        if preamble == b"\x00" * 8:
            self.saw_preamble.set()

        if self.close_after_handshake:
            writer.close()
            return

        for message in self.push_messages:
            writer.write(midi3.frame(message))
        await writer.drain()

        try:
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    break
                self.received.extend(self._unframer.push(chunk))
        except (ConnectionError, asyncio.IncompleteReadError):  # pragma: no cover
            pass
        finally:
            writer.close()


async def wait_for(predicate, timeout: float = 2.0, interval: float = 0.005) -> bool:
    """Poll ``predicate`` until it is true or ``timeout`` elapses."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()
