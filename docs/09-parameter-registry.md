# Parameter registry

A decoded [SysEx message](05-sysex-nrpn.md) gives you an address and a number —
`page 9, number 3, value 6925`. The registry turns that into
`Input Section: Noise Gate Intensity, 42 %`. It is a pure lookup layer: no
device, no network, no state.

## What it maps

| From | To |
|---|---|
| `(page, number)` | parameter name |
| `page` | section name |
| string-tag number (page 0) | tag name |
| effect-type value | effect name |
| effect-type value | category name |
| `(page, number)` | a display string, with a structural fallback |

All of it is offline and total — every lookup either returns a name or `null`,
and unknown addresses are a normal condition, not an error. Firmware adds
parameters; see [Versioning & compatibility](10-versioning-and-compatibility.md).

Vectors: [`../spec/vectors/params.json`](../spec/vectors/params.json).

## Names

```
param_name(9, 3)      →  "Noise Gate Intensity"
param_name(10, 4)     →  "Gain"
param_name(127, 0)    →  "Main Output Volume"
param_name(125, 88)   →  "Looper Record/Playback/Overdub"
param_name(0, 1)      →  "Rig Name"
param_name(50, 0)     →  "Type"
param_name(61, 4)     →  "Mix"
param_name(124, 84)   →  "Meter: Rig Output Level"
param_name(118, 5)    →  "User Scale 1 Step"
param_name(4, 5)      →  null
param_name(125, 112)  →  null
```

Two of those deserve comment. `param_name(50, 0)` and `param_name(61, 4)` are
pages `$32` and `$3D` — modules A and REV — resolving through the **shared
effect map** described below. And `param_name(118, 5)` comes from a **range
entry**: the spec declares User Scale steps as `page $76, numbers 0–11` and
`12–23` rather than 24 separate rows, and the generator expands them. The
**Bank Preview** page (`$96` / 150) is declared the same way — numbers 0–4 are
`"Bank Rig Name"`, 5–9 `"Bank Amp Name"`, 10–14 `"Bank Cabinet Name"`.

Section names come from the page table:

```
page_name(124)  →  "Realtime/Meters"
page_name(10)   →  "Amplifier"
page_name(150)  →  "Bank Preview"
page_name(153)  →  null
```

## Display

`describe(page, number)` produces the string a log line or a UI label wants:

```
describe(9, 3)      →  "Input Section: Noise Gate Intensity"
describe(124, 78)   →  "Realtime/Meters: Tuner Strobe Segment (phase-low)"
describe(153, 5)    →  "page 0x99 #5 (0x05)"
```

The fallback is structural on purpose. An unknown address still prints something
you can act on — the raw page and number in both bases — rather than
`<unknown>`, which tells you nothing when a new firmware starts emitting an
address the spec has not caught up with.

## Kinds

The address map stores names; the **kind** of a parameter — how its 14-bit value
should be interpreted and rendered — is determined by the parameter's role, and
falls into three cases:

| Kind | Value meaning | Render as | Examples |
|---|---|---|---|
| **Continuous** | fractional over the full 0–16383 range, smoothed by the device | percentage, bar, or a scaled unit | Gain, Mix, Volume, Rig Volume, the EQ bands |
| **Switch** | an integer, `0` = off, `1` = on | a toggle | On/Off, section enables, Tempo Enable |
| **Enum** | an integer indexing a value table | the table's name | effect Type, Amp Model, Kone Imprint Select |

