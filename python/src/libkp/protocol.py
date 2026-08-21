"""The "TagStream" wire encoding and the discovery poll packet.

Discovery happens over UDP on :data:`PORT`. The client broadcasts a fixed poll
packet; Profilers on the LAN reply on the same port.

Wire format — the TagStream
---------------------------

A payload is an optional 4-byte ASCII header followed by a series of
length-prefixed fields. Each field is ``[len: u8][content: len-1 bytes]``, where
``len`` is **inclusive of the length byte itself** (so an empty field is
``0x00``, and the content length is ``len - 1``). A ``0x00`` byte terminates the
stream.

The 34-byte poll request is::

    "DSCV"  0x16 "MAC#00:00:00:00:00:00"  0x07 "POLL:)"  0x00

The transport envelope described here is documented from observed
experimentation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import _generated as gen
from .errors import FieldOverrunError, TooShortError

__all__ = [
    "PORT",
    "DISCOVERY_PORT",
    "DSCV_HEADER",
    "PLACEHOLDER_MAC",
    "TagStream",
    "build_poll_request",
    "push_field",
]

#: The single UDP/TCP port the Profiler uses for both discovery and sessions.
PORT: int = gen.PORT

#: Alias of :data:`PORT`, for callers that think of it as the discovery port.
DISCOVERY_PORT: int = gen.PORT

#: 4-byte header that opens a discovery poll request.
DSCV_HEADER: bytes = gen.DISCOVERY_HEADER.encode("ascii")

#: An all-zero client MAC; the device replies to it just the same.
PLACEHOLDER_MAC: str = gen.POLL_PLACEHOLDER_MAC


def push_field(out: bytearray, content: bytes) -> None:
    """Append one length-prefixed field. The length byte is inclusive of itself."""
    if len(content) >= 0xFF:
        raise ValueError(f"TagStream field too long: {len(content)} bytes")
    out.append(len(content) + 1)
    out.extend(content)


def build_poll_request(mac: str = PLACEHOLDER_MAC) -> bytes:
    """Build the discovery poll request packet.

    ``mac`` is the client's own MAC address string. The device appears to track
    it but replies regardless, so the all-zero placeholder works fine for
    probing.
    """
    out = bytearray(DSCV_HEADER)
    push_field(out, f"{gen.POLL_MAC_PREFIX}{mac}".encode("ascii"))
    push_field(out, gen.POLL_PAYLOAD.encode("ascii"))
    out.append(0x00)  # stream terminator
    return bytes(out)


def _is_ascii_graphic(b: int) -> bool:
    return 0x21 <= b <= 0x7E


def _detect_header(buf: bytes) -> tuple[bytes | None, int]:
    """Decide whether ``buf`` opens with a 4-byte ASCII header."""
    if len(buf) >= 5 and all(_is_ascii_graphic(b) for b in buf[:4]):
        length = buf[4]
        if length == 0 or 4 + length <= len(buf):
            return buf[:4], 4
    return None, 0


@dataclass(frozen=True)
class TagStream:
    """A decoded TagStream payload."""

    #: Leading 4-byte ASCII header, if the payload looked like it had one.
    header: bytes | None = None
    #: Length-prefixed fields, content only (length byte stripped).
    fields: list[bytes] = field(default_factory=list)

    @classmethod
    def parse(cls, buf: bytes) -> "TagStream":
        """Best-effort parse of a received payload.

        If the first four bytes are printable ASCII and the fifth is a plausible
        field length, they are taken as a header; otherwise fields are read from
        offset 0.
        """
        if not buf:
            raise TooShortError(need=1, got=0)

        header, off = _detect_header(buf)
        fields: list[bytes] = []

        while off < len(buf):
            length = buf[off]
            if length == 0:
                break  # terminator / empty field
            start = off + 1
            end = start + (length - 1)
            if end > len(buf):
                raise FieldOverrunError(
                    offset=off, length=length, remaining=len(buf) - off - 1
                )
            fields.append(buf[start:end])
            off = end

        return cls(header=header, fields=fields)

    def key_values(self) -> list[tuple[str, bytes]]:
        """Split each field into its 4-char ASCII key and value bytes.

        Discovery-reply fields are ``[4-char key][value]`` (e.g. ``NAME``,
        ``SER#``). Fields whose first four bytes are not printable are skipped.
        """
        out: list[tuple[str, bytes]] = []
        for f in self.fields:
            if len(f) >= 4 and all(_is_ascii_graphic(b) for b in f[:4]):
                out.append((f[:4].decode("ascii"), f[4:]))
        return out

    def get(self, key: str) -> bytes | None:
        """The value of the first field whose 4-char key is ``key``."""
        for k, v in self.key_values():
            if k == key:
                return v
        return None
