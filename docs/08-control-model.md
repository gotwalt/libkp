# Control model

The same outcome can often be reached more than one way — gain is a Control
Change *and* an NRPN parameter; an effect can be switched by either. The
difference is not cosmetic: one is 7-bit and unobservable, the other is 14-bit
and readable back. This document sets out the three layers, which one is
canonical for each capability, and the one capability — loading a rig — that
the model keeps to itself.

## Three layers

### 1. Raw MIDI Control Change — fire-and-forget

Ordinary 7-bit channel-voice MIDI, carried on the stream inside
[MIDI3 frames](04-midi3-framing.md) like anything else:

```
B0 | channel   <controller>   <value>          Control Change
C0 | channel   <program>                       Program Change
```

Values are masked to 7 bits (0–127). Only the controls that have an assigned CC
number exist at this layer, and **nothing is read back** — there is no way to
ask what a CC's current value is. It is the Profiler's performance-control
surface: momentary switches, navigation, and expression pedals.

Constants: [`../spec/controls.toml`](../spec/controls.toml); vectors:
[`../spec/vectors/controls.json`](../spec/vectors/controls.json).

### 2. NRPN parameters — 14-bit and observable

`$01` Single Parameter Change and its request forms, addressed by
`(page, number)` — see [SysEx / NRPN dialect](05-sysex-nrpn.md). Every parameter
the device has is reachable here, not just the ones with a CC. Values are 14-bit
(0–16383), settable with `$01`, and — decisively — **readable back** with
`$41`/`$42`/`$43`, whose replies arrive on the same stream. That read-back is
what makes a consistent state model possible. A `$01` write is applied silently:
established by observed experimentation, the device does not echo it, so a
client that wants its store to confirm a write issues the matching `$41`.

### 3. The DeviceModel — the curated surface

A single object that owns both links to the device, tracks state, and exposes
a small labelled API ([Channels and data paths](11-channels-and-data-paths.md)).
Its methods split into four groups:

- **Parameters** — NRPN-backed, 14-bit, tracked in state. Setting one is a
  `$01` write; the snapshot confirms it when a request's reply lands.
- **Requests** — read-only questions with an answer, through the request lane:
  each returns the value that answers it and folds it into the tree on the way.
- **Actions** — CC-backed, momentary or expression. Nothing is stored, because
  there is nothing to store.
- **Navigation** — a rig load, which is an action with a consequence the device
  must be protected from, and so goes through the model's Navigator rather than
  the raw layers.

Most callers want the DeviceModel. The raw layers stay available for the cases
it does not cover — all but the rig loads.

## The rule

> **A settable value you also observe → NRPN**, exposed as a DeviceModel
> *parameter*.
> **A momentary or expression control with no stored value → CC**, exposed as a
> DeviceModel *action*.
> **A rig load → the Navigator**, which sends the CC pair itself, one load at a
> time.

That is the whole design. Gain has a value you can read back, so it is a
parameter even though CC 72 exists. Tap tempo has no value at all — tapping is
an event — so it is an action. Rig selection has no NRPN and is a CC on the
wire, but two of them too close together wedge the device, so the model does
not let a caller send one: it lets a caller *aim*.

## The Control Change vocabulary

### Continuous controllers

| CC | Name | Notes |
|---|---|---|
| 1 | Wah Pedal | |
| 4 | Pitch Pedal | |
| 7 | Volume Pedal | |
| 10 | Panorama | |
| 11 | Morph Pedal | |
| 68 | Delay Mix | coarse; NRPN `<slot>/4` is finer |
| 69 | Delay Feedback | |
| 70 | Reverb Mix | coarse |
| 71 | Reverb Time | |
| 72 | Gain | coarse; NRPN `$0A`/4 is finer |
| 73 | Monitor (Output) Volume | coarse |

### Switches

