# codegen

`generate.py` reads the spec in [`../spec`](../spec) and emits a **data-only**
module for each implementation. Those modules contain constants and lookup
tables — never protocol logic. Each implementation hand-writes the logic and is
held to the shared conformance vectors.

```sh
python3 codegen/generate.py          # regenerate the three modules
python3 codegen/generate.py --check  # exit 1 if any committed module is stale (CI)
python3 codegen/gen_vectors.py       # regenerate spec/vectors/*.json (every file, cbor.json included)
```

`gen_vectors.py` owns every file under `spec/vectors/`, including `cbor.json`,
for which it carries a minimal CBOR encoder. CI regenerates the vectors and
fails if the committed files differ, so a hand edit to a vector does not
survive — change the script.

Requires Python 3.11+ (stdlib `tomllib`).

## Generated targets

| Language | File | Namespace |
|---|---|---|
| Rust   | `rust/src/generated.rs`               | module `generated` |
| Python | `python/src/libkp/_generated.py`      | module `libkp._generated` |
| Swift  | `swift/Sources/LibKP/Generated.swift` | `enum Generated` |

## The contract (what implementations consume)

Every target exposes the same information, adapted to each language's naming
conventions (`SCREAMING_SNAKE` in Rust/Python, `lowerCamel` under `Generated` in
Swift). Highlights:

- **`SPEC_VERSION`** — the spec version string; assert it in the conformance suite.
- **Transport**: `PORT`, connect/socket timeouts, discovery header + poll pieces,
  handshake terminator / markers, `SESSION_PREAMBLE_LEN`, the four protocol GUIDs.
- **MIDI3**: `MIDI3_TAG_CONTINUATION`, `MIDI3_TAG_FINAL_1/2/3`.
- **SysEx**: `MANUFACTURER_ID`, `PRODUCT_PROFILER/PLAYER`, `DEVICE_OMNI`,
  `FULL_SCALE`, and the `FN_*` function codes; beacon constants.
- **Well-known addresses** used by the state/model layer (`PAGE_REALTIME`,
  `METER_BLOCK_NUMBER`, `TEMPO_BPM_SCALE`, `MORPH_NUMBER`, `TUNER_*`, …).
- **Tables**: `FUNCTION_NAMES`, `PAGE_NAMES`, `EFFECT_SLOTS`, `EFFECT_PARAMS`,
  `NON_EFFECT_PARAMS`, `STRING_TAGS`, `PAGE0_NUMERIC`, `EFFECT_TYPES`,
  `EFFECT_CATEGORIES`, `SLOT_ENABLE_CC`, `METER_FIELDS`, and the `CC_*` control
  constants.
- **The state routing table** from `spec/state.toml`: `STATE_ROUTES`, a flat
  list of `Route` records sorted by address, and the closed enums it is typed
  with — `Field` (one variant per route name: the tree field the address
  writes), `Kind` (how the value decodes), `Lane` (fast / slow) and `Wire`
  (stream / control / both). Each `Route` carries `address`, `field`, `slot`
  (the per-slot index for rows expanded over the effect slots, the bank
  preview, or a spanned block; absent otherwise), `kind`, `lane`, `wire`,
  `dedupe` and `request`. Per-slot and spanned rows are expanded by the
  generator, so the table is already flat.

Tables are emitted as the most natural literal per language: Rust `static`
slices of tuples, Python `dict`/`list`, Swift `Dictionary`/`Array` (with small
`NonEffectKey` / `MeterField` / `EffectCategory` helper structs).
Implementations wrap these with
thin lookup helpers (`param_name`, `effect_type_name`, …); the `params.json`
vectors verify those helpers agree across languages.

The routing table follows the same rule. It is emitted as data — Rust
`generated::{Field, Kind, Lane, Wire, Route, STATE_ROUTES}`, Python
`libkp._generated.{Field, Kind, Lane, Wire, Route, STATE_ROUTES}` (`Enum`s and
a `NamedTuple`), Swift `Route` with nested `Route.Field` / `Route.Kind` /
`Route.Lane` / `Route.Wire` and `Generated.stateRoutes` — and nothing more:
no lookup by address, and no code that writes a field. The fold that turns a
`Route` into a store write is hand-written in each language, where the
compiler's exhaustiveness check over `Field` (or a coverage test, in Python)
keeps it complete. The table is small enough that generating the fold would
buy little and would change the data-only contract.

**Do not edit the generated files by hand.** Change `spec/*.toml` and rerun the
generator.
