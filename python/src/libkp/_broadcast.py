"""Fan-out of values to per-subscriber queues, shared by the two transports."""

from __future__ import annotations

import asyncio
import contextlib

#: Depth of each subscriber queue before the oldest item is dropped.
QUEUE_DEPTH: int = 256


class Broadcast:
    """Fan-out of values to per-subscriber queues, dropping the oldest on overflow.

    A slow consumer never blocks the ingest task; for snapshots, dropping an
    intermediate value loses nothing because the latest is always complete.
    """

    __slots__ = ("_queues",)

    def __init__(self) -> None:
        self._queues: list[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_DEPTH)
        self._queues.append(queue)
        return queue

    def empty(self) -> bool:
        """True while nothing is subscribed, so a caller can buffer instead of
        sending into a void."""
        return not self._queues

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with contextlib.suppress(ValueError):
            self._queues.remove(queue)

    def send(self, value: object) -> None:
        for queue in self._queues:
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(value)