| CC | Name | Value convention |
|---|---|---|
| 16 | Toggle all modules A–REV | any |
| 17, 18, 19, 20 | Module A, B, C, D on/off | 1 on / 0 off |
| 22 | Module X on/off | 1 on / 0 off |
| 24 | Module MOD on/off | 1 on / 0 off |
| 26 | Module DLY off, **no** spillover | |
| 27 | Module DLY on/off, with spillover | 1 on / 0 off |
| 28 | Module REV off, **no** spillover | |
| 29 | Module REV on/off, with spillover | 1 on / 0 off |
| 30 | Tap Tempo | any value taps |
| 31 | Tuner Mode | 1 open / 0 close |
| 33 | Rotary speaker speed | 1 fast / 0 slow |
| 34 | Delay Infinity | 1 on / 0 off |
| 35 | Delay + Reverb Freeze | 1 on / 0 off |
| 47 | Bank / Performance preselect | value = bank − 1; loads nothing by itself |
| 48 | Performance / Rig **up** | 1 — **a rig load**; Navigator only |
| 49 | Performance / Rig **down** | 1 — **a rig load**; Navigator only |
| 50–54 | Load Slot 1–5 | 1 — **a rig load**; Navigator only |
| 75–78 | Effect Buttons I–IIII | 1 |
| 80 | Morph button | 1 rise / 0 fall |

