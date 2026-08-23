# Control model

The same outcome can often be reached more than one way — gain is a Control
Change *and* an NRPN parameter; an effect can be switched by either. The
difference is not cosmetic: one is 7-bit and unobservable, the other is 14-bit
and readable back. This document sets out the three layers, and which one is
canonical for each capability.

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

A single object that ingests the stream, tracks state, and exposes a small
labelled API. Its methods split cleanly in two:

- **Parameters** — NRPN-backed, 14-bit, tracked in state. Setting one updates the
  model when the read-back reply arrives; reading one is answered from state, not
  the wire.
- **Actions** — CC-backed, momentary or expression. Nothing is stored, because
  there is nothing to store.

Most callers want the DeviceModel. The raw layers stay available for the cases it
does not cover.

## The rule

> **A settable value you also observe → NRPN**, exposed as a DeviceModel
> *parameter*.
> **A momentary or expression control with no stored value → CC**, exposed as a
> DeviceModel *action*.

That is the whole design. Gain has a value you can read back, so it is a
parameter even though CC 72 exists. Tap tempo has no value at all — tapping is
an event — so it is an action. Rig selection is an action because the device
offers no NRPN for it.

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
| 47 | Bank / Performance preselect | value = bank − 1 |
| 48 | Performance / Rig **up** | 1 |
| 49 | Performance / Rig **down** | 1 |
| 50–54 | Load Slot 1–5 | 1 |
| 75–78 | Effect Buttons I–IIII | 1 |
| 80 | Morph button | 1 rise / 0 fall |

### Bank Select

| CC | Name |
|---|---|
| 0 | Bank Select MSB |
| 32 | Bank Select LSB |

