# The CBOR channel

> **The model's control link.** `libkp` speaks this channel as the second of
> the two links a `DeviceModel` holds ([Channels and data paths](11-channels-and-data-paths.md)):
> it opens the channel after the MIDI3 stream, writes the one item that asks
> for the state dump, folds the dump and every live push into the same state
> tree the stream feeds, and never writes anything else. That is how the morph
> position — a value the stream never carries — reaches `state.morph`. The
> codec and the dump exchange are exercised by the conformance vectors
> ([`../spec/vectors/cbor.json`](../spec/vectors/cbor.json)) and a sanitized
> real dump is replayed end to end in every language
> ([`../spec/captures`](../spec/captures), kind `cbor_stream`).
>
> Two tools read the channel on its own, built on the same open-and-ingest code
> the model uses: `StateSnapshot::fetch`, a one-shot read of the position and
> morph, and `CborSession`, a raw feed of every value the device pushes. The
> channel's wider command grammar — preset and library management, backup,
> firmware transfer — is still only characterized as a category, and no
> `libkp` code path drives it: the control link has no command queue.

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

Every item observed is a **`tag(1)`** wrapping a small array whose first
element (after an optional leading negative source-flag word) selects the shape:

| Shape | Selector | Notes |
|---|---|---|
| `tag(1)([1, addr, value])` | 1 | one parameter — a single change or reply |
| `tag(1)([2, base, v0, v1, …])` | 2 | a **consecutive run**: `base`, `base+1`, … |
| `tag(1)([4, addr, "text"])` | 4 | a string parameter |
| `tag(1)([5, addr, bytes])` | 5 | an opaque byte-string blob. Four appear in every dump; the library carries them as bytes and stores nothing from them |
| `tag(1)([-1, 1, addr, value])` | — | the single-parameter shape with a leading source flag, observed as `-1` on the device's live pushes of the beat pulse (15872) |

`addr` and `value` are plain CBOR integers, so their encoded width varies with
magnitude — this is not a fixed-size record format. The encoder writes the
**shortest integer head that fits**, which is what the device itself emits
(address 102405 as `1a 00 01 90 05`, 15953 as `19 3e 51`).

The integers are signed. No tracked value is negative, so a negative value is
dropped rather than reinterpreted, and a negative or oversized address is
malformed rather than wrapped into a plausible-looking one.

### Filler bytes

Between top-level items the channel emits runs of the padding byte **`0xC0`** — a
`tag(0)` head with no content, which is not well-formed CBOR on its own. Parsed
naively it swallows the following item, so the decoder skips it. A *genuine*
`tag(0)` is an RFC 8949 date/time whose content must be a text string, so a
`0xC0` is treated as filler only when the next head is **not** a text head,
leaving real datetimes intact.

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

| `addr` | Decomposition | Parameter |
|---|---|---|
| 1 … 4, 16 …, 32 … | `$00` × 128 + n | the rig, amp and cabinet string tags — the same numbers the `$03`/`$43` strings use |
| 119 | `$00` × 128 + `$77` | **Morph Position** — the one value only this channel carries |
| 15872 | `$7C` × 128 + 0 | Tempo / beat pulse — toggles 0 / 16383; pushed live with the `-1` source flag |
| 15953 | `$7C` × 128 + 81 | Tuner Strobe Phase — pushed at ~20 Hz; reads 0 while idle |
| 19200 … 19214 | `$96` × 128 + 0…14 | the bank's five-slot name preview |
| 100701 | ≥ 16384, extended | **current bank**, 0-based (see below) |
| 100702 | ≥ 16384, extended | **current rig slot** in the bank, 0-based |
| 102405 | ≥ 16384, extended | a free-running counter, pushed once a second |

## The state dump — and how to ask for it

The channel does **not** volunteer the device's stored state on connect. A
passive session — one that completes the handshake and preamble and then only
listens — sees just the live change events: the strobe phase, the beat pulse,
the counter, and whatever a knob turn or a rig change produces. It never learns
the starting value of anything it did not watch move.

Writing one item asks for the whole thing:

```
tag(1)([1, 102528, 1])        ->  c1 83 01 1a 00 01 90 80 01
```

`102528` is `state_dump_trigger_address`; the value is `1`. The write is
**non-mutating** — `102528` is a status flag the device already carries — so a
read-only client may send it safely, and it is the only item `libkp` ever
writes on this channel.

### The dump's shape

Established by observed experimentation against a Profiler Player (firmware
14.2.1), identical across repeated dumps:

- **202 items**: 95 consecutive runs (selector 2), 95 strings (selector 4),
  8 singles (selector 1) and 4 blobs (selector 5), carrying about 1500 numeric
  values — the string tags, the rig settings, the amplifier page, the eight
  effect slots, the bank preview, the morph, the position, the system page, and
  334 extended addresses among them.
- **Timing**: the first item 30–90 ms after the trigger, the last at ~340 ms,
  no gap inside the dump longer than 130 ms.
- **Two sections**, each closed by a run whose base is **100800**
  (`dump_end_address`): the system section first — identity, global settings,
  the position — closed by that run at item 32 (~80 ms); then the rig section,
  which opens with the position run again, closed by the same run at item 201
  of 202 (~340 ms). The model ends its dump phase when it has folded the
  **second** such run (`dump_end_runs = 2`), and falls back to `dump_settle_ms`
  (1 s) for a device that never sends it.
- **A side effect**: the trigger also makes the device push a full rig dump —
  about a thousand `$01` messages plus the strings — on the MIDI3 session. The
  dump is not free for the device, which is why the model's connect-time sync
  is the [request burst](11-channels-and-data-paths.md#the-request-lane) and
  not a second trigger.

While the dump streams, a value the device pushes live — on either wire — is
fresher than the dump's copy of it, so the model marks the address and drops the
dump's item for it ([the fold, rule 6](11-channels-and-data-paths.md#the-fold)).

### Current bank and rig

The dump carries the device's current position as two 0-based extended
addresses:

| address | meaning |
|---|---|
| **100701** (`0x1895D`) | current bank, 0-based |
| **100702** (`0x1895E`) | current rig slot in the bank, 0-based |

They arrive together inside one consecutive run — e.g.
`tag(1)([2, 100700, 0, 0, 2])` sets `100700=0`, `100701=0`, `100702=2` — so a
reader must **walk the whole run** rather than assume a fixed position; live
changes then push the elements singly. The dump also carries the current rig
name (string address 1) and the bank's five preview names, so the name and the
index agree.

The same two addresses are readable on the stream with a `$46` request, and the
device pushes a `$06` there whenever either changes — see
[the position report](05-sysex-nrpn.md#the-position-report). Both wires land in
the same two rows of the tree, and the fold's dedupe means a position reported
on both raises one `CurrentPosition`. What neither route needs is the
name-match ([Control model](08-control-model.md)): comparing the loaded rig name
against the [bank preview](09-parameter-registry.md) recovers the *slot* but not
the bank number, and fails when two slots share a name.

### The morph position

The dump carries the morph position at address 119, and the live pushes move it
at about 40 Hz while a morph ramps. This channel is the **only** route to it:
the position never appears on the MIDI3 stream and answers no request there
(the model refuses to ask, returning `Unreadable` without sending). See
[the morph](05-sysex-nrpn.md#the-morph). A model whose control link is off,
unavailable or lost never learns it; `state.channels.control` says which, and
`state.connection` reads `Degraded`.

### Credentials are redacted

The dump also volunteers the device's stored WiFi credentials in the clear — the
network name at address 200008 and its passphrase at 200009
(`sensitive_addresses`). Every reader in `libkp` — the fold, the snapshot
tooling — replaces a string at these addresses with `[redacted]`
(`redacted_placeholder`) before it is exposed, and nothing surfaces them. A
reader built on the raw codec must do the same. (The tree tracks neither
address, so in practice the fold stores nothing at either.)

## The tooling

Most callers want none of this directly: a `DeviceModel` opens the channel by
default, folds what it carries, and reports its health. Two tools exist for
reading the channel by itself, built on the same open-and-ingest path the
model's control link uses, and both pass the connection ledger so opening one
needs no spacing by the caller.

**A one-shot read of the dump** — open, trigger, read until the position and the
morph are known (or the default 3 s timeout), close:

```rust
use libkp::cbor::StateSnapshot;
let snap = StateSnapshot::fetch(ip).await?;
// snap.current_bank, snap.current_rig_slot   (Option<u16>, 0-based)
// snap.morph                                 (Option<u16>)
// snap.strings                               (address, text) pairs, redacted
```

```python
from libkp import fetch_state_snapshot
snap = await fetch_state_snapshot(ip)
# snap.current_bank, snap.current_rig_slot, snap.morph, snap.strings
```

```swift
let snap = try await StateSnapshot.fetch(host: host)
// snap.currentBank, snap.currentRigSlot, snap.morph, snap.strings
```

The items are folded into a scratch state tree through the same decoders a live
session uses, and the snapshot's fields are read off it — so the dump and a live
push cannot disagree about what an address means.

**A live raw feed** — `CborSession` opens the channel, asks for the dump, and
hands out every numeric `(address, value)` the device pushes until it is
closed. It is a tap for captures and protocol study: the values the tree tracks
and the ones it does not, in arrival order.

An application that holds a model should open neither beside it. The model
already has the dump and the live pushes, and a third session is one more
socket on a device that objects to connection churn. Their place is tooling
that has no model at all, or a model connected with `ControlPolicy::Off`.

## The relationship to MIDI3

The two channels overlap heavily, but **neither is a superset**. Each sends
something the other never does, which is why the model holds both — and the
device tolerates that: its fragility is about connection *churn*, not
concurrency, and two sessions coexist indefinitely.

Measured with both channels open across the same gestures:

| | MIDI3 | CBOR |
|---|---|---|
| Meter block (11 values, page `$7C`) | all eleven | **only the tuner strobe phase** — the other ten are never sent, not even at zero |
| Beat pulse (`$7C`/0) | pushed | pushed, with the `-1` source flag |
| Morph position (119) | never | in the dump, and pushed ~40 Hz while ramping |
| Morph button (`$00`/`$50`) | pushed | never |
| Amplifier page (`$0A`) | pushed | in the dump (24 numbers) and pushed on a rig load |
| Whole-state read | no — the 46-request burst | the dump |

So a CBOR-only client can drive a tuner strobe but not a level meter, and a
MIDI3-only client can show everything except where the morph fader sits. The
routing table's `wire` column keeps the two apart where they must be: the
control channel's copies of the strobe and the beat pulse are refused, because
the stream's meter frame is the one the tree wants, while the morph position is
the control channel's alone ([Channels and data paths](11-channels-and-data-paths.md#the-fold)).

In an idle session, most of what this channel pushes is a re-encoding of events
the device also broadcasts as MIDI3-framed SysEx. One event universe, two wire
formats:

| | `{369F50E7-…}` MIDI3 | `{774CDB9E-…}` CBOR |
|---|---|---|
| Framing | 4-byte frames → Kemper SysEx | bare CBOR items |
| Parameter change | `$01` at `<page>/<number>` | `tag(1)([1, addr, value])` |
| Consecutive run | `$02` multi-parameter | `tag(1)([2, base, …])` |
| String | `$03`/`$43` | `tag(1)([4, addr, "text"])` |
| Extended parameter | `$06` with a 5-byte address | the same integer address |
| Full state dump | the rig-load replay only | the selector-2/4 burst above |

The channels are independent sessions on independent sockets. Selecting one
during the handshake precludes the other for that connection — and the full
state dump is something only this channel is known to produce.

## Encodings do not cross over

A MIDI3-framed message written to this channel is not merely unrecognized, it is
**syntactically invalid CBOR**. The framing tag `0x14` decodes as CBOR unsigned
integer 20, and `0xF0` — the SysEx start byte — is a reserved simple value. A
decoder on the far side sees a malformed stream, not a foreign message.

The reverse is equally true: a MIDI3 unframer pointed at this channel produces
plausible-looking garbage, because CBOR payload bytes routinely take the values
`0x14`–`0x17` that the unframer reads as frame tags. Anything that speaks to
this channel must be a CBOR encoder and decoder from the start. The model's
control link therefore selects `{774CDB9E-…}` or nothing: a greeting that does
not offer it fails the open before any selection is written, and never falls
back to another protocol on that socket.

## What is not known

Explicitly out of scope of this document:

- The request and command grammar beyond the state-dump trigger — how to *ask*
  for anything else rather than observe what is pushed. No other request shape
  has been characterized, and the library's control link has no way to send
  one.
- The semantics of the leading source-flag word. It has been observed as `-1`
  on the device's own live pushes of the beat pulse; what other values mean,
  and what the flag denotes, is uncharacterized.
- The content of the `[5, addr, bytes]` blobs. They are carried as opaque bytes
  and never interpreted.
- Every device-management operation: preset and rig management, library
  organization, backup, and firmware transfer.

Do not infer any of these from the shapes above.

## The neighbouring channel

`{2490272E-CD92-4DBA-AE32-E8AF37ED3B0A}` also accepts the handshake. It pushes
nothing unsolicited; on session open it emits a single 11-byte value and then
stays silent. That value is not MIDI3, not SysEx, and not a valid CBOR item.
Its role is described in the [handshake](03-handshake.md) as request/response,
but its grammar is uncharacterized and it is not opened by anything in `libkp`.

## Sources

The existence, wire shape, address space, filler convention, the state-dump
trigger, the dump's two sections and end marker, the live feeds and the
current-position addresses of the CBOR channel were characterized through
observed experimentation, from the outside only. See
[../CREDITS.md](../CREDITS.md).
