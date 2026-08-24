# Realtime status & meters

An open [MIDI3 stream](04-midi3-framing.md) pushes a realtime status block about
**20 times a second**, with no beacon, no subscription, and no request. It is
the device's live telemetry: the tuner strobe and the output meters, eleven
14-bit values in one message.

## The message

The block is an ordinary `$02` **Multi Parameter Change** — nothing special about
its transport, only about its address:

```
F0 00 20 33 00 00 02 00 7C 4E <11 × 14-bit values> F7
                     │  │  │  └── eleven value pairs, 22 bytes
                     │  │  └───── number $4E = 78, the first of eleven
                     │  └──────── page $7C = realtime / meters
                     └─────────── function $02 = multi parameter change
```

Because `$02` applies its values to **consecutive numbers**, the eleven meters
are eleven ordinary NRPN parameters at addresses **`$7C`/78 through `$7C`/88**.
They can be read individually with `$41`; the block is simply the efficient
push form.

A `$02` at the base is taken as the frame **whatever its length**: a truncated
read zero-fills the missing tail, and values past the eleven are ignored. Only
the base address decides — a `$02` anywhere else is an ordinary multi-parameter
write. A frame is never split into per-address generic reports, so a short read
cannot flood the event stream at meter rate.

| | |
|---|---|
| Page | `$7C` (124) |
| First number | `$4E` (78) |
| Count | 11 |
| Value range | 0–16383 |
| Update rate | ~20 Hz |
| Total message | 33 bytes of SysEx, 44 bytes framed (11 frames) |

Constants: [`../spec/meters.toml`](../spec/meters.toml) `[block]`; well-known
addresses `page_realtime`, `meter_block_number`, `meter_count` in
[`../spec/parameters.toml`](../spec/parameters.toml).

## Worked example

From [`../spec/vectors/state.json`](../spec/vectors/state.json):

```
f0 00 20 33 00 00 02 00 7c 4e 00 64 01 48 02 2c 3e 40 5d 60 27 08
46 28 1f 20 00 00 2e 70 00 00 f7
```

| Index | NRPN | Bytes | Value | Field |
|---|---|---|---|---|
| v0 | `$7C`/78 | `00 64` | 100 | Tuner strobe segment (phase-low) |
| v1 | `$7C`/79 | `01 48` | 200 | Tuner strobe segment (phase-mid) |
| v2 | `$7C`/80 | `02 2c` | 300 | Tuner strobe segment (phase-high) |
| v3 | `$7C`/81 | `3e 40` | 8000 | Tuner strobe phase |
| v4 | `$7C`/82 | `5d 60` | 12000 | Stack level, pre-rig-volume |
| v5 | `$7C`/83 | `27 08` | 5000 | Stack power |
| v6 | `$7C`/84 | `46 28` | 9000 | Rig output level, post-rig-volume |
| v7 | `$7C`/85 | `1f 20` | 4000 | Rig output power |
| v8 | `$7C`/86 | `00 00` | 0 | unused |
| v9 | `$7C`/87 | `2e 70` | 6000 | Loudness, slow RMS |
| v10 | `$7C`/88 | `00 00` | 0 | unused |

Recorded status and meter streams are replayed by every implementation's test
suite from [`../spec/captures`](../spec/captures), which asserts the eleven
decoded values of each status frame against the recording.

## The eleven fields

### v0–v3 — the tuner strobe

The tuner is reported as a **strobe**, the way a mechanical strobe tuner works,
not as a needle.

**v3 is a wrapping phase.** It runs on a 0–16383 circle and rotates at a rate
proportional to how far the played note is from pitch. When the note is in tune
the phase is **stationary** — that, not any particular value, is the in-tune
condition. Sharp and flat rotate in opposite directions: holding a bend makes v3
fall continuously through multiple wraparounds, while a slightly flat note
climbs slowly. The value itself is meaningless in isolation; its **rate of
change** is the measurement.

Consequences for a client:

- Never render v3 as a position on a scale. Render its motion — a rotating mark,
  a scrolling pattern, a spinning segment — and let "not moving" read as "in
  tune".
- A single frame cannot tell you anything. You need at least two, and the
  wraparound must be handled: the shortest signed delta on the circle, not
  `new - old`.

**v0, v1 and v2 are display-segment drivers** for that phase. They are fired
purely by where the phase currently sits, crossfading as it rotates: v0 covers
the low part of the circle, v1 the middle, v2 the high, with overlapping bands
so that adjacent segments blend rather than jump. They carry no pitch
information the phase does not already carry — they exist so a device with three
lamps can drive them directly.

**When no pitch is being tracked, all four read 0.** A silent input parks the
whole group at zero while the stream keeps running at 20 Hz. Do not mistake the
idle state for "perfectly in tune"; check whether the group is all-zero first.

