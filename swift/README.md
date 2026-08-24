# libkp — Swift

The Swift implementation of `libkp`, a cross-platform library for the Kemper
Profiler network protocol.

Pure Swift: Foundation plus the `Network` framework for TCP/UDP. No SwiftPM
dependencies, no C bridging, no code generation at build time.

```sh
swift build                  # the library and the examples
swift build --product meters # just the example
swift run MetersApp          # the SwiftUI app example
swift test                   # unit tests + the shared conformance suite
```

Requires Swift 6.0 or newer and macOS 13+.

## Layout

| Path | What it is |
|---|---|
| `Sources/LibKP/Generated.swift` | **Generated** constants and lookup tables — do not edit; see [`../codegen`](../codegen) |
| `Sources/LibKP/Protocol.swift` | The discovery TagStream encoding and the poll packet |
| `Sources/LibKP/Discovery.swift` | UDP discovery over an exclusively-bound BSD socket |
| `Sources/LibKP/Session.swift` | TCP session, protocol handshake, stream preamble, and the per-peer connection ledger that spaces opens by the cooldown. The handshake gives the greeting, and then the selection reply, `Session.handshakeTimeout` (2 s) to *begin* — a device that has served a few sessions can take most of a second to send its first byte — and only the short idle gap between chunks once it has |
| `Sources/LibKP/Inbox.swift` | The timed read buffer behind the session's async reads |
| `Sources/LibKP/Midi3.swift` | Stream framing/unframing |
| `Sources/LibKP/Nrpn.swift` | SysEx/NRPN builders and parsers |
| `Sources/LibKP/Control.swift` | The typed 7-bit CC / PC / Bank Select vocabulary |
| `Sources/LibKP/Params.swift` | Offline name lookups over the generated tables |
| `Sources/LibKP/Registry.swift` | Typed parameter descriptors and value formatting |
| `Sources/LibKP/State.swift` | The state tree and the decoders in front of its fold |
| `Sources/LibKP/Routes.swift` | The fold: one routing table, one funnel, whichever wire carried the value |
| `Sources/LibKP/Cbor.swift` | The CBOR decoder/encoder, the control link, and the `CborSession` / `StateSnapshot` tooling built on it |
| `Sources/LibKP/DeviceModel.swift` | The `actor` that owns both links and publishes state |
| `Sources/LibKP/Link.swift` | Connect options, the stream link, and the supervisor: connect order, reconnect, channel health |
| `Sources/LibKP/Lane.swift` | The request lane: requests that resolve with their reply, and `refresh()` |
| `Sources/LibKP/Navigator.swift` | The Navigator: the vector-pinned state machine that serialises rig loads, and `navigateTo` / `stepRig` / `stepBank` / `selectSlot` |
| `Sources/meters/main.swift` | A live full-screen terminal view |
| `Sources/MetersApp/` | The same dashboard as a native SwiftUI macOS app |

Everything the conformance vectors exercise — framing, builders, parsers, name
lookups, and `DeviceState.apply` / `applyUpdate` — is pure and imports no
networking, so it is unit-testable with no device attached.

## Two layers

`DeviceState` is the **pure core**: a value-type tree (rig, amp, cabinet, the
eight effect slots, tuner, output, morph, bank preview, position, and the latest
`RealtimeStatus` meter frame). It folds one value at a time, whichever wire
carried it: an already-unframed MIDI3 message, or one CBOR item.

```swift
var state = DeviceState()
let outcome = state.apply(message)                    // -> ApplyOutcome(events:slowChanged:)
state.applyCbor(address: 119, value: 8192)            // a CBOR numeric: the morph position
state.applyCborText(address: 1, text: "AC30")         // a CBOR string: the rig name
```

Every entry point is a thin decoder in front of `applyUpdate`, and what the tree
does with an address is decided by one spec-declared table (`spec/state.toml`,
generated as `Generated.stateRoutes`): which field it writes, how the value
decodes, which wire may write it, whether a repeat is a no-op, and whether it
is FAST or SLOW. An address with no row is untracked. Between `beginDump()` and
`endDump()` — the CBOR state dump — a live push outranks the dump's stale copy
of the same address. The dump has two sections, the system state and then the
loaded rig, each closed by a run based at `Generated.dumpEndAddress`; the model
ends the dump phase at the `Generated.dumpEndRuns`-th (second) such run, or
`Generated.dumpSettleMs` after the trigger if it never comes.

