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
    "RigLoadRequiresNavigatorError",
    "RequestError",
    "RequestDisconnectedError",
    "RequestTimeoutError",
    "RequestUnreadableError",
    "ChannelError",
    "ChannelOffError",
    "ChannelTooSoonError",
    "ChannelDisconnectedError",
    "ChannelSessionError",
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


class RigLoadRequiresNavigatorError(CommandError):
    """A command that would load a rig was refused before any byte was written.

    Rig loads -- the up/down and slot-load Control Changes
    (:data:`libkp._generated.RIG_LOAD_CONTROLLERS`), a Program Change, and the
    Bank Select pair that only exists to qualify one -- go through the model's
    Navigator alone (:meth:`~libkp.model.DeviceModel.navigate_to`,
    :meth:`~libkp.model.DeviceModel.step_rig`,
    :meth:`~libkp.model.DeviceModel.step_bank`,
    :meth:`~libkp.model.DeviceModel.select_slot`), which serialises them so two
    can never overlap on the wire: an overlapping load leaves the device on a
    delayed fuse that only a power cycle clears. The bank preselect (CC47) loads
    nothing and is not refused.
    """

    def __init__(self, what: str) -> None:
        super().__init__(
            f"{what} would load a rig; use the model's Navigator (navigate_to, "
            f"step_rig, step_bank, select_slot) so loads cannot overlap"
        )
        #: What was refused, for the message.
        self.what = what


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


class RequestError(LibKPError):
    """A read request (``request_param`` and its siblings) could not be answered."""


class RequestDisconnectedError(RequestError):
    """The stream is not open, so there is nothing to ask."""

    def __init__(self) -> None:
        super().__init__("device model is disconnected; nothing to request from")


class RequestTimeoutError(RequestError):
    """No reply arrived inside :data:`libkp._generated.REQUEST_TIMEOUT_MS`.

    The request is abandoned, never retried: the device silently ignores an
    address it cannot answer, and asking again costs it more than it costs the
    caller to do without.
    """

    def __init__(self, address: int, seconds: float) -> None:
        super().__init__(
            f"no reply to the request at address {address} after {seconds * 1000:.0f} ms"
        )
        #: The flat address that was asked for.
        self.address = address
        self.seconds = seconds


class RequestUnreadableError(RequestError):
    """The address is one the stream cannot answer, so nothing was sent.

    The morph position is the case: the table routes it to the control channel
    alone, and a ``$41`` for it draws no reply -- waiting out a timeout would
    only report the same thing later.
    """

    def __init__(self, address: int) -> None:
        super().__init__(f"address {address} is not readable over the stream")
        self.address = address


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------


class ChannelError(LibKPError):
    """The control channel could not be (re)opened on request."""


class ChannelOffError(ChannelError):
    """The connect options set the control policy to off; nothing to reopen."""

    def __init__(self) -> None:
        super().__init__("the control channel is off by policy")


class ChannelTooSoonError(ChannelError):
    """The last control open was inside
    :data:`libkp._generated.CONTROL_REOPEN_MIN_GAP_MS`; the device is not to be
    asked for a second control socket that close to the first."""

    def __init__(self, remaining: float) -> None:
        super().__init__(
            f"the control channel was opened too recently; try again in {remaining:.0f} s"
        )
        #: Seconds until a reopen would be allowed.
        self.remaining = remaining


class ChannelDisconnectedError(ChannelError):
    """The stream is not open, and the control channel never runs without it."""

    def __init__(self) -> None:
        super().__init__("device model is disconnected; the control channel needs the stream")


class ChannelSessionError(ChannelError):
    """The control open itself failed; ``cause`` is the :class:`SessionError`."""

    def __init__(self, cause: SessionError) -> None:
        super().__init__(f"the control channel could not be opened: {cause}")
        self.cause = cause
