"""Finding a Profiler, and finding it again when its address moves.

The Profiler answers a UDP broadcast with its name, **serial** and firmware
version, and costs itself nothing to do it — no session, no handshake. That
makes the serial the device's real identity and the IP address merely where it
happens to be today: a DHCP lease expires over a weekend and the same amp comes
back on a different address.

So discovery is used in two places, and both live here rather than in the
config flow: once when a Profiler is added, and once per setup to ask "where is
serial X now". Every poll is a single 3-second listen — there is no loop, and a
port that another program holds is an ordinary answer of "nothing found".
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .const import DEFAULT_NAME, DIRECTED_DISCOVERY_SECONDS, DISCOVERY_SECONDS
from .libkp import DiscoveryOptions, DiscoveryPort, LibKPError
from .libkp.discovery import Reply


@dataclass(frozen=True, slots=True)
class Found:
    """One Profiler that answered a discovery poll."""

    host: str
    name: str
    serial: str | None
    version: str | None

    @classmethod
    def from_reply(cls, reply: Reply) -> Found:
        """What a raw reply says about the device that sent it."""
        return cls(
            host=reply.ip,
            name=reply.name or DEFAULT_NAME,
            serial=reply.serial,
            version=reply.version,
        )


async def async_discover(
    *, listen_for: float = DISCOVERY_SECONDS, targets: Iterable[str] | None = None
) -> list[Found]:
    """Poll for Profilers. An unavailable port or a failed poll means none.

    ``targets`` adds explicit unicast destinations to the broadcast, which is
    how a device on the other side of a router — one no broadcast reaches — can
    still be asked to identify itself.
    """
    try:
        with DiscoveryPort.acquire() as port:
            replies = await port.poll(
                DiscoveryOptions(listen_for=listen_for, extra_targets=list(targets or ()))
            )
    except LibKPError, OSError:
        return []
    return [Found.from_reply(reply) for reply in replies]


async def async_find_serial(serial: str) -> Found | None:
    """Where the Profiler with this serial is now, if it answers at all."""
    for found in await async_discover():
        if found.serial == serial:
            return found
    return None


async def async_identify(host: str) -> Found | None:
    """Ask one known address who it is: a short, directed poll.

    Used after a manually entered host has been proved to be a Profiler, so
    that a hand-added device is keyed by its serial like a discovered one and
    survives moving to another address.
    """
    for found in await async_discover(listen_for=DIRECTED_DISCOVERY_SECONDS, targets=[host]):
        if found.host == host:
            return found
    return None
