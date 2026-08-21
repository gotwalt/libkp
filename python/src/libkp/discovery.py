"""Async UDP discovery: broadcast the poll, collect Profiler replies.

The client broadcasts the fixed poll packet built by
:func:`libkp.protocol.build_poll_request` to every plausible broadcast address
and listens on the same port for replies. Replies are TagStreams of ``[4-char
key][value]`` fields (``NAME``, ``SER#``, ``VSTR``, …).
"""

from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass, field

from . import _generated as gen
from .errors import DiscoverError, ParseError
from .protocol import PLACEHOLDER_MAC, PORT, TagStream, build_poll_request

__all__ = ["Reply", "DiscoveryOptions", "discover", "find_first"]

#: The global IPv4 broadcast address.
_GLOBAL_BROADCAST = "255.255.255.255"


@dataclass(slots=True)
class Reply:
    """One raw reply from a candidate device."""

    #: The source address the reply came from.
    addr: tuple[str, int]
    #: The raw payload bytes.
    payload: bytes

    @property
    def ip(self) -> str:
        """The sender's IP address."""
        return self.addr[0]

    def tags(self) -> TagStream | None:
        """The parsed TagStream, or ``None`` if the payload is malformed."""
        try:
            return TagStream.parse(self.payload)
        except ParseError:
            return None

    def _text(self, key: str) -> str | None:
        tags = self.tags()
        if tags is None:
            return None
        value = tags.get(key)
        return None if value is None else value.decode("utf-8", "replace")

    @property
    def name(self) -> str | None:
        """The device's advertised ``NAME`` field, if the payload parses."""
        return self._text("NAME")

    @property
    def serial(self) -> str | None:
        """The device's advertised ``SER#`` field, if the payload parses."""
        return self._text("SER#")

    @property
    def version(self) -> str | None:
        """The device's advertised ``VSTR`` version string, if present."""
        return self._text("VSTR")


@dataclass(slots=True)
class DiscoveryOptions:
    """Options controlling a discovery run."""

    #: Client MAC placed in the poll (the all-zero placeholder is fine).
    mac: str = PLACEHOLDER_MAC
    #: How long to keep listening for replies, in seconds.
    listen_for: float = 3.0
    #: Re-send the poll this often while listening, in seconds.
    repeat_every: float = gen.POLL_INTERVAL_MS / 1000.0
    #: Extra explicit targets to send the poll to (e.g. a known device IP).
    extra_targets: list[str] = field(default_factory=list)
    #: UDP port to poll on.
    port: int = PORT


def _is_poll(payload: bytes) -> bool:
    """Whether a packet is a discovery POLL (ours or another client's) rather than
    a device reply. Polls carry a ``POLL`` field; replies carry NAME/SER#/…."""
    try:
        tags = TagStream.parse(payload)
    except ParseError:
        return False
    return any(key == "POLL" for key, _ in tags.key_values())


def _local_ipv4_addresses() -> list[str]:
    """Best-effort list of this host's IPv4 addresses, standard library only."""
    found: set[str] = set()

    # The routing table knows which address would be used to reach the LAN; no
    # packet is actually sent by connecting a UDP socket.
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # TEST-NET-1, never routed
        found.add(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except (OSError, UnicodeError):  # pragma: no cover - host-name dependent
        pass

    return [ip for ip in sorted(found) if not ip.startswith("127.")]


def broadcast_targets(extra: list[str] | None = None) -> list[str]:
    """Poll targets: the global broadcast address, a /24 subnet broadcast for each
    local IPv4 interface, plus any caller-supplied extras.

    The /24 assumption is a standard-library compromise — netmasks are not
    portable to read without a third-party dependency — and only ever adds extra
    destinations for the poll.
    """
    targets = [_GLOBAL_BROADCAST]
    for ip in _local_ipv4_addresses():
        octets = ip.split(".")
        if len(octets) != 4:  # pragma: no cover - defensive
            continue
        bcast = ".".join(octets[:3] + ["255"])
        if bcast not in targets:
            targets.append(bcast)
    for ip in extra or ():
        if ip not in targets:
            targets.append(ip)
    return targets


def _bind_broadcast_socket(port: int) -> socket.socket:
    """Create a UDP socket suitable for broadcast discovery, bound to ``port``."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    except OSError as exc:
        raise DiscoverError(f"failed to create UDP socket: {exc}") from exc
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        reuse_port = getattr(socket, "SO_REUSEPORT", None)
        if reuse_port is not None:
            sock.setsockopt(socket.SOL_SOCKET, reuse_port, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setblocking(False)
        sock.bind(("0.0.0.0", port))
    except OSError as exc:
        sock.close()
        raise DiscoverError(f"failed to configure discovery socket: {exc}") from exc
    return sock


async def _send_round(
    loop: asyncio.AbstractEventLoop, sock: socket.socket, poll: bytes, targets: list[str], port: int
) -> None:
    """Send the poll to every target. A single unreachable target (e.g. global
    broadcast denied by a firewall) must not abort the whole sweep."""
    sent = 0
    last_error: OSError | None = None
    for ip in targets:
        try:
            await loop.sock_sendto(sock, poll, (ip, port))
            sent += 1
        except OSError as exc:
            last_error = exc
    if sent == 0 and last_error is not None:
        raise DiscoverError(f"failed to send discovery poll: {last_error}")


async def discover(options: DiscoveryOptions | None = None) -> list[Reply]:
    """Broadcast the discovery poll and gather replies until the window ends.

    Returns one :class:`Reply` per distinct source IP (last payload wins). The
    device answers from a fresh ephemeral port each poll, so keying by full
    source address would report one "device" per reply.
    """
    opts = options or DiscoveryOptions()
    loop = asyncio.get_running_loop()
    poll = build_poll_request(opts.mac)
    targets = broadcast_targets(opts.extra_targets)
    sock = _bind_broadcast_socket(opts.port)

    replies: dict[str, Reply] = {}
    try:
        await _send_round(loop, sock, poll, targets, opts.port)
        deadline = loop.time() + opts.listen_for
        next_poll = loop.time() + opts.repeat_every

        while True:
            now = loop.time()
            if now >= deadline:
                break
            window = min(deadline, next_poll) - now
            if window > 0:
                try:
                    payload, addr = await asyncio.wait_for(loop.sock_recvfrom(sock, 2048), window)
                except TimeoutError:
                    payload, addr = b"", None
                except OSError as exc:
                    raise DiscoverError(f"failed while receiving replies: {exc}") from exc
                # Ignore POLL packets — our own echoed back on the shared socket,
                # or another client's — since recording one as a "device" would
                # return a random client's IP as the Profiler.
                if addr is not None and payload and not _is_poll(payload):
                    replies[addr[0]] = Reply(addr=addr, payload=payload)

            if loop.time() >= next_poll:
                await _send_round(loop, sock, poll, targets, opts.port)
                next_poll = loop.time() + opts.repeat_every
    finally:
        sock.close()

    return [replies[ip] for ip in sorted(replies)]


async def find_first(listen_for: float = 3.0) -> Reply | None:
    """Discover for ``listen_for`` seconds and return the first device found."""
    found = await discover(DiscoveryOptions(listen_for=listen_for))
    return found[0] if found else None
