# The CBOR channel

> **Partially implemented.** `libkp` speaks this channel for two purposes: the
> one-shot **state-dump snapshot** that reads the device's current bank, rig and
> morph, and a **live session** (`CborSession`) that holds the channel open and
> streams every value the device pushes
> ([`cbor`](../rust/src/cbor.rs) / [`libkp.cbor`](../python/src/libkp/cbor.py) /
> [`Cbor`](../swift/Sources/LibKP/Cbor.swift)). The codec (encode + streaming
> decode) and the dump exchange are exercised by the conformance vectors
> ([`../spec/vectors/cbor.json`](../spec/vectors/cbor.json)). The channel's wider
> command grammar — preset and library management, backup, firmware transfer — is
> still only characterized as a category and no `libkp` code path drives it.
>
> For live control and monitoring, use the MIDI3 stream
> ([04](04-midi3-framing.md), [05](05-sysex-nrpn.md)); it covers the whole
> realtime surface. The one thing it cannot report is the device's current
> position, which is what this channel is used for.

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

Every parameter item observed is a **`tag(1)`** wrapping a small array whose
first element (after an optional leading negative source-flag word) selects the
shape:

| Shape | Selector | Notes |
|---|---|---|
| `tag(1)([1, addr, value])` | 1 | one parameter — a single change or reply |
| `tag(1)([2, base, v0, v1, …])` | 2 | a **consecutive run**: `base`, `base+1`, … |
| `tag(1)([4, addr, "text"])` | 4 | a string parameter |
| `tag(1)([-1, 1, addr, value])` | — | the single-parameter shape with a leading source flag (observed as `-1`) |

`addr` and `value` are plain CBOR integers, so their encoded width varies with
magnitude — this is not a fixed-size record format. The encoder writes the
**shortest integer head that fits**, which is what the device itself emits
(address 102405 as `1a 00 01 90 05`, 15953 as `19 3e 51`).

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
| 15872 | `$7C` × 128 + 0 | Tempo / beat pulse — toggles 0 / 16383 |
| 15953 | `$7C` × 128 + 81 | Tuner Strobe Phase — reads 0 while idle |
| 102405 | ≥ 16384, extended | an extended-address parameter with no page/number form |
| 100701 | ≥ 16384, extended | **current bank**, 0-based (see below) |
| 100702 | ≥ 16384, extended | **current rig slot** in the bank, 0-based |

## The state dump — and how to ask for it

The channel does **not** volunteer the device's stored state on connect. A
passive session — one that completes the handshake and preamble and then only
listens — sees just the live change events physical knob turns produce; it never
learns the starting value of anything it did not watch move.

Writing one item asks for the whole thing:

```
tag(1)([1, 102528, 1])        ->  c1 83 01 1a 00 01 90 80 01
```

`102528` is `state_dump_trigger_address`; the value is `1`. The device answers
with its **entire parameter state** as a burst of selector-`2` runs and
selector-`4` strings. The write is **non-mutating** — `102528` is a status flag
the device already carries — so a read-only client may send it safely. This one
item is sufficient by itself.

### Current bank and rig

The dump carries the device's current position as two 0-based extended
addresses:

| address | meaning |
|---|---|
| **100701** (`0x1895D`) | current bank, 0-based |
| **100702** (`0x1895E`) | current rig slot in the bank, 0-based |

At session open they arrive together inside one consecutive run — e.g.
`tag(1)([2, 100700, 0, 0, 2])` sets `100700=0`, `100701=0`, `100702=2` — so a
reader must **walk the whole run** rather than assume a fixed position; live
changes then push the elements singly. The dump also carries the current rig
name (string address 1) and the bank's five preview names, so the name and the
index agree.

