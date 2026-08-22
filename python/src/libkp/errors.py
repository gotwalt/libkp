"""Error types raised by :mod:`libkp`.

Every exception derives from :class:`LibKPError`, so a caller can catch the
whole family with one ``except``.
"""

from __future__ import annotations

__all__ = [
    "LibKPError",
    "ParseError",
    "TooShortError",
    "FieldOverrunError",
    "DiscoverError",
    "SessionError",
    "ConnectError",
    "TimeoutErrorLibKP",
    "ConnectionClosedError",
    "ProtocolRejectedError",
    "CommandError",
    "DisconnectedError",
    "UnknownSlotError",
]


class LibKPError(Exception):
    """Base class for every error raised by libkp."""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


class ParseError(LibKPError):
    """A malformed or truncated payload."""


class TooShortError(ParseError):
    """A payload ended before the minimum number of bytes was available."""

    def __init__(self, need: int, got: int) -> None:
        super().__init__(f"payload too short: need at least {need} bytes, got {got}")
        self.need = need
        self.got = got


class FieldOverrunError(ParseError):
    """A length-prefixed field claimed more bytes than the payload holds."""

    def __init__(self, offset: int, length: int, remaining: int) -> None:
        super().__init__(
            f"field at offset {offset} claims length {length} but only {remaining} bytes remain"
        )
        self.offset = offset
        self.length = length
        self.remaining = remaining


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


class DiscoverError(LibKPError):
    """Discovery could not be carried out (socket, bind, or send failure)."""


class PortUnavailableError(DiscoverError):
    """The discovery port could not be taken exclusively.

    libkp requires sole ownership of UDP :data:`~libkp.protocol.PORT` for as long
    as a session is active. The device answers a poll only on that port, and the
    operating system hands each reply to exactly one of the sockets bound to it,
    so a second listener silently swallows replies rather than duplicating them.
    Binding exclusively turns that into this error at start-up instead of a
    device that intermittently "cannot be found".

    The usual holder is other Kemper software on the same machine — Rig Manager
    keeps the port open for its whole run — which must be quit first.
    """

    def __init__(self, port: int, cause: OSError) -> None:
        super().__init__(
            f"UDP port {port} is already held by another application. libkp needs "
            f"exclusive use of it while a session is active; quit any other Kemper "
            f"software (Rig Manager keeps this port open) and try again. ({cause})"
        )
        #: The port that could not be acquired.
        self.port = port
        #: The underlying :class:`OSError` from :func:`socket.socket.bind`.
        self.cause = cause


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


class SessionError(LibKPError):
    """Base class for TCP session and handshake failures."""


class ConnectError(SessionError):
    """The TCP connection to the device could not be established."""

    def __init__(self, addr: tuple[str, int], source: BaseException | None = None) -> None:
        super().__init__(f"failed to connect to {addr[0]}:{addr[1]}: {source}")
        self.addr = addr
        self.source = source


class TimeoutErrorLibKP(SessionError):
    """A protocol phase did not complete inside its deadline."""

    def __init__(self, phase: str, seconds: float) -> None:
        super().__init__(f"timed out waiting for {phase} after {seconds * 1000:.0f} ms")
        self.phase = phase
        self.seconds = seconds


class ConnectionClosedError(SessionError):
    """The device closed the connection."""

    def __init__(self, detail: str = "connection closed by device") -> None:
        super().__init__(detail)


class ProtocolRejectedError(SessionError):
    """The device answered the protocol selection with a rejection line."""

    def __init__(self, name: str, detail: str | None = None) -> None:
        msg = f'device rejected protocol "{name}"'
        if detail:
            msg += f": {detail}"
        super().__init__(msg)
        self.name = name
        self.detail = detail


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


class CommandError(LibKPError):
    """A command could not be issued."""


class DisconnectedError(CommandError):
    """The ingest task has ended, so the command channel is closed."""

    def __init__(self) -> None:
        super().__init__("device model is disconnected; command channel closed")


class UnknownSlotError(CommandError):
    """An effect-slot name did not match A/B/C/D/X/MOD/DLY/REV."""

    def __init__(self, slot: str) -> None:
        super().__init__(f"unknown effect slot {slot!r}; use A B C D X MOD DLY REV")
        self.slot = slot
