# The CBOR channel

> **Experimental — not implemented in `libkp`.**
> This document records what is known about the device's native control channel
> from the outside. It is deliberately incomplete: only the wire shape has been
> characterized, not the command grammar. Nothing here is exercised by the
> conformance vectors, and no `libkp` code path speaks it.
>
> If you need to read or write parameters, use the MIDI3 stream
> ([04](04-midi3-framing.md), [05](05-sysex-nrpn.md)). It covers the entire
> control and monitoring surface this library exposes.

## What it is

`{774CDB9E-74ED-4740-AF09-AC96B3A69A11}` is one of the four protocol GUIDs the
Profiler offers during the [handshake](03-handshake.md). It accepts selection
and completes the handshake exactly like the MIDI3 stream — including the
[8-byte session preamble](03-handshake.md#step-4--the-session-preamble) — and
then streams something entirely different: **CBOR** (RFC 8949), not MIDI3
frames.

This is the device's own control link, and the route to the operations the MIDI
surface has no vocabulary for: preset and rig management, library and backup
operations, and firmware transfer. Those are visible only as a category; none of
their message shapes are documented here.

## Wire shape

After the preamble the socket carries a **continuous sequence of top-level CBOR
items** with no outer framing, length prefix, or delimiter — the CBOR
self-description supplies the boundaries. A decoder reads one item at a time
from the byte stream and hands each to the application; a well-formed capture
decodes to a whole number of items with no residue.

Every item observed has the same shape: a **`tag(1)`** wrapping a small array of
integers.

| Shape | Notes |
|---|---|
| `tag(1)([1, addr, value])` | the common case — one parameter event |
| `tag(1)([e, 1, addr, value])` | a 4-element variant with a leading element (observed as `-1`); its meaning is not characterized |

`addr` and `value` are plain CBOR integers, so their encoded width varies with
magnitude — this is not a fixed-size record format.

## Addressing

The address is the **same NRPN address space** described in
[SysEx / NRPN dialect](05-sysex-nrpn.md), expressed as a single integer:

```
addr = page * 128 + number
```

which is the identical formula the `$06`/`$07` extended messages use for their
5-byte addresses. Extended addresses — those at or above 16384, with no
`(page, number)` decomposition — appear in this space too, so the channel's
address field spans the full extended range rather than just the 14-bit part.

Worked examples, decoded against known addresses:

| `addr` | Decomposition | Parameter |
|---|---|---|
| 15872 | `$7C` × 128 + 0 | Tempo / beat pulse — toggles 0 / 16383 |
| 15953 | `$7C` × 128 + 81 | Tuner Strobe Phase — reads 0 while idle |
| 102405 | ≥ 16384, extended | an extended-address parameter with no page/number form |

Address 102405 also appears as an `$06` Extended Parameter Change on the MIDI3
stream — see the worked unframing example in
[MIDI3 framing](04-midi3-framing.md). The two channels are addressing the same
thing.

## The relationship to MIDI3

In an idle session, everything this channel pushes is a re-encoding of events
the device already broadcasts as MIDI3-framed SysEx. One event universe, two
wire formats:

| | `{369F50E7-…}` MIDI3 | `{774CDB9E-…}` CBOR |
|---|---|---|
| Framing | 4-byte frames → Kemper SysEx | bare CBOR items |
| Parameter change | `$01` at `<page>/<number>` | `tag(1)([1, addr, value])` |
| Beat pulse | `$01` page `$7C`, number 0 | the 4-element variant at addr 15872 |
| Extended parameter | `$06` with a 5-byte address | the same integer address |
| Realtime status block | `$02` at `$7C`/`$4E` | not observed |

The channels are independent sessions on independent sockets. Selecting one
during the handshake precludes the other for that connection.

## Encodings do not cross over

A MIDI3-framed message written to this channel is not merely unrecognized, it is
**syntactically invalid CBOR**. The framing tag `0x14` decodes as CBOR unsigned
integer 20, and `0xF0` — the SysEx start byte — is a reserved simple value. A
decoder on the far side sees a malformed stream, not a foreign message.

The reverse is equally true: a MIDI3 unframer pointed at this channel produces
plausible-looking garbage, because CBOR payload bytes routinely take the values
`0x14`–`0x17` that the unframer reads as frame tags. Anything that speaks to
this channel must be a CBOR encoder and decoder from the start.

## What is not known

Explicitly out of scope of this document:

- The request and command grammar — how to *ask* for something rather than
  observe what is pushed. No request shape has been characterized.
- Whether the channel needs a subscription or handshake step analogous to the
  MIDI3 [beacon](05-sysex-nrpn.md#beacon-and-sense) before it will answer.
- The semantics of the leading element in the 4-element variant.
- The realtime status block's representation, if it has one here.
- Every device-management operation: preset and rig management, library
  organization, backup, and firmware transfer.

Do not infer any of these from the shapes above.

## The neighbouring channel

`{2490272E-CD92-4DBA-AE32-E8AF37ED3B0A}` also accepts the handshake. It pushes
nothing unsolicited; on session open it emits a single 11-byte value and then
stays silent. That value is not MIDI3, not SysEx, and not a valid CBOR item.
Its role is described in the [handshake](03-handshake.md) as request/response,
but its grammar is uncharacterized and it is likewise not implemented.

## Sources

The existence, wire shape, and address space of the CBOR channel were
characterized through observed experimentation, from the outside only. See
[../CREDITS.md](../CREDITS.md).
