# Control model

The same outcome can often be reached more than one way — gain is a Control
Change *and* an NRPN parameter; an effect can be switched by either. The
difference is not cosmetic: one is 7-bit and unobservable, the other is 14-bit
and echoed back. This document sets out the three layers, and which one is
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
(0–16383), settable with `$01`, readable with `$41`/`$42`/`$43`, and — decisively
— **the device echoes every change on the same stream**, including changes made
at the front panel. That echo is what makes a consistent state model possible.

### 3. The DeviceModel — the curated surface

A single object that ingests the stream, tracks state, and exposes a small
labelled API. Its methods split cleanly in two:

- **Parameters** — NRPN-backed, 14-bit, tracked in state. Setting one updates the
  model when the echo arrives; reading one is answered from state, not the wire.
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
| Main / monitor volume | NRPN `$7F`/0, `$7F`/2 | monitor also CC 73 | precision + read-back |
| Effect on/off | NRPN `<slot>`/3 | CC 17–29 | tracked in the slot state |
| Effect mix | NRPN `<slot>`/4 | CC 68 / 70 (delay, reverb only) | precision + read-back |
| Effect **type** | NRPN `<slot>`/0, **read-only** | — | set by loading a rig, not over MIDI |
| Tempo in BPM | NRPN `$04`/0 (bpm × 64) | — | it is a value; tapping is the action |
| Morph position (read) | NRPN `$00`/`$0B` | — | observed state |
| Looper transport | NRPN `$7D`/88–94 | — | latched values |
| Freeze per module | NRPN `$7D`/107–111, 113–115 | CC 35 (global) | per-slot state |
| **Rig select 1–5** | **CC 50–54** | — | no NRPN equivalent — an action |
| **Rig up / down** | **CC 48 / 49** | — | navigation |
| **Bank preselect** | **CC 47** | — | navigation; loads on the next rig select |
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

A caller that wants "turn reverb off" calls `set_effect_enabled("REV", false)`
and sees the change reflected in the model's state once the echo lands. A caller
building a foot controller uses the actions. Anyone needing a control the model
does not name reaches the complete raw vocabulary through the control module and
its send-control entry point, or an arbitrary address through `set_param`.

## Consequences to design around

**Program Change feedback does not come back.** Program Change, Note On and Note
Off are inert in the device's network MIDI encoder — switching rigs produces no
Program Change on the stream. A client that wants to display the current rig must
**request the rig name** (`$43`, page 0, number 1) after switching. See
[SysEx / NRPN dialect](05-sysex-nrpn.md).

**Read-back is not immediate.** A `$01` write is echoed roughly a second later.
Order state changes before read-backs, and give a confirmation poll at least a
couple of seconds before deciding a write failed.

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
absence of Program Change feedback on the network link was established by
observed experimentation. See [../CREDITS.md](../CREDITS.md).
