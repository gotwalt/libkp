# libkp

**A cross-platform library and documentation hub for the Kemper Profiler network
protocol.**

The Kemper Profiler exposes a rich control and monitoring surface over the
network — discovery, a live parameter/meter stream, and full parameter and
performance control. `libkp` documents that protocol and implements it in three
languages that stay in lockstep:

| Language | Package | Async model | Directory |
|---|---|---|---|
| Rust   | `libkp`  | `tokio`         | [`rust/`](rust/) |
| Python | `libkp`  | `asyncio`       | [`python/`](python/) |
| Swift  | `LibKP`  | `async`/`await` | [`swift/`](swift/) |

Each implementation ships the same example — **`meters`**, a live terminal view
of the current rig, its effect blocks, the tuner strobe, and the output meters.
The Swift implementation additionally ships **`MetersApp`**, the same dashboard
as a native SwiftUI macOS app.

## Documentation

The protocol is documented in [`docs/`](docs/):

1. [Overview](docs/01-overview.md)
2. [Discovery](docs/02-discovery.md) — UDP `DSCV` TagStream
3. [Handshake](docs/03-handshake.md) — the TCP GUID negotiation
4. [MIDI3 framing](docs/04-midi3-framing.md)
5. [SysEx / NRPN dialect](docs/05-sysex-nrpn.md)
6. [The CBOR channel](docs/06-cbor-channel.md) — the current bank/rig snapshot
7. [Realtime status & meters](docs/07-realtime-status.md)
8. [Control model](docs/08-control-model.md) — CC vs NRPN vs the device model
9. [Parameter registry](docs/09-parameter-registry.md)
10. [Versioning & compatibility](docs/10-versioning-and-compatibility.md)

## How the three implementations stay compatible

Everything derives from one source of truth in [`spec/`](spec/):

- **`spec/*.toml`** — the transport constants, parameter maps, effect types, the
  control vocabulary, and the meter block.
- **`codegen/generate.py`** turns the spec into a *data-only* module for each
  language (`generated.rs`, `_generated.py`, `Generated.swift`). CI fails if a
  committed module drifts from the spec, so the constant tables are provably
  identical in all three languages.
- **`spec/vectors/*.json`** are language-neutral conformance vectors — hex
  inputs paired with expected outputs — that all three test suites load. The
  hand-written logic (framing, encode/decode, the device model) is held to
  byte-for-byte agreement.
- **`spec/captures/*.json`** are sanitized recordings of real protocol traffic
  that every implementation replays end-to-end, validating decode against
  genuine wire data (identity scrubbed; structure and framing intact).
- A single **`SPEC_VERSION`** is embedded in each library and checked by CI.

See [docs/10](docs/10-versioning-and-compatibility.md) for the full mechanism.

## Status

`libkp` implements the fully-validated MIDI3 surface: discovery, session,
framing, NRPN read/write, CC control, the realtime meter/tuner decode, a typed
parameter registry, and an observable device model. It also speaks the device's
native CBOR channel for one purpose — the state-dump snapshot that reads the
current bank and rig (docs/06); the channel's wider management grammar is not
implemented.

## Attribution

The MIDI parameter model — page/number addresses, effect types, string tags, and
the control-change map — follows the official
[Kemper MIDI Parameter Documentation](https://www.kemper-amps.com/downloads/5/User-Manuals),
cross-checked against the excellent [PySwitch](https://github.com/Tunetown/PySwitch)
project, whose Kemper client is credited for confirming addresses and the
bidirectional beacon. The effect-type **category** blocks are inferred from the
value-range structure of that documentation's Appendix B rather than transcribed
from a printed table. The transport envelope — discovery, the handshake, MIDI3
framing, session encapsulation, and the CBOR channel — was characterized through
observed experimentation. See [CREDITS.md](CREDITS.md).

## License

MIT — see [LICENSE](LICENSE).
