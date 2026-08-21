"""MIDI3 stream framing — the wrapper that carries MIDI over the TCP streaming
protocol.

The stream is a sequence of 4-byte frames, ``[tag][b0][b1][b2]``:

- ``0x14`` — continuation group: all 3 bytes are valid, more groups follow.
- ``0x15`` — final group, 1 valid byte  (b0), message ends.
- ``0x16`` — final group, 2 valid bytes (b0, b1), message ends.
- ``0x17`` — final group, 3 valid bytes (b0, b1, b2), message ends.

Concatenating the valid bytes of the groups making up a message yields a raw
MIDI message (typically a Kemper SysEx ``F0 00 20 33 … F7``). Example — a real
19-byte SysEx::

    14 f0 00 20 | 14 33 02 00 | 14 06 00 00 | 14 00 06 20
    14 05 00 00 | 14 00 00 10 | 15 f7 00 00
      -> F0 00 20 33 02 00 06 00 00 00 06 20 05 00 00 00 00 10 F7

The framing is documented from observed experimentation.
"""

from __future__ import annotations

from . import _generated as gen

__all__ = [
    "TAG_CONT",
    "TAG_FINAL_1",
    "TAG_FINAL_2",
    "TAG_FINAL_3",
    "Unframer",
    "frame",
    "is_kemper_sysex",
]

#: Continuation frame tag: 3 valid bytes, message continues.
TAG_CONT: int = gen.MIDI3_TAG_CONTINUATION
#: Final frame tag carrying 1 valid byte.
TAG_FINAL_1: int = gen.MIDI3_TAG_FINAL_1
#: Final frame tag carrying 2 valid bytes.
TAG_FINAL_2: int = gen.MIDI3_TAG_FINAL_2
#: Final frame tag carrying 3 valid bytes.
TAG_FINAL_3: int = gen.MIDI3_TAG_FINAL_3

_VALID_BYTES = {
    TAG_CONT: 3,
    TAG_FINAL_1: 1,
    TAG_FINAL_2: 2,
    TAG_FINAL_3: 3,
}

_MANUFACTURER = bytes(gen.MANUFACTURER_ID)


class Unframer:
    """A streaming de-framer. Feed it raw bytes; it yields complete MIDI messages."""

    __slots__ = ("_partial", "_current")

    def __init__(self) -> None:
        # Leftover bytes that don't yet complete a 4-byte frame.
        self._partial = bytearray()
        # Bytes accumulated for the in-progress MIDI message.
        self._current = bytearray()

    def push(self, data: bytes) -> list[bytes]:
        """Feed raw stream bytes; return the MIDI messages completed by this input.

        A frame whose tag is not ``0x14..0x17`` is treated as a desync: the
        in-progress message is discarded and one byte skipped to resync.
        """
        self._partial.extend(data)
        out: list[bytes] = []

        while len(self._partial) >= 4:
            tag = self._partial[0]
            valid = _VALID_BYTES.get(tag)
            if valid is None:
                # Unknown tag — resync by dropping one byte.
                del self._partial[0:1]
                self._current.clear()
                continue

            payload = self._partial[1:4]
            del self._partial[0:4]
            self._current.extend(payload[:valid])
            if tag != TAG_CONT:
                out.append(bytes(self._current))
                self._current.clear()
        return out

    def pending(self) -> int:
        """Bytes buffered but not yet forming a complete message."""
        return len(self._partial) + len(self._current)

    def reset(self) -> None:
        """Drop every buffered byte."""
        self._partial.clear()
        self._current.clear()


def frame(msg: bytes) -> bytes:
    """Frame a raw MIDI message into MIDI3 wire format (inverse of :class:`Unframer`).

    Splits into 3-byte groups: full non-final groups get ``0x14``; the final
    group gets ``0x15``/``0x16``/``0x17`` for 1/2/3 valid bytes (padded to 3).
    """
    out = bytearray()
    n_groups = max(-(-len(msg) // 3), 1)
    for i in range(0, len(msg), 3):
        chunk = msg[i : i + 3]
        last = (i // 3) + 1 == n_groups
        tag = TAG_CONT + len(chunk) if last else TAG_CONT
        out.append(tag)
        out.extend(chunk)
        out.extend(b"\x00" * (3 - len(chunk)))
    return bytes(out)


def is_kemper_sysex(msg: bytes) -> bool:
    """True if ``msg`` is a Kemper SysEx (``F0 00 20 33 … F7``)."""
    return len(msg) >= 5 and msg[0] == 0xF0 and msg[1:4] == _MANUFACTURER and msg[-1] == 0xF7
