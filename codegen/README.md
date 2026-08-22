# codegen

`generate.py` reads the spec in [`../spec`](../spec) and emits a **data-only**
module for each implementation. Those modules contain constants and lookup
tables — never protocol logic. Each implementation hand-writes the logic and is
held to the shared conformance vectors.

```sh
python3 codegen/generate.py          # regenerate the three modules
python3 codegen/generate.py --check  # exit 1 if any committed module is stale (CI)
python3 codegen/gen_vectors.py       # regenerate spec/vectors/*.json
```

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

Tables are emitted as the most natural literal per language: Rust `static`
slices of tuples, Python `dict`/`list`, Swift `Dictionary`/`Array` (with small
`NonEffectKey` / `MeterField` / `EffectCategory` helper structs).
Implementations wrap these with
thin lookup helpers (`param_name`, `effect_type_name`, …); the `params.json`
vectors verify those helpers agree across languages.

**Do not edit the generated files by hand.** Change `spec/*.toml` and rerun the
generator.
