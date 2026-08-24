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

// Connect: opens the stream, runs the read-only sync burst in the background,
// and opens the control link (the morph position's only source) after it.
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
| `session` | TCP connect, the protocol-list handshake, and the stream preamble. Every open passes the process-wide connection ledger, which waits out `CONNECTION_COOLDOWN` since the last open or close to the same device. The handshake gives the greeting, and then the selection reply, `HANDSHAKE_TIMEOUT` (2 s) to *begin* — a device that has served a few sessions can take most of a second to send its first byte — and only the short idle gap between chunks once it has. |
| `midi3` | The 4-byte stream framing (`Unframer`, `frame`). |
| `cbor` | The native CBOR channel: codec, and the one open-and-ingest path the model's control link is built on. `StateSnapshot::fetch` (a one-shot read of the current bank/rig/morph) and `CborSession` (a live raw feed) are tools on the same path for reading the channel on its own. |
| `nrpn` | Kemper SysEx/NRPN builders and parsers (14-bit values, string tags, extended strings, the beacon). |
| `control` | The 7-bit CC / Program Change / Bank Select vocabulary (`Control`). |
| `params` | Offline `page/number → name` lookups over the generated tables. |
| `registry` | Typed descriptors (`ParamKind`, `format_value`) layered over `params`. |
| `state` | The immutable `DeviceState` tree — rig, amp, cabinet, eight effect slots, tuner, output, morph, meters — and its decoders: `apply` (one MIDI3 message), `apply_cbor` / `apply_cbor_text` (one CBOR item), each a thin front on the fold. |
| `routes` | The state routing fold: `DeviceState::apply_update`, the one funnel every value passes through whichever wire carried it, driven by the generated `STATE_ROUTES` table (`spec/state.toml`) — which addresses are tracked, which channel may write them, how they decode, whether a repeat is a no-op. |
| `model` | `DeviceModel`, the async observable store: the only object that holds a socket to the device. Owns the MIDI3 stream (ingest, writer, the request lane) and the CBOR control link, feeds both into the one funnel, supervises connect, loss and reconnect, and holds the Navigator — the one way a rig is loaded (`model::nav`). |
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

### Two links

A `DeviceModel` holds two sockets and shows you neither:

- The **stream** is the MIDI3 session — the meter frame, every parameter push,
  the strings, and the request/reply lane. It is required: losing it is
  losing the device.
- The **control link** is the device's native CBOR channel, opened right after
  the stream by default (`ControlPolicy::BestEffort`). Its state dump and its
  live pushes fold into the same tree, which is how the morph position — a
  value the stream never carries — reaches `state.morph`. The link is
  read-only by construction: the one item libkp writes on it is the dump
  trigger, and there is no queue to write anything else.

`state.channels` reports each link as it really is (`Closed`, `Connecting`,
`Open`, `Unavailable`, `Lost`) and `state.connection` summarises them:
`Connected`, `Degraded` (the stream is up but the control link, which was
asked for, is not — the morph has gone stale, nothing else has), `Reconnecting
{ attempt }`, or `Disconnected`. Every transition raises
`DeviceEvent::ConnectionChanged` / `ChannelChanged`; `Connected` and
`Disconnected` are still raised too.

`DeviceModel::connect_with(ip, ConnectOptions { .. })` chooses: whether to open
the control link at all (`Off`), in the background (`BestEffort`), or before
`connect` returns and failing it otherwise (`Required`); whether to run the
sync burst (`SyncStrategy::StreamBurst`, the default — the 46 `request = true`
rows of the routing table, answered by the device within tens of milliseconds
and reported by `SyncCompleted { Stream }`) or nothing (`Off`); and whether to
redial the stream after a loss (`ReconnectPolicy::stream: Some(Backoff::
default_stream())`, 4 s doubling to 30 s) — by default a lost stream is
reported as `Disconnected` and left there, and a lost control link as
`Degraded` until `reopen_control()` asks for it again (never inside
`CONTROL_REOPEN_MIN_GAP_MS` of the last attempt). Dropping the last handle,
or `close()`, closes both links and raises `Disconnected`.