`DeviceModel` is the **async handle**: an actor that owns every socket to the
device and publishes both a coalesced snapshot stream and a granular event
stream. It holds two links: the MIDI3 **stream**, which is required and is what
`connected` means, and — by default — the CBOR **control** link, which asks
for the device's state dump when it opens and then carries what the stream
never does, the morph position above all. Both feed the one tree, so an app
sees one handle, one snapshot, one event stream, and never a channel name
except in the `channelChanged` event.

```swift
import LibKP

let model = try await DeviceModel.connect(host: "192.168.1.50")

// The store: the current state first, then a fresh snapshot whenever slow
// state changes. Finishes only on close().
Task {
    for await state in await model.snapshots() {
        print(state.rig.name ?? "—", state.morph ?? 0, state.effect("REV")?.on ?? false)
    }
}

// The fast lane: poll per animation frame.
let meters = await model.status().raw

try await model.setEffectEnabled("REV", false)   // a tracked parameter
let on = try await model.requestParam(page: 0x3D, number: 3)  // a request, answered
try await model.tapTempo()                       // a momentary action
try await model.send(control: .freeze(true))     // any raw control
await model.close()                              // both links; the streams finish
```

`connect(host:port:options:)` takes a `ConnectOptions`; the defaults are the
recommended session, and a bare `connect` is all the examples use:

| Option | Default | Meaning |
|---|---|---|
| `port` | `Generated.port` | The TCP port both links dial — the only one a real device listens on; fakes and tests set another. A `port` passed to `connect` itself overrides it. |
| `control` | `.bestEffort` | Open the control link after the stream, in the background. `.off` never opens it (`connection` is `.connected` on the stream alone, the morph stays `nil`); `.required` fails `connect` if it cannot open. |
| `sync` | `.streamBurst` | Send the 46-request burst (`refresh()`) as soon as the stream is up: string tags, effect types and states, the bank preview, the position, the header values. `.off` asks for nothing. |
| `reconnect.stream` | `nil` | With a `Backoff` (`Backoff.defaultStream()` is 4 s doubling to 30 s), a lost stream is dialled again on the same handle — same streams, same tree — through `.reconnecting(attempt:)` until it is `.connected` again or `close()` is called. The attempt count, and the doubling with it, runs on until a life is fully up: under `.required`, a stream that opens but whose control link is refused is the next attempt, not a fresh start. `nil` reports `.disconnected` and stops. |
| `reconnect.controlReopen` | `nil` | Reopen a lost control link this long after the last attempt at it began (never inside `Generated.controlReopenMinGapMs`). `nil` never reopens it; `reopenControl()` is the explicit way, refused with `ChannelError.tooSoon` inside the gap. |

`state.connection` is `.disconnected`, `.reconnecting(attempt:)`, `.connected`
or `.degraded` — the last meaning the stream is up but a control link that was
asked for is `.unavailable` (its open failed) or `.lost` (it dropped); the tree
keeps working, only the morph stops moving. `state.channels` says which link is
where. Every transition of either is an event (`connectionChanged`,
`channelChanged`), and `connected` / `disconnected` are still raised at the two
ends of a life.

`CborSession` and `StateSnapshot.fetch` remain as tooling — a raw tap on the
control channel, and a one-shot dump read — built on the same control-link
code the model uses. An app that holds a model should not open either beside
it: the model already has the dump and the live pushes, and a third session is
churn the device objects to.

Every socket goes through `Session.connect`, whose ledger spaces opens to a
peer by `Session.connectionCooldown`; once dialled, `Session.handshake` waits
up to `Session.handshakeTimeout` (`Generated.handshakeTimeoutMs`, 2000) for
the first byte of the greeting, and again for the first byte of the reply to
the protocol selection, before reading the rest of each with the short idle
gap. A device that has served a few sessions can take most of a second to
greet, and a connect must not fail on that. A device that never greets — or
greets with nothing to choose — throws `SessionError.timeout` for the
`"greeting"` phase, reporting that full wait; one that reads the selection and
never answers it, the same for `"protocol selection"`. The control link reads
its greeting on the same budget. A dial the peer refuses fails at once with
`SessionError.connect`, not after the connect timeout.

