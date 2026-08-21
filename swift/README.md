# libkp — Swift

The Swift implementation of `libkp`, a cross-platform library for the Kemper
Profiler network protocol.

Pure Swift: Foundation plus the `Network` framework for TCP/UDP. No SwiftPM
dependencies, no C bridging, no code generation at build time.

```sh
swift build                  # the library and the example
swift build --product meters # just the example
swift test                   # unit tests + the shared conformance suite
```

Requires Swift 6.0 or newer and macOS 13+.

## Layout

| Path | What it is |
|---|---|
| `Sources/LibKP/Generated.swift` | **Generated** constants and lookup tables — do not edit; see [`../codegen`](../codegen) |
| `Sources/LibKP/Protocol.swift` | The discovery TagStream encoding and the poll packet |
| `Sources/LibKP/Discovery.swift` | UDP discovery over `NWListener` / `NWConnection` |
| `Sources/LibKP/Session.swift` | TCP session, protocol handshake, stream preamble |
| `Sources/LibKP/Inbox.swift` | The timed read buffer behind the session's async reads |
| `Sources/LibKP/Midi3.swift` | Stream framing/unframing |
| `Sources/LibKP/Nrpn.swift` | SysEx/NRPN builders and parsers |
| `Sources/LibKP/Control.swift` | The typed 7-bit CC / PC / Bank Select vocabulary |
| `Sources/LibKP/Params.swift` | Offline name lookups over the generated tables |
| `Sources/LibKP/Registry.swift` | Typed parameter descriptors and value formatting |
| `Sources/LibKP/State.swift` | The state tree and its decode routing |
| `Sources/LibKP/DeviceModel.swift` | The `actor` that owns the session and publishes state |
| `Sources/meters/main.swift` | A live full-screen terminal view |

Everything the conformance vectors exercise — framing, builders, parsers, name
lookups, and `DeviceState.apply` — is pure and imports no networking, so it is
unit-testable with no device attached.

## Two layers

`DeviceState` is the **pure core**: a value-type tree (rig, amp, cabinet, the
eight effect slots, tuner, output, morph, and the latest `RealtimeStatus` meter
frame). It decodes one already-unframed MIDI message at a time:

```swift
var state = DeviceState()
let outcome = state.apply(message)   // -> ApplyOutcome(events:slowChanged:)
```

`DeviceModel` is the **async handle**: an actor that owns an `NWConnection`,
runs an ingest loop, and publishes both a coalesced snapshot stream and a
granular event stream.

```swift
import LibKP

let model = try await DeviceModel.connect(host: "192.168.1.50")

// The store: a fresh snapshot whenever slow state changes.
Task {
    for await state in await model.snapshots() {
        print(state.rig.name ?? "—", state.effect("REV")?.on ?? false)
    }
}

// The fast lane: poll per animation frame.
let meters = await model.status().raw

try await model.setEffectEnabled("REV", false)   // a tracked parameter
try await model.tapTempo()                       // a momentary action
try await model.send(control: .freeze(true))     // any raw control
```

Discovery is a one-liner when the address is unknown:

```swift
if let device = try await Discovery.findFirst(listenFor: 3) {
    print(device.name ?? "unnamed", device.host)
}
```

### Fast vs slow state

State is classified into two lanes. **FAST** is the meter `RealtimeStatus`
block, the beat pulse and tuner deviance — high-rate data best polled through
`status()`. **SLOW** is everything else; those changes drive the coalesced
`snapshots()` stream, at most one snapshot per ingested stream chunk.

### Parameters vs actions

- **Parameters** (`setGain`, `setEffectEnabled`, `setTempoBpm`, `setParam`, …)
  are settable values the device reports back. They go out as 14-bit NRPN `$01`
  Single Parameter Changes, so the device echoes the change on the same stream
  the model ingests and the snapshot stays consistent.
- **Actions** (`tapTempo`, `rigUp`, `selectRig`, `send(control:)`, …) are
  momentary presses and live expression. They go out as 7-bit Control Change
  messages and are *not* reflected in state.
- **Requests** (`refreshRig`, `requestParam`, `requestString`,
  `requestRender`) are read-only: they ask the device for values and change
  nothing.

## The `meters` example

A live full-screen terminal view built on `DeviceModel`. On connect it runs the
read-only rig sync, then renders the current patch and block status.

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

## Tests

`swift test` runs two things:

1. **Unit tests** for each layer — framing, builders, parsers, the control
   vocabulary, name lookups, descriptors, and the decode routing.
2. **The shared conformance suite**, which loads every file in
   [`../spec/vectors`](../spec/vectors) and every fixture listed in
   [`../spec/captures`](../spec/captures) and asserts this implementation
   matches. The vectors pin individual functions; the capture fixtures replay
   recorded streams end to end. Both are located relative to the test source
   file, so no resource bundling is involved.

## Provenance

Parameter maps, the SysEx/NRPN grammar, effect types, string tags, the CC map
and the beacon come from the Kemper MIDI Parameter Documentation and PySwitch.
Discovery, the handshake, the stream framing, session encapsulation and the
realtime status field identities were established by observed experimentation.
