# MIDI3 framing

Once the [handshake](03-handshake.md) has accepted
`{369F50E7-750B-459A-BAEE-85ADD3F3798D}` and the 8-byte preamble is written, the
TCP socket carries a continuous stream of **4-byte frames**. Framing is
identical in both directions.

```
┌──────┬──────┬──────┬──────┐
│ tag  │  b0  │  b1  │  b2  │     one frame — always exactly 4 bytes
└──────┴──────┴──────┴──────┘
```

The tag says how many of the three payload bytes are valid and whether the
message continues.

| Tag | Meaning | Valid payload bytes |
|---|---|---|
| `0x14` | continuation — more frames follow | 3 (`b0 b1 b2`) |
| `0x15` | final frame of the message | 1 (`b0`) |
| `0x16` | final frame of the message | 2 (`b0 b1`) |
| `0x17` | final frame of the message | 3 (`b0 b1 b2`) |

Constants `tag_continuation`, `tag_final_1`, `tag_final_2`, `tag_final_3` in
[`../spec/protocol.toml`](../spec/protocol.toml) `[midi3]`.

Concatenating the valid bytes of a continuation run plus its terminating final
frame yields one **raw MIDI message** — in practice almost always a Kemper SysEx
(`F0 00 20 33 … F7`), occasionally a Control Change. The framing layer knows
nothing about MIDI; it is a pure byte-transport with message boundaries.

Padding bytes after the valid ones in a final frame are written as `0x00` and
must be ignored on receive. The frame is always 4 bytes wide regardless.

## Unframing

```
buffer := []
loop:
    read 4 bytes                       # if fewer are available, keep them pending
    tag, b0, b1, b2 := those bytes
    match tag:
        0x14 -> buffer += [b0, b1, b2]
        0x15 -> buffer += [b0];          emit(buffer); buffer := []
        0x16 -> buffer += [b0, b1];      emit(buffer); buffer := []
        0x17 -> buffer += [b0, b1, b2];  emit(buffer); buffer := []
        _    -> resynchronize
```

A TCP read returns an arbitrary number of bytes, so an unframer is inherently
stateful: it holds a **partial frame** (0–3 bytes) and a **partial message**
across reads. The conformance vectors report the leftover count as `pending`.

### Worked example — unframing

From [`../spec/vectors/midi3.json`](../spec/vectors/midi3.json), the first
`unframe` case. Twenty-eight bytes off the socket:

```
14f00020143302001406000014000620140500001400001015f70000
```

Split into 4-byte frames:

```
frame   tag   payload      contributes
─────────────────────────────────────────────
  0     14    f0 00 20     f0 00 20        (3 valid, continues)
  1     14    33 02 00     33 02 00
  2     14    06 00 00     06 00 00
  3     14    00 06 20     00 06 20
  4     14    05 00 00     05 00 00
  5     14    00 00 10     00 00 10
  6     15    f7 00 00     f7              (1 valid, final; 00 00 is padding)
```

Concatenated:

```
f0 00 20 33 02 00 06 00 00 00 06 20 05 00 00 00 00 10 f7
```

That is a complete Kemper SysEx: manufacturer `00 20 33`, product `02`, device
`00`, function `$06` (Extended Parameter Change), instance `00`, then a 5-byte
address `00 00 06 20 05` = 102405 and a 5-byte value `00 00 00 00 10` = 16. See
[SysEx / NRPN dialect](05-sysex-nrpn.md) for the message grammar.

### Worked example — a partial read

The third `unframe` case shows what a mid-stream read looks like:

```
stream:   14 f0 00 20   17 f7 00 00   14 aa
```

The first two frames complete a message — `f0 00 20 f7 00 00` — and the trailing
`14 aa` is only two bytes of a third frame. It is **pending**: hold it and
prepend it to the next read. Discarding a partial frame desynchronizes the
stream permanently.

## Framing

```
frame(message):
    out := []
    while len(remaining) > 3:
        out += [0x14] + remaining[0:3]
        remaining = remaining[3:]
    n := len(remaining)                 # 1, 2, or 3
    out += [0x14 + n] + remaining + [0x00] * (3 - n)
```

Two properties follow, and both matter:

- **The last frame always carries a final tag**, even when the message length is
  an exact multiple of three. A 6-byte message emits one `0x14` frame and one
  `0x17` frame — never two `0x14` frames.
- `0x14 + n` produces `0x15`/`0x16`/`0x17` for n = 1/2/3, which is why the tag
  values are contiguous.

### Worked example — framing

From the `frame` cases in [`../spec/vectors/midi3.json`](../spec/vectors/midi3.json).
Turning module REV on — a `$01` Single Parameter Change writing 1 to page `$3D`,
number 3:

```
message (13 bytes)
f0 00 20 33 00 7f 01 00 3d 03 00 01 f7

13 = 4×3 + 1  →  four continuation frames and a 0x15 final frame

14 f0 00 20
14 33 00 7f
14 01 00 3d
14 03 00 01
15 f7 00 00

on the wire
14f000201433007f1401003d1403000115f70000        (20 bytes)
```

The other three cases cover the length remainders:

| Message | Length | Framed | Last tag |
|---|---|---|---|
| `f0002033027f7e004002230ff7` (beacon) | 13 | `14f000201433027f147e00401402230f15f70000` | `0x15` |
| `f0002033f7` | 5 | `14f000201633f700` | `0x16` |
| `f000203301f7` | 6 | `14f00020173301f7` | `0x17` |

Overhead is 4 bytes per 3 payload bytes — a uniform 33 %, plus up to 2 padding
bytes per message.

## Round-trip invariant

`unframe(frame(m)) == [m]` with `pending == 0`, for every message `m`. The
conformance suites in all three languages assert exactly this over the vector
set. They additionally replay the `midi3_stream` fixtures in
[`../spec/captures`](../spec/captures) — sanitized recordings of real traffic,
from observed experimentation — which exercise the unframer against genuine
message sequences rather than hand-built ones. See
[Versioning & compatibility](10-versioning-and-compatibility.md).

## Resynchronization

An unexpected tag byte means the reader has lost frame alignment. There is no
in-band resync marker, so the only reliable recovery is to drop the connection
and re-handshake. In practice misalignment does not occur on a healthy stream —
it is a symptom of a bug in the reader's pending-byte handling, most often
discarding a partial frame at a read boundary.

Do not attempt to resynchronize by scanning for `0x14`: payload bytes routinely
take that value (`0x14` is a legal SysEx data byte), so a scan will lock onto
false frame starts. The unframer that latches onto payload bytes rather than
tags produces plausible-looking garbage indefinitely.

## Sources

MIDI3 framing — the 4-byte frame, the tag semantics, and the padding convention —
was characterized through observed experimentation. See
[../CREDITS.md](../CREDITS.md).
