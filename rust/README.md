# libkp (Rust)

The Rust implementation of `libkp`, a cross-platform library for the Kemper
Profiler network protocol: LAN discovery, the TCP session and handshake, the
MIDI3 stream framing, Kemper SysEx/NRPN, the MIDI control vocabulary, and an
observable device model.

```toml
[dependencies]
libkp = "0.1"
```

## Quick start

```rust,no_run
use std::net::Ipv4Addr;
use libkp::control::Control;
use libkp::model::DeviceModel;

# async fn run() -> Result<(), Box<dyn std::error::Error>> {
// Find a Profiler on the LAN (or skip straight to `connect` with a known IP).
let reply = libkp::find_first(std::time::Duration::from_secs(3)).await?;
let ip = reply.and_then(|r| r.ipv4()).unwrap_or(Ipv4Addr::new(192, 168, 1, 50));

// Connect: opens the streaming session and runs a read-only initial rig sync.
let model = DeviceModel::connect(ip).await?;

// The store: a fresh snapshot every time snapshot-visible state changes.
let mut snapshots = model.subscribe();
while let Ok(state) = snapshots.recv().await {
    println!("rig: {:?}  REV on: {:?}",
        state.rig.name,
        state.effect("REV").and_then(|e| e.on));
}
# Ok(())
# }
```

Discovery needs UDP 5727 **exclusively** — the device replies only to that
port, and the kernel hands each reply to just one bound socket, so a second
listener steals replies rather than copying them. Acquiring it fails fast if
another program (Kemper's Rig Manager, typically) holds it. Hold a
`DiscoveryPort` across a session rather than re-acquiring per attempt; see
[Discovery](../docs/02-discovery.md#owning-the-port).

```rust
let port = libkp::DiscoveryPort::acquire()?;  // Err(PortUnavailable) if taken
let replies = port.poll(&libkp::Options::default()).await?;
```

## Layout

| Module | What it does |
|---|---|
| `generated` | **Data only** — constants and lookup tables emitted from the shared `spec/` by `codegen/generate.py`. Never edited by hand. |
| `protocol` | The tag-stream wire encoding and the `DSCV` discovery poll packet. |
| `discovery` | Async UDP broadcast discovery (`DiscoveryPort`, `discover`, `find_first`). |
| `session` | TCP connect, the protocol-list handshake, and the stream preamble. |
| `midi3` | The 4-byte stream framing (`Unframer`, `frame`). |
| `cbor` | The native CBOR channel: codec, `StateSnapshot::fetch` (a one-shot read of the current bank/rig/morph) and `CborSession` (a live session streaming what MIDI3 omits). |
| `nrpn` | Kemper SysEx/NRPN builders and parsers (14-bit values, string tags, extended strings, the beacon). |
| `control` | The 7-bit CC / Program Change / Bank Select vocabulary (`Control`). |
| `params` | Offline `page/number → name` lookups over the generated tables. |
| `registry` | Typed descriptors (`ParamKind`, `format_value`) layered over `params`. |
| `state` | The immutable `DeviceState` tree — rig, amp, cabinet, eight effect slots, tuner, output, morph, meters. |
| `model` | `DeviceModel`, the async observable store, plus `DeviceState::apply` (the pure decode routing). |
| `error` | `DiscoverError`, `SessionError`, `ParseError`. |
| `fmt` | Small hex/ASCII formatting helpers. |

### Two lanes

`DeviceModel` classifies state as **fast** or **slow**:

- **Fast** — the realtime status block (meters, tuner strobe), the beat pulse,
  and tuner deviance. Poll `model.status()` once per animation frame.
- **Slow** — everything else. `model.subscribe()` emits a fresh `DeviceState`
  snapshot only when a snapshot-visible field actually changed, coalesced to at
  most one per ingested stream chunk.

`model.events()` is the granular delta stream if you would rather have deltas
than snapshots.

### Parameters vs actions

- **Parameters** (`set_gain`, `set_rig_volume`, `set_effect_enabled`, …) go out
  as 14-bit NRPN Single Parameter Changes. The device applies them silently and
  does not echo them, so follow a set with `request_param` when the snapshot
  should confirm the new value.
- **Actions** (`tap_tempo`, `rig_up`, `select_rig`, `tuner_mode`, …) go out as
  7-bit Control Changes. They are momentary and carry no read-back, so they are
  not reflected in state.

`set_param` and `send_control` are the escape hatches for any address or any
raw control.

## The `meters` example

A live full-screen terminal view: the current rig (name, author, tempo), the
amp and cabinet, the eight effect blocks with on/off state and type name, the
tuner strobe with an in-tune verdict, level bars with peak-hold, a tempo pulse
indicator, and the most recent parameter change.

```sh
cargo run --example meters                     # discover a device
cargo run --example meters -- --ip 192.168.1.50
cargo run --example meters -- --all --width 60 # all 11 raw realtime fields
```

Ctrl-C quits and restores the terminal.

## Tests

```sh
cargo test
```

`tests/conformance.rs` is the cross-language contract. It loads
`spec/vectors/*.json` (synthetic builder/parser vectors, which pin every
function's exact bytes) and `spec/captures/*.json` (sanitized replay fixtures,
which prove a real stream decodes end to end), and asserts this crate matches.
The Python and Swift implementations run the same files, so any divergence
fails that implementation's build.

Note that `src/generated.rs` is written by `codegen/generate.py`, not by
`rustfmt`; exclude it from any `cargo fmt --check` gate, or teach the generator
to emit rustfmt-clean output.

## Provenance

The message grammar, parameter maps, effect types, string tags, CC map, and the
beacon come from the Kemper MIDI Parameter Documentation and from PySwitch.
Discovery, the TCP handshake, the MIDI3 framing, the session encapsulation, and
the realtime status/meter field identities were established by observed
experimentation against hardware.

## License

MIT. See [`../LICENSE`](../LICENSE).