For an absolute, single-frame pitch reading use the separate parameter
[below](#tuner-deviance) instead.

Constants: `[strobe]` in [`../spec/meters.toml`](../spec/meters.toml) —
`phase_index = 3`, `segment_indices = [0, 1, 2]`.

### v4–v7 — the two chain taps

v4 and v6 are amplitude taps at two points in the signal chain, and v5 and v7
are their corresponding power values (each roughly the square of its level, on a
much smaller scale).

| | Tap | Follows | Ignores |
|---|---|---|---|
| v4 / v5 | **stack**, before rig volume | playing dynamics | rig volume, all output volumes |
| v6 / v7 | **rig output**, after rig volume | playing dynamics *and* rig volume | monitor, headphone and main output volumes, panorama |

They are **not** a stereo pair — panning hard left or right moves neither — and
they are **not** output meters: sweeping the monitor or headphone volume across
its whole range leaves both untouched. The pair differs by exactly one gain
stage, the rig volume, which is what makes v6 the right choice for a "what is
leaving this rig" meter and v4 the right choice for a "how hard is this being
driven" meter.

### v8 and v10 — unused

Both read 0 in all observed states. Decode them, keep them in the raw array, and
do not display them.

### v9 — loudness

A slow-RMS loudness of the output. It rises and decays much more slowly than v6
and tails off after playing stops, which makes it the field to use for a
level-over-time or "how loud is this rig" reading rather than a peak meter.

### Render hints

[`../spec/meters.toml`](../spec/meters.toml) tags each field with a `render`
hint so all three implementations present the block the same way:

| Hint | Fields | Meaning |
|---|---|---|
| `strobe` | v0, v1, v2, v3 | tuner strobe components — motion, not position |
| `bar` | v4, v6, v9 | level meters, sensible as a 0–16383 bar |
| `extra` | v5, v7, v8, v10 | derived or unused; hide unless a raw view is requested |

## Related realtime addresses

Page `$7C` carries two more values that arrive as ordinary `$01` Single
Parameter Changes rather than in the block.

### Beat pulse

```
F0 00 20 33 00 00 01 00 7C 00 <value> F7        page $7C, number 0
```

Toggles between 0 and 16383 in time with the rig tempo. It is pushed **without a
beacon**, alongside the status block, and is the direct source for a blinking
tempo indicator. The numeric tempo itself is a different parameter — page `$04`,
number 0, holding bpm × 64 (see [SysEx / NRPN dialect](05-sysex-nrpn.md)).

Constant: `beat_pulse_number = 0x00`.

### Tuner deviance

```
F0 00 20 33 00 00 01 00 7C 0F <value> F7        page $7C, number 15
```

The absolute pitch deviance as a single 14-bit value, with **8192 = in tune**.
Unlike the strobe phase this is meaningful in one frame: below 8192 is flat,
above is sharp, and the distance from 8192 is the magnitude. Use it for a needle
or a cents readout; use the strobe for a strobe.

Constants: `tuner_deviance_number = 0x0F`, `tuner_in_tune_center = 8192`, and
`tuner_in_tune_window = 350` — the ± band all three implementations treat as
"in tune" so their displays agree.

The note being tracked is reported separately at page `$7D`, number 84
(`tuner_note_number = 0x54`).

## The `meters` example

Each implementation ships the same example — `rust/examples`,
`python/src/libkp/examples`, `swift/Sources/meters` — and it exists to make this
document concrete. It opens a session, ingests the stream, and renders a live
terminal view:

- the **current rig**, from the string tags the device dumps on a rig change;
- the **effect blocks**, slot by slot, with each one's type name and on/off state;
- the **tuner**, driven from the strobe: the note from `$7D`/84, the in-tune
  condition from the deviance at `$7C`/15 against `tuner_in_tune_center` ±
  `tuner_in_tune_window`, and the animated strobe from the v3 phase and its
  three segment drivers;
- the **output meters** — the three `bar` fields, v4, v6 and v9 — as level bars
  scaled against 16383;
- the **`extra`** fields only when a raw view is asked for.

Every number on that screen arrives unrequested. The example sends no beacon and
issues no polling loop; it connects, and the device does the rest.

## Sources

The block layout follows the documented Multi Parameter Change format from the
[Kemper MIDI Parameter Documentation](https://www.kemper-amps.com/downloads/5/User-Manuals);
the tuner-deviance, tuner-note and beat-pulse addresses are credited to
[PySwitch](https://github.com/Tunetown/PySwitch). The identities of the eleven
fields — the strobe phase and its segment drivers, the two chain taps and their
power values, the loudness field, and the two unused slots — were established by
observed experimentation. See [../CREDITS.md](../CREDITS.md).