CC 48–54 are `rig_load_controllers` in `spec/protocol.toml`: the model's
`send_control` and `send_raw` refuse them ([below](#loading-a-rig-the-navigator)).

### Bank Select

| CC | Name |
|---|---|
| 0 | Bank Select MSB |
| 32 | Bank Select LSB |

Standard MIDI bank select, paired with a Program Change for rig selection —
which makes both of them rig loads, refused by the model the same way.

### Effect slot → enable CC

| Slot | CC |
|---|---|
| A | 17 |
| B | 18 |
| C | 19 |
| D | 20 |
| X | 22 |
| MOD | 24 |
| DLY | **27** (with spillover) |
| REV | **29** (with spillover) |

DLY and REV use their with-spillover controllers so that switching a delay or
reverb off lets its tail ring out rather than cutting it dead.

### Worked bytes

From [`../spec/vectors/controls.json`](../spec/vectors/controls.json), all on
channel 0. These pin the `Control` type's encoding; the rig-load rows are what
the Navigator puts on the wire, and not something the model sends on request:

| Intent | Bytes |
|---|---|
| Load rig slot 1 | `b0 32 01` |
| Load rig slot 3 | `b0 34 01` |
| Rig up | `b0 30 01` |
| Rig down | `b0 31 01` |
| Preselect bank 3 | `b0 2f 03` |
| Tap tempo | `b0 1e 01` |
| Open the tuner | `b0 1f 01` |
| Close the tuner | `b0 1f 00` |
| Module REV on | `b0 1d 01` |
| Module DLY off | `b0 1b 00` |
| Gain to 64 | `b0 48 40` |
| Morph button, rise | `b0 50 01` |
| Program Change 5 | `c0 05` |
| Bank Select MSB 0, LSB 3 | `b0 00 00` `b0 20 03` |

The MIDI channel occupies the low nibble of the status byte, so the same gain
message on channel 15 is `bf 48 40`.

Argument handling is uniform across the three implementations and pinned by the
vectors: values are **masked** to 7 bits (a wah-pedal value of 200 sends `0x48`),
slot numbers are **clamped** to 1–5, and effect-button numbers to 1–4.

## Canonical mechanism per capability

| Capability | Canonical | Also exists as | Why |
|---|---|---|---|
| Set gain | NRPN `$0A`/4 | CC 72 | 14-bit precision, and it reads back |
| Rig volume | NRPN `$04`/1 | — | value with read-back |
| Main / monitor / headphone volume | NRPN `$7F`/0, `$7F`/2, `$7F`/1 | monitor also CC 73 | precision + read-back |
| Master volume (read) | NRPN `$7F`/1 = `$7F`/2 | — | the physical knob; read-only (see below) |
| Bank rig/amp/cab names (read) | ext-string page `$96`/0–14 | — | the current bank's five-slot preview |
| Effect on/off | NRPN `<slot>`/3 | CC 17–29 | tracked in the slot state |
| Effect mix | NRPN `<slot>`/4 | CC 68 / 70 (delay, reverb only) | precision + read-back |
| Effect **type** | NRPN `<slot>`/0, **read-only** | — | set by loading a rig, not over MIDI |
| Tempo in BPM | NRPN `$04`/0 (bpm × 64) | — | it is a value; tapping is the action |
| Morph position (read) | NRPN `$00`/`$77`, control link only | — | see [the morph](05-sysex-nrpn.md#the-morph) |
| Morph button (read) | NRPN `$00`/`$50` | — | momentary; the press/release the device reports |
| Current position (read) | ext `$06` at 100701/100702 | the CBOR dump and pushes | pushed on every change; see [the position report](05-sysex-nrpn.md#the-position-report) |
| Looper transport | NRPN `$7D`/88–94 | — | latched values |
| Freeze per module | NRPN `$7D`/107–111, 113–115 | CC 35 (global) | per-slot state |
| **Load a rig** | **the Navigator** (`navigate_to`, `step_rig`, `step_bank`, `select_slot`) | CC 47 + CC 50–54 on the wire; CC 48/49; Program Change; Bank Select | overlapping loads wedge the device; only one sender can keep them apart |
| **Bank preselect** | **CC 47** | — | loads nothing by itself; the Navigator sends its own |
| **Tap tempo** | **CC 30** | — | momentary event |
| **Tuner mode** | **CC 31** | state readable at NRPN `$7F`/126 | set momentarily, read as state |
| **Morph button** | **CC 80** | — | momentary, carries rise/fall |
| **Effect buttons I–IIII** | **CC 75–78** | — | momentary |
| **Freeze / infinity / rotary** | **CC 35 / 34 / 33** | — | momentary |
| **Wah / pitch / volume / morph pedal** | **CC 1 / 4 / 7 / 11** | — | live expression |

Two rows deserve a second look. **Effect type is read-only**: there is no way to
change what an effect *is* over MIDI — that happens by loading a rig — so a
client reads `<slot>`/0 and looks the value up in the effect-type table. And
**tuner mode is split**: CC 31 sets it, NRPN `$7F`/126 reports it, so a UI that
wants a correct toggle state must read one address while writing to another.

**Master volume is a potentiometer, not a stored value.** The device's physical
master-volume knob drives Headphone (`$7F`/1) and Monitor (`$7F`/2) together, 1:1,
under the default output routing (Main, `$7F`/0, is independent). It has an
absolute position — unlike every other knob, which is an endless encoder — so the
pot is ground truth: there is no soft-takeover, and a value written to `$7F`/1 or
`$7F`/2 is authoritative only until the knob next moves. The model therefore
exposes master volume as a **read-only readout** (`Output.master_volume`, which
reports Headphone and falls back to Monitor) and ships no `set_master_volume`.

**Bank names are a five-slot preview.** Page `$96` (150) carries the loaded
bank's five rig names (numbers 0–4), their amps (5–9) and cabinets (10–14). The
device pushes the whole block on a bank change, and it is readable on demand as
extended strings (function `$47` → `$07`). The model folds it into `state.bank`.

## Loading a rig: the Navigator

**The navigation controls are momentary, and the release is not optional.**
CC 48, CC 49 and CC 50–54 are button presses: value 1 presses, value 0 releases.
A press on its own *does* take effect — the device loads the target rig and
pushes the new bank's name preview — but if the release never arrives it
abandons the change and reloads the previous rig about two seconds later, which
looks exactly like the device spontaneously undoing the navigation. Sending
value 0 alone is inert, being the release of a press that never happened. The
`Control` type therefore renders `up`, `down` and `load_slot` as a press
immediately followed by its release, one 6-byte message.

**Two loads too close together wedge the device.** Established by observed
experimentation against a Profiler Player (firmware 14.2.1): two rig loads
issued about 8 ms apart are both answered normally, and then the device closes
the session some twenty seconds later and refuses TCP until it is
power-cycled. Nothing in the immediate response says harm was done. So the
model never sends a load on request. `send_control` refuses `LoadSlot`, `Up`,
`Down`, `ProgramChange` and `BankSelect`, and `send_raw` refuses any buffer
carrying a Program Change status (`0xC0`–`0xCF`) or a Control Change on one of
`rig_load_controllers` (CC 48–54) — every status byte in the buffer is
examined, so a load cannot ride in behind another message — both with
`RigLoadRequiresNavigator` before a byte goes out. The bare bank preselect
(`bank`, CC 47) still passes: it loads nothing.

**The device says where it is, at two extended addresses.** Its current bank
(100701) and rig slot (100702), both 0-based, read with a `$46` request and
pushed unasked as `$06` whenever either moves — from the front panel as readily
as from a controller — and carried on the CBOR channel too. Together they are a
flat rig index, `bank × 5 + slot`: index 123 is bank 25, slot 4. That index is
the only address that names a rig **outside** the current bank, so it is what
navigation is computed in: ±1 is the next or previous rig, ±5 the next or
previous bank, and any rig is reachable by sending the absolute bank preselect
(CC 47) followed by the slot load. Bank boundaries stop being special. CC 48 /
49 are *not* a general bank control — on at least one device they alternate
between two banks rather than stepping — which is one more reason the Navigator
addresses every move as a flat index and never uses them.

**A caller aims.** The four entry points return at once:

```
navigate_to(index)      aim at a flat, 0-based rig index
step_rig(delta)         ±1 the next/previous rig, from the aimed index, floored at 0
step_bank(forward)      ±5: the same slot a bank over
select_slot(slot)       slot 1–5 of the aimed bank
```

The aim lands in `state.navigation` immediately (`aim`, `in_flight`), so a slot
highlight answers every tap; `state.aimed_rig_index()` — the aim while there is
one, the device's own position otherwise — is what a rig browser highlights and
what the steppers step from, so two taps inside the device's reporting delay
compose instead of both stepping from the same stale index. The steppers do
nothing while no position is known (there is nothing to step from), and a step
that lands where the aim already is sends nothing.

**The Navigator rations the sending.** The first aim goes out now as the
documented pair — CC 47 for `index / 5`, then CC 50–54 for `index % 5` — and is
*in flight* for `rig_load_settle_ms` (500 ms). Every aim that arrives meanwhile
only moves the target; when the settle elapses the final target is sent, once.
A burst of taps therefore costs **two loads** however long it is, and two loads
can never overlap. The settle is the measured edge: after a load the device
reports its position within ~40 ms on the stream and has pushed the entire
landed rig on both wires by ~400 ms, and the flight is never shortened by the
early position report because those pushes are still streaming when it lands.
Since the device pushes the whole landed rig itself, there is no read-back after
a move.

**The device confirms, or it does not.** A position report that matches the
aim, from either wire, retires it with `NavigationSettled`. Re-loading the rig
already loaded still confirms: the device's position push carries the values
already stored, and the tree dedupes the *event*, but the report itself still
reaches the Navigator — confirmation rides on pushes, not on changes. How many
rigs a device holds varies and nothing in the protocol announces it, so
nothing here assumes a ceiling: aim past the end and the device stays put and
says so in its position push, which does not match; the aim is kept for
`pending_window_ms` (1.5 s) after the move settled and then dropped with
`NavigationDropped`, and the device's own position is the truth again. The
wire itself has a ceiling, though: the bank preselect is a 7-bit CC value, so
an index at or past `128 × bank_slots` cannot be expressed at all and is
dropped at once — masking the bank would silently load a real but wrong rig.
An index already on the wire is never sent again while it stands. With the
stream down an aim is dropped at once, the same way, rather than raising: an
aim is a destination, not a command that failed.

The state machine is pure — `NavigatorState`, four fields and four inputs —
and pinned by [`../spec/vectors/navigation.json`](../spec/vectors/navigation.json),
so every language runs the same one.

## The DeviceModel surface

**Parameters** — NRPN-backed, state-tracked:

```
set_gain            set_rig_volume        set_main_volume
set_monitor_volume  set_effect_enabled(slot, on)
set_effect_mix(slot, value)               set_tempo_bpm
set_param(page, number, value)            ← generic escape hatch
```

**Requests** — read-only, they change nothing on the device, and each returns
the value that answers it:

```
request_param(page, number)     → the 14-bit value      ($41 → $01)
request_string(page, number)    → the text              ($43 → $03)
request_ext_param(address)      → the value             ($46 → $06)
request_ext_string(address)     → the text              ($47 → $07)
request_render(page, number, value) → the display text  ($7C → $3C)
refresh                         every request = true row of the routing table (46)
refresh_rig / refresh_bank / refresh_position   its subsets
```

Each rides the request lane — at most 16 on the wire, a 300 ms timeout, never
retried, the morph refused as `Unreadable` without sending — see
[Requests and replies](05-sysex-nrpn.md#requests-and-replies). `connect` runs
`refresh` as its sync burst.

**Actions** — CC-backed, momentary or expression, nothing stored:

```
bank(n)             ← the preselect alone; loads nothing
tap_tempo           tuner_mode(on)        morph_button(rise)
effect_button(1..4) freeze(on)            rotary_fast(on)
delay_infinity(on)  toggle_all_modules
wah_pedal  pitch_pedal  volume_pedal  panorama  morph_pedal
send_control(control)                     ← any raw control but a rig load
send_raw(bytes)                           ← any MIDI bytes but a rig load
```

**Navigation** — the only way to load a rig:

```
navigate_to(index)  step_rig(delta)  step_bank(forward)  select_slot(slot)
```

A caller that wants "turn reverb off" calls `set_effect_enabled("REV", false)`,
then `request_param(0x3D, 3)`, and gets `0` back with the change reflected in
the model's state. A caller building a foot controller uses the actions for
everything but the rig buttons, which it forwards to the Navigator. Anyone
needing a control the model does not name reaches the complete raw vocabulary
through `send_control`, or an arbitrary address through `set_param`, or the
wire itself through `send_raw`.

## Consequences to design around

**Program Change feedback does not come back.** Program Change, Note On and Note
Off are inert in the device's network MIDI encoder — switching rigs produces no
Program Change on the stream, and nothing but SysEx ever comes back. Where the
device *is* it says as a `$06` position report, which the model tracks; what is
loaded it says as the rig's strings, which it pushes after every load. See
[SysEx / NRPN dialect](05-sysex-nrpn.md).

**The rig name is not a position.** It tells you *what* is loaded, not *where* it
sits. Matching the loaded name against the [bank preview](09-parameter-registry.md)
recovers the slot, but not the bank number, and it is ambiguous when two slots
share a name. The position itself comes from the two extended addresses above —
the sync burst reads them at connect, the device pushes them thereafter on both
wires, and the CBOR dump carries them too — so `state.current_rig_index()` is
always the device's own answer.

**A write is not echoed.** Established by observed experimentation, the device
applies a `$01` write without reporting it back on a plain streaming session, so
a store that must confirm the applied value follows the write with
`request_param`, which returns the value and folds it into the tree. The device
answers within tens of milliseconds; order the write before its read-back and
the writer sends them in that order.

**A rig change is a windfall.** The device unprompted dumps the entire new rig —
name, author, comment, amp, cabinet, every effect slot's type and state, and
the rig settings — on both wires, done within ~400 ms. One load yields a
complete patch description; there is no need to enumerate it with requests,
which is why the Navigator issues none.

**CC is coarse.** CC 72 gives gain 128 steps; NRPN `$0A`/4 gives 16384. For
anything a user drags with a mouse, use NRPN.

## Sources

The Control Change map and the NRPN parameter grammar follow the
[Kemper MIDI Parameter Documentation](https://www.kemper-amps.com/downloads/5/User-Manuals),
cross-checked against [PySwitch](https://github.com/Tunetown/PySwitch), which is
credited for the tuner-mode state address and the rig/bank selection scheme. The
absence of Program Change feedback on the network link, the fact that a `$01`
write is applied without being echoed, the delayed fuse behind overlapping rig
loads, and the timing of a rig load's pushes were established by observed
experimentation. See [../CREDITS.md](../CREDITS.md).