Discovery is a one-liner when the address is unknown:

```swift
if let device = try await Discovery.findFirst(listenFor: 3) {
    print(device.name ?? "unnamed", device.host)
}
```

Discovery needs UDP 5727 **exclusively** — the device replies only to that
port, and the kernel hands each reply to just one bound socket, so a second
listener steals replies rather than copying them. Acquiring it fails fast if
another program (Kemper's Rig Manager, typically) holds it. Hold a
`DiscoveryPort` across a session rather than re-acquiring per attempt; see
[Discovery](../docs/02-discovery.md#owning-the-port).

```swift
let port = try DiscoveryPort()   // throws .portUnavailable if it is taken
defer { port.close() }
let replies = try await port.poll()
```

### Fast vs slow state

State is classified into two lanes. **FAST** is the meter `RealtimeStatus`
block, the beat pulse and tuner deviance — high-rate data best polled through
`status()`. **SLOW** is everything else; those changes drive the coalesced
`snapshots()` stream, at most one snapshot per ingested chunk from either link
— the whole state dump is one snapshot.

### Parameters vs actions

- **Parameters** (`setGain`, `setEffectEnabled`, `setTempoBpm`, `setParam`, …)
  are settable values the device stores. They go out as 14-bit NRPN `$01`
  Single Parameter Changes; the device applies the write silently and does *not*
  echo it back, so follow a set with `requestParam` when the snapshot should
  confirm the new value — the `$41` reply flows through normal ingest, which is
  what `MetersApp` does when you click an effect block. Every command goes
  through the stream link's command queue — bounded at 64, drained in order
  by one writer — so a call returns once queued, and waits for room while
  the queue is full.
- **Actions** (`tapTempo`, `morphPedal`, `bank`, `send(control:)`, …) are
  momentary presses and live expression. They go out as 7-bit Control Change
  messages and are *not* reflected in state.
- **Navigation** (`navigateTo`, `stepRig(by:)`, `stepBank(forward:)`,
  `selectSlot`) is the one way to load a rig — see below.
- **Requests** (`requestParam`, `requestString`, `requestExtParam`,
  `requestExtString`, `requestRender`, and `refresh` with its `refreshRig` /
  `refreshBank` / `refreshPosition` subsets) are read-only: they ask the
  device for values and change nothing. Each travels the request lane — at
  most `Generated.maxInFlightRequests` on the wire, the rest queued — and
  returns the value that lands at its address (folding it into the tree on the
  way), or throws `RequestError.timeout` after `Generated.requestTimeoutMs`
  with no retry; `refresh()` sends its rows in the table's address order, so
  the wire order is the same in every language. The morph position is
  `RequestError.unreadable` without a byte sent, whichever request form asks
  for it (`requestRender` included): the stream never answers it, the
  control link carries it. So is a `requestParam` reply wider than the 14
  bits a `$01` carries, which only the control wire can put at the address.

### Loading rigs: the Navigator

Two rig loads that land on top of each other wedge the device: it answers the
first, closes the session some twenty seconds later, and stops accepting
connections until it is power-cycled. So the model is the only thing that
loads a rig, through its **Navigator**, and the direct routes —
`send(control: .loadSlot(_:))`, `.up`, `.down`, a Program Change or Bank
Select, and `sendRaw` bytes carrying any of them — throw
`CommandError.rigLoadRequiresNavigator` before a byte is written. The bank
preselect (`bank(_:)`, CC47) loads nothing on its own and still passes.

```swift
await model.navigateTo(16)          // a flat 0-based rig index: bank 4, slot 2
await model.stepRig(by: 1)          // from state.aimedRigIndex, floored at 0
await model.stepBank(forward: true) // ± Params.bankSlots
await model.selectSlot(3)           // slot 1…5 of the aimed bank; out of range is ignored
```

Every call returns at once with the aim recorded in `state.navigation`
(`aim`, `inFlight`); `state.aimedRigIndex` is the aim while there is one,
else `currentRigIndex`, and is what the steppers step from — so two taps
inside the device's reporting delay compose instead of both stepping from the
same stale index. A load is the documented pair, bank preselect then slot
load, queued on the stream link as one unit — or, if the queue has no room
for both, or the stream is down, dropped at once with `navigationDropped` —
and is in flight for `Generated.rigLoadSettleMs` (500 ms, the measured
edge of the device pushing the landed rig on both wires); taps during the
flight only move the aim, and the final aim is sent once the flight settles, so
a burst costs two loads however long it is. The device's position report
(either wire) that matches the aim retires it with `navigationSettled`; an
aim the device never confirms — past the last rig, which it ignores — is
dropped `Generated.pendingWindowMs` (1500 ms) after its move settled, with
`navigationDropped`, and the device's own position is the truth again. The
steppers do nothing until a position is known. The state machine is pure
(`NavigatorState`) and pinned by `spec/vectors/navigation.json`.

## The `meters` example

A live full-screen terminal view built on `DeviceModel`. A bare `connect` runs
the read-only sync burst and opens the control link; it then renders the
current patch and block status, and exits when the device hangs up.

```sh
swift run meters                     # discover a device on the LAN
swift run meters --ip 192.168.1.50
swift run meters --all --width 60
```

| Flag | Meaning |
|---|---|
| `--ip <addr>` | Device IPv4 address; discovery runs when omitted |
| `--all` | Show all eleven raw meter fields, not just the level bars |
| `--width <n>` | Bar width in characters (default 44, clamped to 8…512) |

It shows the rig name, author and tempo; the amp and cabinet; the eight effect
blocks (A B C D X MOD DLY REV) with an on/off indicator and effect type; the
tuner strobe with an in-tune/sharp/flat verdict derived from the phase drift
rate; level bars with peak-hold; a tempo pulse; and the last parameter seen.
Ctrl-C restores the terminal.

## The `MetersApp` example

The same dashboard as a native SwiftUI macOS app: the rig name, author and
tempo; the amp and cabinet; the eight effect blocks with their on/off state,
effect type, category and mix; the tuner strobe with its in-tune/sharp/flat
verdict; level meters with peak-hold; and the tempo pulse. Clicking a
signal-chain block toggles that effect on or off — the one write it performs;
everything else it sends is a value request.

```sh
swift run MetersApp
```

It discovers a device on the LAN by default. Settings (⌘,) switches to a manual
IP for a device discovery cannot reach, and toggles the level list between the
three bar meters and all eleven raw fields. The app connects with
`ReconnectPolicy(stream: .defaultStream())`, so when the device drops the
connection the model dials again on its own and the app only shows the
attempt count; its rig buttons forward to the model's Navigator, and the
highlighted slot is `state.navigation.aim` until the device confirms the move.
The morph readout comes from the model's control link; while the model is
`.degraded` it shows `—`. With `KP_DEBUG_PORT` set it also listens on loopback
for the same taps as text commands, plus a `state` line that reports the
connection, channels, navigation and position as JSON.

## Tests

`swift test` runs two things:

1. **Unit tests** for each layer — framing, builders, parsers, the control
   vocabulary, name lookups, descriptors, the decode routing, and the model's
   links and request lane against an in-process fake device
   (`Tests/LibKPTests/FakeDevice.swift`), which serves any number of MIDI3 and
   CBOR connections on a loopback port.
2. **The shared conformance suite**, which loads every file in
   [`../spec/vectors`](../spec/vectors) and every fixture listed in
   [`../spec/captures`](../spec/captures) and asserts this implementation
   matches. The vectors pin individual functions; the capture fixtures replay
   recorded streams end to end. Both are located relative to the test source
   file, so no resource bundling is involved.

## Provenance

Parameter maps, the SysEx/NRPN grammar, effect types, string tags, the CC map
and the beacon come from the Kemper MIDI Parameter Documentation and PySwitch;
the effect-type category blocks are inferred from the value-range structure of
its Appendix B. Discovery, the handshake, the stream framing, session
encapsulation and the realtime status field identities were established by
observed experimentation.
