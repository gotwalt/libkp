"""The request lane: pacing read requests on the stream and matching replies.

A request on the MIDI3 stream is a fire-and-forget SysEx; the reply is just
another value arriving at the address asked for, indistinguishable from an
unsolicited push of the same address. The lane turns that into request/reply:
:meth:`RequestLane.request` sends the message, registers what it is waiting
for, and returns once a matching value has come through the fold or
:data:`libkp._generated.REQUEST_TIMEOUT_MS` has passed.

Pacing is the point. The device answers every request type in well under 50 ms
even with the whole connect burst in flight (docs/11), so the caps are
generous, but they are caps: at most
:data:`libkp._generated.MAX_IN_FLIGHT_REQUESTS` requests are on the wire at
once and the rest wait their turn rather than being dropped, and a request
that goes unanswered is abandoned, never retried -- the device ignores an
address it cannot answer, and asking twice only costs it more.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Hashable

from . import _generated as gen
from .errors import RequestDisconnectedError, RequestTimeoutError

__all__ = ["RequestLane", "REQUEST_TIMEOUT", "MAX_IN_FLIGHT"]

#: How long a request waits for its reply, in seconds.
REQUEST_TIMEOUT: float = gen.REQUEST_TIMEOUT_MS / 1000.0
#: How many requests may be on the wire at once; the rest queue.
MAX_IN_FLIGHT: int = gen.MAX_IN_FLIGHT_REQUESTS


class RequestLane:
    """Pending requests keyed by what will answer them.

    A key is whatever the caller will recognise the reply by -- the flat
    address plus the shape of value expected there, or the page/number/value
    triple of a rendered-string request -- and :meth:`resolve` is called by the
    fold for every value it sees. Several requests waiting on one key are all
    answered by the one reply.
    """

    __slots__ = ("_send", "_on_timeout", "_timeout", "_slots", "_pending")

    def __init__(
        self,
        send: Callable[[bytes], Awaitable[None]],
        on_timeout: Callable[[int], None],
        *,
        timeout: float = REQUEST_TIMEOUT,
        max_in_flight: int = MAX_IN_FLIGHT,
    ) -> None:
        self._send = send
        self._on_timeout = on_timeout
        self._timeout = timeout
        # A semaphore is FIFO in asyncio, so waiting requests go out in the
        # order they were made once a slot frees up.
        self._slots = asyncio.Semaphore(max_in_flight)
        self._pending: dict[Hashable, list[asyncio.Future]] = {}

    async def request(self, key: Hashable, address: int, message: bytes) -> object:
        """Send ``message`` once a slot is free and wait for the value that
        answers ``key``.

        ``address`` is what a timeout is reported against. Raises
        :class:`~libkp.errors.RequestTimeoutError` when nothing answers in
        time, and :class:`~libkp.errors.RequestDisconnectedError` if the
        stream goes away while waiting.
        """
        loop = asyncio.get_running_loop()
        async with self._slots:
            future: asyncio.Future = loop.create_future()
            self._pending.setdefault(key, []).append(future)
            try:
                await self._send(message)
                return await asyncio.wait_for(future, self._timeout)
            except TimeoutError:
                self._on_timeout(address)
                raise RequestTimeoutError(address, self._timeout) from None
            finally:
                waiters = self._pending.get(key)
                if waiters is not None:
                    if future in waiters:
                        waiters.remove(future)
                    if not waiters:
                        del self._pending[key]

    def resolve(self, key: Hashable, value: object) -> None:
        """Answer every request waiting on ``key`` with ``value``."""
        waiters = self._pending.pop(key, None)
        if not waiters:
            return
        for future in waiters:
            if not future.done():
                future.set_result(value)

    def fail_all(self) -> None:
        """The stream is gone: every waiting request is told so."""
        pending = list(self._pending.values())
        self._pending.clear()
        for waiters in pending:
            for future in waiters:
                if not future.done():
                    future.set_exception(RequestDisconnectedError())

    @property
    def pending(self) -> int:
        """How many requests are waiting for a reply."""
        return sum(len(waiters) for waiters in self._pending.values())
