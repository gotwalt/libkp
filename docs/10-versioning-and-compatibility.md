# Versioning & compatibility

`libkp` maintains three implementations — Rust, Python, and Swift — of one
protocol. Keeping them behaviorally identical, release after release, is a
first-class design goal rather than an afterthought. Four mechanisms do it.

## 1. One source of truth

Every protocol constant and lookup table lives once, in [`../spec`](../spec):

- `protocol.toml` — ports, protocol GUIDs, MIDI3 framing tags, the SysEx
  envelope (manufacturer id, product/device, function codes), the beacon, the
  discovery poll, the CBOR channel's shapes and dump markers, and the
  `[safety]` pacing the model applies on the device's behalf (the request
  timeout and in-flight cap, the rig-load settle and pending window, the
  reconnect and reopen floors, the rig-load controllers).
- `parameters.toml` — the page/number parameter maps, effect slots, string tags,
  and well-known addresses.
- `effect-types.toml` — the effect Type value → name table, and the category
  blocks the values are allocated in.
- `controls.toml` — the Control Change vocabulary.
- `meters.toml` — the realtime status / meter block.
- `state.toml` — the state routing table: which addresses the device model
  stores, how each decodes, which lane and wire it belongs to, and whether the
  connect-time sync requests it (see
  [State routing](09-parameter-registry.md#state-routing)).

No constant is defined in any implementation's own source. If a value is wrong,
it is wrong in exactly one place.

## 2. Generated data modules

[`../codegen/generate.py`](../codegen/generate.py) serializes the spec into a
data-only module per language:

```
rust/src/generated.rs
python/src/libkp/_generated.py
swift/Sources/LibKP/Generated.swift
```

These are committed so that consumers never need the toolchain. CI runs
`generate.py --check`, which regenerates in memory and fails the build if any
committed module differs. The constant tables are therefore **provably
identical** across the three languages at all times — a divergence cannot merge.

The modules are data only. That holds for the routing table too: `state.toml`
becomes `STATE_ROUTES` plus the `Field` / `Kind` / `Lane` / `Wire` enums, and
the fold that consumes them is hand-written in each language, where the
compiler's exhaustiveness check over `Field` keeps it complete.

## 3. Shared conformance vectors

Constants being equal is necessary but not sufficient: the hand-written logic
(framing, SysEx builders, NRPN parsing, control encoding, state decoding) must
also agree. [`../spec/vectors`](../spec/vectors) holds language-neutral vectors —
hex inputs paired with expected structured or hex outputs:

| File | Covers |
|---|---|
| `u14.json` | 14-bit split/join |
| `discovery.json` | the `DSCV` poll packet |
| `midi3.json` | frame / unframe |
| `nrpn.json` | SysEx builders + parsers |
| `controls.json` | Control op → MIDI bytes |
| `params.json` | offline name lookups |
| `state.json` | the state fold: `messages` cases apply unframed MIDI3 to a fresh tree; `steps` cases drive the funnel with transport-tagged updates (`midi3`, `cbor`, `cbor_text`, `cbor_dump`, `cbor_dump_text`, `dump_begin`, `dump_end`) and pin the ordered events and the snapshot count as well as the tree — one case per rule of the fold |
| `cbor.json` | the CBOR control channel: the state-dump write, and reading position, strings and morph out of a decoded dump |
| `navigation.json` | the Navigator's pure state machine: `navigate`, `settle`, `window` and `position` steps, with the exact ordered actions and the wire log of every load sent |

Each implementation's test suite loads these files and asserts byte-for-byte
agreement. A behavioral drift in any language fails its own build.

Every vector file is written by [`../codegen/gen_vectors.py`](../codegen/gen_vectors.py),
which carries one reference implementation of each builder (SysEx, MIDI3
framing, the CBOR items, the Navigator's transitions) and cross-checks it
inline against hardware-validated bytes before serializing. CI reruns the
script and fails if `spec/vectors` changes, so the committed vectors always
match the script.

### Replay captures

The synthetic vectors pin individual functions on constructed inputs. To also
validate the full decode pipeline against **real** wire data, `libkp` ships a
cross-platform replay harness in [`../spec/captures`](../spec/captures): sanitized
recordings of actual protocol traffic — a discovery reply, the live status
stream, a longer meter stream, a rig-load dump (kinds `discovery` and
`midi3_stream`), and the CBOR channel's reply to one state-dump trigger with
the live items that arrived in the same window (kind `cbor_stream`). All
identifying text — device names, owners, serials, MAC addresses, profile
authors, timestamps, and every string and blob payload in the CBOR dump — is
stripped; the numeric and structural content and the exact framing are kept
intact. Every implementation replays these fixtures and asserts the decoded
result, so a regression in real-stream handling — framing across message
boundaries, the mix of message types, status-block decoding, the dump's item
shapes, its end marker and its fold into the tree — fails that language's
build. See [`spec/captures/README.md`](../spec/captures/README.md) for the
harness contract.

## 4. A single spec version

`spec/version.toml` defines `SPEC_VERSION`. It is generated into every library
(`libkp::generated::SPEC_VERSION`, `libkp._generated.SPEC_VERSION`,
`Generated.specVersion`) and asserted by each conformance suite. CI cross-checks
that all three report the same value. Bump it whenever a spec change alters
generated data or wire behavior.

### Bumping the spec version

A bump touches one file and three literals, and every one of them is checked:

1. `spec/version.toml` — `spec_version`.
2. The literal each conformance suite pins, so a stale library fails its own
   tests rather than silently running against a newer spec:
   `rust/tests/conformance.rs` (`spec_version_matches`),
   `python/tests/test_conformance.py` (`test_spec_version_matches`), and
   `swift/Tests/LibKPTests/ConformanceTests.swift` (`testSpecVersionMatches`).
3. `python3 codegen/generate.py` to carry the version into the three generated
   modules, and `python3 codegen/gen_vectors.py` if the change touched any
   vector input.

A new well-known address, routing row or `[safety]` constant is a minor bump; a
constant that only tunes timing is a patch bump.

## What this means in practice

- **To add or fix a parameter, effect type, or CC**: edit the relevant
  `spec/*.toml`, run `codegen/generate.py`, and commit. All three languages gain
  it at once.
- **To track a new field in the device state**: add its address to
  `[well_known]` in `parameters.toml` if it lacks a key, add a `[[route]]` row to
  `state.toml`, regenerate, then teach each language's fold the new `Field`
  variant — the compiler (or Python's coverage test) points at the gap.
- **To change wire behavior**: update the logic in each implementation and add a
  vector to `spec/vectors/` that pins the new behavior. The vector is the
  contract; the three implementations follow it.
- **To change how the model paces the device** — a timeout, a settle, a
  floor: change the `[safety]` constant in `protocol.toml` and regenerate;
  every implementation reads it from its generated module and none carries a
  literal of its own.
- **CI gates** every change on: all three build + test suites pass, the
  generated modules are not stale, and the spec version is consistent.

The library version (crate/package version) and the spec version are separate:
the library version tracks API and packaging; the spec version tracks the
protocol data and wire contract the three implementations share.