This is the route to these values *before* a streaming session exists. Once one
is open the same two addresses are readable there, with a `$46` request, and the
device pushes a `$06` whenever either changes — see
[the position report](05-sysex-nrpn.md#the-position-report). What neither route
needs is the name-match ([Control model](08-control-model.md)): comparing the
loaded rig name against the [bank preview](09-parameter-registry.md) recovers
the *slot* but not the bank number, and fails when two slots share a name.

### The morph position

The dump also carries the morph position at address 119, which `StateSnapshot`
reads out alongside the indices. This channel is the **only** route to it: the
position never appears on the MIDI3 stream and answers no request there. See
[the morph](05-sysex-nrpn.md#the-morph).

### Credentials are redacted

The dump also volunteers the device's stored WiFi credentials in the clear — the
network name at address 200008 and its passphrase at 200009 (`sensitive_addresses`).
The snapshot reader replaces any string value at these addresses with
`[redacted]` (`redacted_placeholder`) before it is exposed, and nothing in
`libkp` surfaces them. A reader built on the raw [`cbor`](../rust/src/cbor.rs)
codec must do the same.

## The snapshot API

One call opens a fresh, short-lived CBOR session, sends the trigger, reads the
current position out of the dump, and closes:

```rust
use libkp::cbor::StateSnapshot;
let snap = StateSnapshot::fetch(ip).await?;
// snap.current_bank, snap.current_rig_slot   (both Option<u16>, 0-based)
```

```python
from libkp import fetch_state_snapshot
snap = await fetch_state_snapshot(ip)
# snap.current_bank, snap.current_rig_slot     (both int | None, 0-based)
```

```swift
let snap = try await StateSnapshot.fetch(ip)
// snap.currentBank, snap.currentRigSlot        (both UInt16?, 0-based)
```

**Space it from any other connection.** It opens its own socket, independent of
a [`DeviceModel`](07-realtime-status.md). Concurrent sessions are fine — see
[Channels and data paths](11-channels-and-data-paths.md) — but connection
*churn* is not. It refuses to greet — or resets — a session opened too soon
after a prior socket closed: a MIDI3 session
opened immediately after the snapshot's socket closes times out waiting for the
greeting, while spacing them by about a second connects cleanly. So a controller
that also wants live meters should fetch the snapshot first, **wait at least the
connection cooldown** (`connection_cooldown_ms`, exposed as `CONNECTION_COOLDOWN`
in Rust/Python and `Session.connectionCooldown` in Swift), let that socket close,
then open the MIDI3 model — and feed the result in with
`DeviceModel::set_current_position` (Rust) / `set_current_position` (Python) /
`setCurrentPosition` (Swift), which folds the indices into `DeviceState`
(`current_bank` / `current_rig_slot`) and emits a `CurrentPosition` event.

A controller that opens a streaming session anyway does not need any of that:
`refresh_position` reads the same two indices over the session it already has,
and `connect` runs it as part of the initial sync.

## The relationship to MIDI3

The two channels overlap heavily, but **neither is a superset**. Each sends
something the other never does, so a client that wants the whole device opens
both — which the device tolerates: its fragility is about connection *churn*,
not concurrency, and two read-only sessions coexist indefinitely.

Measured with both channels open across the same gestures:

| | MIDI3 | CBOR |
|---|---|---|
| Meter block (11 values, page `$7C`) | all eleven | **only the tuner strobe phase** — the other ten are never sent, not even at zero |
| Morph position (119) | never | pushed, ~40 Hz while ramping |
| Morph button (`$00`/`$50`) | pushed | never |
| Amplifier page (`$0A`) | pushed | absent from the dump |
| Session-open state dump | none | the whole parameter state |

So a CBOR-only client can drive a tuner strobe but not a level meter, and a
MIDI3-only client can show everything except where the morph fader sits. `libkp`
exposes the live channel as `CborSession` (all three implementations) for exactly
this: hold it alongside a `DeviceModel` and fold its values in with
`apply_cbor` / `applyCbor`, and one state tree carries what both channels know.

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
| Full state dump | not observed | the selector-`2`/`4` burst above |

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
this channel must be a CBOR encoder and decoder from the start.

## What is not known

Explicitly out of scope of this document:

- The request and command grammar beyond the state-dump trigger — how to *ask*
  for anything else rather than observe what is pushed. No other request shape
  has been characterized.
- The semantics of the leading source-flag element in the 4-element variant.
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

The existence, wire shape, address space, filler convention, the state-dump
trigger, and the current-position addresses of the CBOR channel were
characterized through observed experimentation, from the outside only. See
[../CREDITS.md](../CREDITS.md).
