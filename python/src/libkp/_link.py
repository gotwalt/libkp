"""The stream link: the MIDI3 socket a :class:`~libkp.model.DeviceModel` owns.

One :class:`StreamLink` is one life of the stream: it dials, handshakes and
writes the preamble in :meth:`StreamLink.open`, then :meth:`StreamLink.start`
spawns the two tasks that own the socket -- an ingest task that reads, unframes
and hands every message to the model, and a writer task that drains a bounded
command queue to the wire. It decides nothing about the tree: the model folds
what it delivers, and the model is told when the socket ends.

The control channel's counterpart is :class:`libkp.cbor.ControlLink`, which
lives beside the CBOR codec it is built on.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable

from . import midi3
from .errors import SessionError
from .session import PROTOCOL_MIDI3_STREAM, Session

__all__ = ["StreamLink", "READ_IDLE", "READ_MAX", "COMMAND_QUEUE_DEPTH"]

#: Read idle gap driving the ingest loop; short so it reacts per packet.
READ_IDLE: float = 0.03
#: Max bytes per stream read.
READ_MAX: int = 64 * 1024
#: How many outbound messages may wait for the writer before a sender blocks.
#: Bounded so a wedged socket stalls the caller rather than growing a queue.
COMMAND_QUEUE_DEPTH: int = 64


class StreamLink:
    """The MIDI3 socket: an ingest task, a writer task, and the session both
    share."""

    __slots__ = ("_session", "_tail", "_commands", "_tasks", "_closed")

    def __init__(self, session: Session, tail: bytes) -> None:
        self._session = session
        self._tail = tail
        #: Each item is one or more messages written back to back as a unit,
        #: so the Navigator's bank-preselect/slot-load pair cannot be split.
        self._commands: asyncio.Queue[tuple[bytes, ...]] = asyncio.Queue(
            maxsize=COMMAND_QUEUE_DEPTH
        )
        self._tasks: list[asyncio.Task] = []
        self._closed = False

    @classmethod
    async def open(cls, ip: str, port: int) -> StreamLink:
        """Dial ``ip:port`` (paced by the connection ledger), select the
        streaming protocol and write the preamble.

        Every failure closes the socket before propagating: a session left open
        with no owner is exactly the churn the device does not survive.
        """
        session = await Session.connect(ip, port)
        try:
            outcome = await session.handshake([PROTOCOL_MIDI3_STREAM], READ_IDLE)
            await session.write_session_preamble()
        except BaseException:
            await session.close()
            raise
        return cls(session, outcome.response_tail())

    def start(
        self,
        on_chunk: Callable[[list[bytes]], None],
        on_lost: Callable[[], None],
    ) -> None:
        """Spawn the ingest and writer tasks.

        ``on_chunk`` receives the unframed messages of each read, in order, so
        the model can fold a whole chunk before publishing one snapshot.
        ``on_lost`` is called once, from whichever task first finds the socket
        gone, and never after :meth:`close`.
        """
        loop = asyncio.get_running_loop()
        self._tasks.append(loop.create_task(self._ingest(on_chunk, on_lost)))
        self._tasks.append(loop.create_task(self._writer(on_lost)))

    async def send(self, message: bytes) -> None:
        """Queue one raw (pre-framing) MIDI message for the writer."""
        await self._commands.put((message,))

    def send_nowait(self, message: bytes) -> None:
        """Queue one message without waiting, for a caller that cannot -- the
        Navigator's timers fire in plain callbacks. Raises
        :class:`asyncio.QueueFull` when the writer is
        :data:`COMMAND_QUEUE_DEPTH` messages behind, which is a socket that
        has stopped taking bytes, not a burst of commands."""
        self._commands.put_nowait((message,))

    def send_pair_nowait(self, first: bytes, second: bytes) -> None:
        """Queue two messages as one unit -- the Navigator's bank preselect
        and slot load -- so both go on the wire back to back. Raises
        :class:`asyncio.QueueFull` exactly as :meth:`send_nowait` does, and a
        refusal queues neither: an orphaned preselect would leave the device
        armed for a load that never followed. The pair travels as one queue
        item but is two commands, so it is refused unless the queue has room
        for both -- the same depth at which the other implementations refuse
        it."""
        if self._commands.maxsize - self._commands.qsize() < 2:
            raise asyncio.QueueFull
        self._commands.put_nowait((first, second))

    async def close(self) -> None:
        """Stop both tasks and close the socket. Idempotent."""
        if self._closed:
            return
        self._closed = True
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        await self._session.close()

    async def _ingest(
        self, on_chunk: Callable[[list[bytes]], None], on_lost: Callable[[], None]
    ) -> None:
        """Own the socket's read side: read, unframe, hand over."""
        unframer = midi3.Unframer()
        try:
            # Decode anything that rode in on the handshake acceptance tail.
            tail = unframer.push(self._tail)
            if tail:
                on_chunk(tail)
            while True:
                chunk = await self._session.read_once(READ_IDLE, READ_MAX)
                if chunk:
                    messages = unframer.push(chunk)
                    if messages:
                        on_chunk(messages)
        except asyncio.CancelledError:
            raise
        except SessionError:
            if not self._closed:
                on_lost()

    async def _writer(self, on_lost: Callable[[], None]) -> None:
        """Drain the command queue to the wire, framing each message."""
        try:
            while True:
                messages = await self._commands.get()
                await self._session.write_all(b"".join(midi3.frame(m) for m in messages))
        except asyncio.CancelledError:
            raise
        except SessionError:
            if not self._closed:
                on_lost()
