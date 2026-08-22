# Versioning & compatibility

`libkp` maintains three implementations — Rust, Python, and Swift — of one
protocol. Keeping them behaviorally identical, release after release, is a
first-class design goal rather than an afterthought. Four mechanisms do it.

## 1. One source of truth

Every protocol constant and lookup table lives once, in [`../spec`](../spec):

- `protocol.toml` — ports, protocol GUIDs, MIDI3 framing tags, the SysEx
  envelope (manufacturer id, product/device, function codes), the beacon, and
  the discovery poll.
- `parameters.toml` — the page/number parameter maps, effect slots, string tags,
  and well-known addresses.
- `effect-types.toml` — the effect Type value → name table, and the category
  blocks the values are allocated in.
- `controls.toml` — the Control Change vocabulary.
- `meters.toml` — the realtime status / meter block.

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
| `state.json` | applying messages to the device state |

Each implementation's test suite loads these files and asserts byte-for-byte
agreement. A behavioral drift in any language fails its own build.

### Replay captures

The synthetic vectors pin individual functions on constructed inputs. To also
validate the full decode pipeline against **real** wire data, `libkp` ships a
cross-platform replay harness in [`../spec/captures`](../spec/captures): sanitized
recordings of actual protocol traffic (a discovery reply, the live status
stream, a longer meter stream, and a rig-load dump). All identifying text —
device names, owners, serials, MAC addresses, profile authors, timestamps — is
stripped; the numeric and structural content and the exact framing are kept
intact. Every implementation replays these fixtures and asserts the decoded
result, so a regression in real-stream handling — framing across message
boundaries, the mix of message types, status-block decoding — fails that
language's build. See [`spec/captures/README.md`](../spec/captures/README.md) for
the harness contract.

## 4. A single spec version

`spec/version.toml` defines `SPEC_VERSION`. It is generated into every library
(`libkp::generated::SPEC_VERSION`, `libkp._generated.SPEC_VERSION`,
`Generated.specVersion`) and asserted by each conformance suite. CI cross-checks
that all three report the same value. Bump it whenever a spec change alters
generated data or wire behavior.

## What this means in practice

- **To add or fix a parameter, effect type, or CC**: edit the relevant
  `spec/*.toml`, run `codegen/generate.py`, and commit. All three languages gain
  it at once.
- **To change wire behavior**: update the logic in each implementation and add a
  vector to `spec/vectors/` that pins the new behavior. The vector is the
  contract; the three implementations follow it.
- **CI gates** every change on: all three build + test suites pass, the
  generated modules are not stale, and the spec version is consistent.

The library version (crate/package version) and the spec version are separate:
the library version tracks API and packaging; the spec version tracks the
protocol data and wire contract the three implementations share.