Standard MIDI bank select, paired with a Program Change for rig selection.

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
channel 0:

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
| Morph position (read) | NRPN `$00`/`$77` | — | CBOR-only; see [the morph](05-sysex-nrpn.md#the-morph) |
| Morph button (read) | NRPN `$00`/`$50` | — | momentary; the press/release the device reports |
| Looper transport | NRPN `$7D`/88–94 | — | latched values |
| Freeze per module | NRPN `$7D`/107–111, 113–115 | CC 35 (global) | per-slot state |
| **Rig select 1–5** | **CC 50–54** | — | no NRPN equivalent — a momentary action |
| **Rig up / down** | **CC 48 / 49** | — | navigation; momentary |
| **Bank preselect** | **CC 47** | — | navigation; loads on the next rig select |
| **Tap tempo** | **CC 30** | — | momentary event |
| **Tuner mode** | **CC 31** | state readable at NRPN `$7F`/126 | set momentarily, read as state |
| **Morph button** | **CC 80** | — | momentary, carries rise/fall |
| **Effect buttons I–IIII** | **CC 75–78** | — | momentary |
| **Freeze / infinity / rotary** | **CC 35 / 34 / 33** | — | momentary |
| **Wah / pitch / volume / morph pedal** | **CC 1 / 4 / 7 / 11** | — | live expression |

**The navigation controls are momentary, and the release is not optional.**
CC 48, CC 49 and CC 50–54 are button presses: value 1 presses, value 0 releases.
A press on its own *does* take effect — the device loads the target rig and
pushes the new bank's name preview — but if the release never arrives it
abandons the change and reloads the previous rig about two seconds later, which
looks exactly like the device spontaneously undoing the navigation. Sending
value 0 alone is inert, being the release of a press that never happened. The
`Control` type therefore renders `up`, `down` and `load_slot` as a press
immediately followed by its release, one 6-byte message; a caller that wants to
model a genuinely held button has to build the two Control Changes itself.

**The device says where it is, at two extended addresses.** Its current bank
(100701) and rig slot (100702), both 0-based, read with a `$46` request and
pushed unasked as `$06` whenever either moves — from the front panel as readily
as from a controller. Together they are a flat rig index, `bank × 5 + slot`:
index 123 is bank 25, slot 4. A client reads them once at connect and then
listens. See [the position report](05-sysex-nrpn.md#the-position-report).

That index is also the only address that names a rig **outside** the current
bank, so it is what navigation is computed in: ±1 is the next or previous rig,
±5 the next or previous bank, and any rig is reachable by sending the absolute
bank preselect (CC 47) followed by the slot load. Bank boundaries stop being
special. Note that CC 48 / 49 are *not* a general bank control — on at least one
device they alternate between two banks rather than stepping — so a client that
needs to reach an arbitrary bank should use CC 47 and not step.

How many rigs a device holds varies, and nothing in the protocol announces it.
Rather than assume a ceiling, aim: the device stays put if the target does not
exist, and its next position report says where it actually is.

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

## The DeviceModel surface

**Parameters** — NRPN-backed, state-tracked:

```
set_gain            set_rig_volume        set_main_volume
set_monitor_volume  set_effect_enabled(slot, on)
set_effect_mix(slot, value)               set_tempo_bpm
set_param(page, number, value)            ← generic escape hatch
```

**Actions** — CC-backed, momentary or expression, nothing stored:

```
select_rig(1..5)    rig_up    rig_down    bank(n)
tap_tempo           tuner_mode(on)        morph_button(rise)
effect_button(1..4) freeze(on)            rotary_fast(on)
delay_infinity(on)  toggle_all_modules
wah_pedal  pitch_pedal  volume_pedal  panorama  morph_pedal
```

**Requests** — read-only, they change nothing on the device:

```
refresh_rig         refresh_bank          request_param(page, number)
request_string(page, number)              request_render(page, number, value)
```

`refresh_bank` issues the fifteen `$47` extended-string requests for the current
bank's rig/amp/cabinet names; `connect` runs it once alongside `refresh_rig`.

A caller that wants "turn reverb off" calls `set_effect_enabled("REV", false)`,
then `request_param(page, 3)` and sees the change reflected in the model's state
once the reply lands. A caller building a foot controller uses the actions.
Anyone needing a control the model does not name reaches the complete raw
vocabulary through the control module and its send-control entry point, or an
arbitrary address through `set_param`.

## Consequences to design around

**Program Change feedback does not come back.** Program Change, Note On and Note
Off are inert in the device's network MIDI encoder — switching rigs produces no
Program Change on the stream, and nothing but SysEx ever comes back. A client
that wants to display the current rig must **request the rig name** (`$43`,
page 0, number 1) after switching. See [SysEx / NRPN dialect](05-sysex-nrpn.md).

**The rig name is not a position.** It tells you *what* is loaded, not *where* it
sits. Matching the loaded name against the [bank preview](09-parameter-registry.md)
recovers the slot, but not the bank number, and it is ambiguous when two slots
share a name. The position itself comes from the two extended addresses above —
`refresh_position` reads them, and the device pushes them thereafter. Before a
streaming session exists the same two values are in the
[CBOR channel](06-cbor-channel.md) state dump, which is what
`StateSnapshot::fetch` reads; a client that opens the streaming session anyway
does not need it.

**A write is not echoed.** Established by observed experimentation, the device
applies a `$01` write without reporting it back on a plain streaming session, so
a store that must confirm the applied value follows the write with a `$41`
request. The reply lands roughly a second later: order state changes before
read-backs, and give a confirmation poll at least a couple of seconds before
deciding a write failed.

**A rig change is a windfall.** The device unprompted dumps the entire new rig —
name, author, comment, amp, cabinet, microphone, speaker, every effect slot's
type, and the rig settings. One CC 50 costs three bytes and yields a complete
patch description; there is no need to enumerate it with requests.

**CC is coarse.** CC 72 gives gain 128 steps; NRPN `$0A`/4 gives 16384. For
anything a user drags with a mouse, use NRPN.

## Sources

The Control Change map and the NRPN parameter grammar follow the
[Kemper MIDI Parameter Documentation](https://www.kemper-amps.com/downloads/5/User-Manuals),
cross-checked against [PySwitch](https://github.com/Tunetown/PySwitch), which is
credited for the tuner-mode state address and the rig/bank selection scheme. The
absence of Program Change feedback on the network link, and the fact that a
`$01` write is applied without being echoed, were established by observed
experimentation. See [../CREDITS.md](../CREDITS.md).