### Parameters, requests and actions

- **Parameters** (`set_gain`, `set_rig_volume`, `set_effect_enabled`, …) go out
  as 14-bit NRPN Single Parameter Changes. The device applies them silently and
  does not echo them, so follow a set with `request_param` when the snapshot
  should confirm the new value.
- **Requests** (`request_param`, `request_string`, `request_ext_param`,
  `request_ext_string`, `request_render`, and `refresh` / `refresh_rig` /
  `refresh_bank` / `refresh_position`) are read-only questions with an answer:
  each rides the stream's request lane and resolves with the device's reply,
  or with `RequestError::Timeout` after `REQUEST_TIMEOUT_MS` (300 ms — never
  retried). At most `MAX_IN_FLIGHT_REQUESTS` (16) are on the wire at once; the
  rest queue. The morph position is `Unreadable` over the stream and is
  refused without sending.
- **Actions** (`tap_tempo`, `tuner_mode`, `bank`, the buttons and pedals, …)
  go out as 7-bit Control Changes. They are momentary and carry no read-back,
  so they are not reflected in state.

`set_param`, `send_control` and `send_raw` are the escape hatches for any
address, any raw control, or any MIDI bytes at all — all but a rig load, which
only the Navigator sends.

### The Navigator

A rig load makes the device replay its whole parameter tree, and two loads
close together (8 ms apart is enough) wedge it on a delayed fuse: it answers
the first normally, closes the session some twenty seconds later, and stops
accepting connections until it is power cycled. So libkp never sends a load
directly. A caller *aims*:

```rust,no_run
# use libkp::model::DeviceModel;
# fn run(model: &DeviceModel) {
model.navigate_to(14);      // a flat, 0-based rig index: bank 3, slot 5
model.step_rig(1);          // the next rig, from wherever the aim (or the device) is
model.step_bank(false);     // the same slot a bank down
model.select_slot(2);       // slot 2 of the bank the aim is in
# }
```

Each returns at once. The aim lands in `state.navigation.aim` immediately, so
a slot highlight answers every tap, and `state.aimed_rig_index()` — the aim
while there is one, the device's own position otherwise — is the readout to
bind and to step from. The Navigator sends one load at a time — the bank
preselect (CC47) and the slot load (CC50–54), the documented pair — and the
next only `RIG_LOAD_SETTLE_MS` (500 ms) after the last, so a burst of taps
costs two loads however long it is. A position report that matches the aim,
on either wire, retires it with `DeviceEvent::NavigationSettled`; an aim the
device never confirms (one past the end of its rigs — it stays put and says
so) is dropped after `PENDING_WINDOW_MS` (1.5 s) with
`DeviceEvent::NavigationDropped`. `send_control` and `send_raw` refuse the
load controllers (`RIG_LOAD_CONTROLLERS`), Program Change and Bank Select
with `CommandError::RigLoadRequiresNavigator` before a byte goes out; the
bare preselect (`bank`) still passes, since it loads nothing.

The state machine behind this, `model::nav::NavigatorState`, is pure and
pinned by `spec/vectors/navigation.json`, so the Rust, Python and Swift
Navigators make exactly the same moves.

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

`←`/`→` step to the previous or next rig through the Navigator; Ctrl-C quits
and restores the terminal.

## Tests

```sh
cargo test
```

`tests/model.rs` drives `DeviceModel` against the fake device in
`tests/common/mod.rs` — a multi-connection, protocol-aware stand-in that
answers requests from value tables, serves a CBOR dump on the trigger, and
can hang up either link — through connect, the dump, the request lane, the
Navigator (a burst of taps is two loads on the wire, a settle apart), loss,
reconnect, close and drop. The fake takes an ephemeral port, so the
connection ledger only ever spaces a model's own second socket.

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