Only one enum has its table in the spec — effect Type, in
[`../spec/effect-types.toml`](../spec/effect-types.toml) — so it is the only one
that resolves to a name offline. For the rest, the device itself can render a
value as display text on request: `$7C` Request Rendered String, reply `$3C`.
That is the general answer to "what does 8192 mean for *this* parameter", and it
is what `<0.0>` in the [rendered-string example](05-sysex-nrpn.md#rendered-strings)
is showing.

A few continuous parameters carry a scaled quantity rather than a fraction. The
one every client meets is tempo: page `$04`, number 0 holds **bpm × 64**
(`tempo_bpm_scale = 64`), so 7680 is 120 bpm.

## The effect-slot model

The Profiler has **eight effect slots** in signal-chain order, each addressed by
its own page. All eight share **one** parameter-number map — "mix" is the same
number in every slot — which is why the registry stores that map once and
resolves any effect page through it.

| Slot | Page | Enable CC |
|---|---|---|
| A | `$32` | 17 |
| B | `$33` | 18 |
| C | `$34` | 19 |
| D | `$35` | 20 |
| X | `$38` | 22 |
| MOD | `$3A` | 24 |
| DLY | `$3C` | 27 |
| REV | `$3D` | 29 |

Note the gaps — `$36`, `$37`, `$39`, `$3B` are not slots. Never derive a slot
page by arithmetic; use the table.

### The four numbers that matter

| Number | Parameter | Kind | Notes |
|---|---|---|---|
| **0** | Type | enum | value → effect name; **read-only** over MIDI |
| **3** | On/Off | switch | `00 01` on, `00 00` off |
| **4** | Mix | continuous | dry/wet, 0–16383 |
| **6** | Volume | continuous | 0–16383 |

Constants: `[effect_param_numbers]` in
[`../spec/parameters.toml`](../spec/parameters.toml). The remaining ~100 numbers
are shared across effect types, so one number often carries several
slash-separated names — number 21 is
`Drive Definition / Fuzz Transistor Tone / Modulation Depth / …`. The spec keeps
the full string rather than picking one, because which name applies depends on
the slot's current Type. A UI that knows the Type can split on `/`; a log line
can print the whole thing and lose nothing.

### Reading a slot

Two requests describe a slot completely:

```
$41 request  page $3D  number 0   →  $01 reply, value = Type
$41 request  page $3D  number 3   →  $01 reply, value = On/Off
```

Or one `$42`, which returns the whole unit at once and is much cheaper —
`f0002033027f42003400f7` requests every parameter of module C.

Resolving the type value gives the display name:

```
effect_type_name(0)    →  "empty"
effect_type_name(32)   →  "Kemper Drive"
effect_type_name(179)  →  "Easy Reverb"
effect_type_name(193)  →  "Spring Reverb"
effect_type_name(5)    →  null
```

The table is sparse — values 5, 14–16, 22–31 and many others are unassigned — so
`null` is expected and means "no effect type with that value", not a bug.

### Type categories

The Type values are not scattered: Appendix B allocates them in 16-value blocks
(a few split into half-blocks), one block per group of the device's type knob.
The registry carries those blocks as a second lookup, so a UI can label a slot
with its family without knowing the individual type:

```
effect_category_name(0)    →  null
effect_category_name(16)   →  "Wah"
effect_category_name(17)   →  "Shaper"
effect_category_name(76)   →  "Modulation"
effect_category_name(179)  →  "Reverb"
effect_category_name(300)  →  null
```

| Values | Category | Values | Category |
|---|---|---|---|
| 1–16 | Wah | 96–111 | Equalizer |
| 17–31 | Shaper | 112–120 | Booster |
| 32–48 | Distortion | 121–127 | Effect Loop |
| 49–63 | Dynamics | 128–143 | Pitch |
| 64–79 | Modulation | 144–175 | Delay |
| 80–95 | Phaser & Flanger | 176–207 | Reverb |

Unlike the name table this one is *dense within a block*, so a type value the
spec has no name for still gets a category — 76 is unnamed but plainly a
modulation. Value 0 ("empty") and anything past the last block return `null`.
The blocks are `effect_categories` in
[`../spec/effect-types.toml`](../spec/effect-types.toml).

Put together, this is the whole effect-block view the
[`meters`](07-realtime-status.md#the-meters-example) example renders:

```
A    ○ off   Wah Wah
C    ● ON    Kemper Drive
DLY  ● ON    Rhythm Delay
REV  ● ON    Easy Reverb
```

## Page 0 is dual-use

Page `$00` addresses **string tags** through `$03`/`$43` and **numeric
parameters** through `$01`/`$41`. The same number means different things
depending on which function reached it, so a decoder must key on the function
code before consulting the registry.

```
string_tag_name(1)   →  "Rig Name"
string_tag_name(32)  →  "Cabinet Name"
string_tag_name(99)  →  null
```

The string tags cover rig, amp and cabinet metadata: name, author, creation
date, comment, manufacturer, model, microphone, speaker. The numeric side of
page 0 is sparse — its members are number `$50`, the **Morph Button**, and
number `$77`, the **Morph Position**. Number `$0B` is *not* the morph: it reads
a constant 0 whether the rig is morphed or at base. See
[the morph](05-sysex-nrpn.md#the-morph).

## State routing

The registry names addresses; a second, much smaller table says what the
[device model](11-channels-and-data-paths.md) *does* with the handful of them it
stores. [`../spec/state.toml`](../spec/state.toml) declares one `[[route]]` row
per tracked field, and the generator flattens it into `STATE_ROUTES`: a list of
records sorted by flat address (`page × 128 + number`, or a bare extended
address), one per address, each carrying:

| Column | Meaning |
|---|---|
| `field` | which tree field the address writes — `rig_name`, `amp_gain`, `effect_on`, `current_bank`, … (the `Field` enum) |
| `slot` | the index for rows expanded per effect slot, per bank-preview slot, or per element of a spanned block; absent otherwise |
| `kind` | how the value decodes: `u14`, `u16`, `u7`, `bool`, `text`, `bpm`, or `multi` for one element of a block folded as a unit |
| `lane` | `fast` (event only — the meter frame, beat pulse, tuner deviance, morph button) or `slow` (republishes the snapshot) |
| `wire` | which channel may write it: `stream` for the realtime page and the momentaries, `control` for the morph position, `both` elsewhere |
| `dedupe` | whether an update repeating the stored value is a no-op |
| `request` | whether the connect-time sync asks the device for it |

Both wires feed the same rows: a `$01` at `0x0A/4` and a CBOR `[1, 1284, v]`
land in the same `amp_gain`, because both are address 1284. Rows reference
addresses by their `[well_known]` key rather than by number — `page =
"amp_page", number = "gain_number"` — so `parameters.toml` remains the only
place an address is written, and `state.toml` only adds routing. The eight
effect slots and the three bank-preview groups are written once and expanded
by the generator (`effect_param = "type"` becomes eight rows, one per slot in
signal-chain order; `bank_preview = "bank_rig_name_base"` becomes five).

Untracked addresses have no row: the stream still reports them as a generic
`ParamChanged`, and the snapshot is untouched. Page 0 being dual-use, a row's
`kind` also says which face of the page it accepts — a numeric arriving at a
`text` address is untracked.

Like everything else the generator emits, the table is data only. The fold that
turns a route into a store write is hand-written per language and held to
[`../spec/vectors/state.json`](../spec/vectors/state.json) and
[`../spec/vectors/cbor.json`](../spec/vectors/cbor.json).

## Where the data comes from

Nothing in this document is written by hand in any implementation. It all comes
from [`../spec/`](../spec):

| File | Supplies |
|---|---|
| `parameters.toml` | pages, the shared effect map, non-effect parameters, range entries, string tags, page-0 numerics, well-known addresses |
| `effect-types.toml` | the effect Type value → name table, and the category blocks |
| `controls.toml` | the CC vocabulary and the per-slot enable CCs |
| `meters.toml` | the realtime status block's field identities |
| `protocol.toml` | transport, handshake, framing and SysEx constants |
| `state.toml` | the state routing table: which addresses the device model stores, and how |

`codegen/generate.py` serializes those into a data-only module per language —
`rust/src/generated.rs`, `python/src/libkp/_generated.py`,
`swift/Sources/LibKP/Generated.swift` — which each implementation wraps in thin
lookup helpers. The tables are therefore provably identical across Rust, Python
and Swift, and the helpers are held to
[`../spec/vectors/params.json`](../spec/vectors/params.json) by all three test
suites.

**To add or correct a parameter, an effect type, a CC, or a tracked field, edit
the spec** and regenerate. Never edit a generated module, and never define a constant in an
implementation's own source. The full mechanism, and what a spec change obliges
you to do, is in
[Versioning & compatibility](10-versioning-and-compatibility.md).

## Sources

The page/number map, the shared effect-module parameter list, the string tags,
and the effect-type table are transcribed from the
[Kemper MIDI Parameter Documentation](https://www.kemper-amps.com/downloads/5/User-Manuals);
the effect-type category blocks are inferred from the value-range structure of
its Appendix B and the device's type-knob grouping. The Fixed-FX page and
several tuner and tempo addresses are credited to
[PySwitch](https://github.com/Tunetown/PySwitch). The page `$7C` meter-field
identities and the page-0 morph addresses were established by observed
experimentation.
See [../CREDITS.md](../CREDITS.md).
