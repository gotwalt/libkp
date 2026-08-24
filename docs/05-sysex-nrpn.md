# SysEx / NRPN dialect

Everything the Profiler says about its own state, and almost everything a
controller says back, is a Kemper System Exclusive message. The dialect is a
SysEx envelope around the classic MIDI **NRPN** model: a parameter is a
`(page, number)` address and a 14-bit value.

## The envelope

```
F0  00 20 33  <product>  <device>  <function>  <instance>  <page>  <number>  <values…>  F7
│   └───┬───┘  └──┬───┘  └──┬───┘  └───┬────┘  └───┬────┘  └──┬─┘  └───┬──┘  └───┬───┘  │
│    manufacturer │        │          │           │          │        │         │      end
│                 │        │      message kind    │       address MSB │      payload
│              product   device                instance         address LSB
start
```

| Field | Values |
|---|---|
| Manufacturer id | `00 20 33` (Kemper) |
| `product` | `$00` Profiler · `$02` Profiler Player class |
| `device` | `$7F` = omni (address any device). Requests use `$7F`. |
| `function` | the message kind — see the table below |
| `instance` | `$00` in all traffic this library handles |
| `page` | NRPN address MSB, 0–127 |
| `number` | NRPN address LSB, 0–127 |

Constants: [`../spec/protocol.toml`](../spec/protocol.toml) `[sysex]`.

**Asymmetry worth knowing:** a client addresses the device as `product $00`
(or `$02`) with `device $7F`, but the Profiler's own messages on the network
stream carry `product $00, device $00`. Do not filter inbound messages on
`device == $7F`; you will drop everything.

Every byte between `F0` and `F7` is 7-bit — the high bit is never set inside a
SysEx body. That constraint is why values are split, and why extended addresses
need five bytes.

## Function codes

| Code | Name | Direction | Layout after `<instance>` |
|---|---|---|---|
| `$01` | Single Parameter Change | both | `<page> <num> <MSB> <LSB> [morphMSB morphLSB]` |
| `$02` | Multi Parameter Change | both | `<page> <num> (<MSB> <LSB>)×N` |
| `$03` | String Parameter | both | `<page> <num> <ascii…> [00]` |
| `$04` | BLOB | both | `<page> <num> <startMSB> <startLSB> <sizeMSB> <sizeLSB> <content…>` |
| `$06` | Extended Parameter Change | both | `<5-byte address> <5-byte value>` |
| `$07` | Extended String Parameter | both | `<5-byte address> <ascii…> [00]` |
| `$08` | Morphed Multi Parameter Change | both | like `$02`, then an equal-sized morph-B block |
| `$3C` | Rendered String reply | device → client | `<page> <num> <MSB> <LSB> <ascii…> [00]` |
| `$41` | Request Single Parameter | client → device | `<page> <num>` — reply `$01` |
| `$42` | Request Multi Parameters | client → device | `<page> <num>` — reply `$02` |
| `$43` | Request String Parameter | client → device | `<page> <num>` — reply `$03` |
| `$46` | Request Extended Parameter | client → device | `<5-byte address>` — reply `$06` (or `$01` if the address fits in 14 bits) |
| `$47` | Request Extended String | client → device | `<5-byte address>` — reply `$07` (or `$03` if the address fits in 14 bits) |
| `$7C` | Request Rendered String | client → device | `<page> <num> <MSB> <LSB>` — reply `$3C` |
| `$7E` | Beacon / sense | both | see [below](#beacon-and-sense) |

Requests are read-only and cheap. A request for a nonexistent or unreadable
parameter is **silently ignored** — there is no error reply, so a client must
apply its own timeout rather than waiting indefinitely. How `libkp` does that is
[the request lane](#requests-and-replies).

Constants: `[sysex.functions]` in [`../spec/protocol.toml`](../spec/protocol.toml);
the code → short-name table is in
[`../spec/parameters.toml`](../spec/parameters.toml).

## 14-bit values

A value is 0–16383 (`full_scale = 16383`) carried as two 7-bit bytes,
most-significant first:

```
MSB = (value >> 7) & 0x7F
LSB =  value       & 0x7F
value = (MSB << 7) | LSB
```

| Value | MSB | LSB | Meaning |
|---|---|---|---|
| 0 | `00` | `00` | minimum / off |
| 1 | `00` | `01` | on (for switches) |
| 130 | `01` | `02` | |
| 6925 | `36` | `0D` | ~42 % |
| 8192 | `40` | `00` | 50 % — also "in tune" for tuner deviance |
| 16383 | `7F` | `7F` | full scale |

Vectors: [`../spec/vectors/u14.json`](../spec/vectors/u14.json).

Two parameter shapes share that encoding:

- **Continuous** — gain, volumes, mix: the full 0–16383 range, fractional and
  smoothed by the device.
- **Switch / enum** — On/Off, effect Type, section enables: an integer taken
  from the LSB pair. Off is `00 00`, on is `00 01`; enums such as effect Type use
  the joined 14-bit value (Type 179 = `01 33`).

A few parameters carry a scaled quantity rather than a fraction. Tempo is the
notable one: page `$04`, number 0 holds **bpm × 64**, so 120 bpm is 7680.
Constant `tempo_bpm_scale = 64`.

## The address space

`(page, number)` is the NRPN controller address: `page` is CC 99 (address MSB),
`number` is CC 98 (address LSB); the value pair is CC 6 / CC 38. Pages group the
device:

| Page | Section |
|---|---|
| `$00` | String tags (via `$03`/`$43`); also a few numeric parameters via `$01`/`$41` |
| `$04` | Rig Settings |
| `$05` | Fixed FX |
| `$09` | Input Section |
| `$0A` | Amplifier |
| `$0B` | Amplifier EQ |
| `$0C` | Cabinet |
| `$32 $33 $34 $35` | Effect modules A B C D |
| `$38 $3A $3C $3D` | Effect modules X MOD DLY REV |
| `$76` | User Scales |
| `$7C` | Realtime / meters |
| `$7D` | Looper and per-module Freeze |
| `$7F` | System / Global |

All eight effect pages share one parameter-number map, so "module C mix" and
"module REV mix" are the same number on different pages. The full map, and the
typed registry built from it, are in
[Parameter registry](09-parameter-registry.md).

Page `$00` is dual-use: the same page number addresses ASCII string tags when
reached through `$03`/`$43` and numeric parameters when reached through
`$01`/`$41`. The function code disambiguates, so a decoder must key on it — for
example page 0 / number 1 is the **Rig Name** string, while page 0 / number
`$77` is the numeric **Morph Position**.

## The string tags

The page-0 string tags come in three blocks of the same shape — the rig's, the
amplifier's and the cabinet's — each starting with the name and continuing
through author, date and comment at the same offsets:

| Block | Numbers | Name | Then |
|---|---|---|---|
| Rig | `$01`–`$04` | `$01` | author `$02`, creation date `$03`, comment `$04` |
| Amplifier | `$10`–`$1A` | `$10` | author, date, comment, then manufacturer, model, channel, pickup, year |
| Cabinet | `$20`–`$2D` | `$20` | author, date, comment, then manufacturer, model, microphone, speaker |

The state tree stores the three names (`string_rig_name`, `string_amp_name`,
`string_cabinet_name` in `[well_known]`) and the rig's author, date and
comment; the rest are readable with `$43` and named by the registry.

**`$0A` is not the amp name.** The amplifier block starts at `$10`, not `$0A`:
established by observed experimentation, a `$43` request for page 0 / number
`$0A` draws **no reply at all**, while `$10` answers with the amp's name. An
earlier value of `$0A` survived for a long time precisely because a
fire-and-forget request cannot notice an answer that never comes; the
[request lane](#requests-and-replies), which times a request out, is what
caught it.

On the CBOR channel the same numbers are the same flat addresses — 1 for the
rig name, 16 for the amp name, 32 for the cabinet name — and land in the same
fields ([The CBOR channel](06-cbor-channel.md#addressing)).

## Worked example — request a rig name

The safest first message to any Profiler: a `$43` Request String Parameter for
page 0, number 1. It changes nothing.

```
request                                    from ../spec/vectors/nrpn.json
f0 00 20 33 00 7f 43 00 00 01 f7
│  └──┬───┘ │  │  │  │  │  └── number 1 = Rig Name
│     │     │  │  │  │  └───── page 0 = string tags
│     │     │  │  │  └──────── instance 00
│     │     │  │  └─────────── function $43 = request string
│     │     │  └────────────── device $7F = omni
│     │     └───────────────── product $00 = Profiler
│     └──────────────────────── manufacturer 00 20 33
└────────────────────────────── F0
```

The device replies with `$03` carrying the ASCII:

```
reply                                      from ../spec/vectors/state.json
f0 00 20 33 00 00 03 00 00 01 41 43 33 30 f7
                  │        │  └────┬────┘
                  │        │      "AC30"
                  │        └── page 0, number 1
                  └─────────── function $03 = string parameter
```

The text runs from after `<number>` to the `F7`. A trailing `00` terminator is
conventional and is present in most replies; decoders must accept the string
with or without it, and must strip it when present.

The equivalent for a Player-class product, requesting page `$0A` number 0
(Amp Model):

```
f0 00 20 33 02 7f 43 00 0a 00 f7
```

## Worked example — turn an effect on

A `$01` Single Parameter Change writing 1 to page `$3D` (module REV), number 3
(On/Off):

```
f0 00 20 33 00 7f 01 00 3d 03 00 01 f7
                  │     │  │  └──┬─┘
                  │     │  │    value 1 = on   (00 00 = off)
                  │     │  └───── number 3 = On/Off
                  │     └──────── page $3D = module REV
                  └────────────── function $01 = single parameter change
```

Framed for the wire, this becomes
`14f000201433007f1401003d1403000115f70000` — see
[MIDI3 framing](04-midi3-framing.md).

The device applies that write, but — established by observed experimentation —
it does **not** echo it back on a plain streaming session. A client whose state
store must reflect the applied value follows the write with a `$41` single
parameter request for the same address; the reply is an ordinary `$01` from
`product 00, device 00` and flows through normal ingest:

```
f0 00 20 33 00 00 01 00 3d 03 00 01 f7
```

In `libkp` that is `set_effect_enabled("REV", true)` followed by
`request_param(0x3D, 3)`, which returns the value the reply carries — the
device answers within tens of milliseconds — and folds it into the snapshot on
the way. Order the write before its read-back; the writer sends them in the
order they were queued.

`$01` may carry an **optional trailing 14-bit pair**, the morph "B value" for
the same address. A parser must therefore accept 2 *or* 4 value bytes and use
the first pair as the current value.

Other `$01` examples from the vectors:

| Bytes | Meaning |
|---|---|
| `f0002033007f01000a044000f7` | set Gain (page `$0A`/4) to 8192 |
| `f00020330000010004003c00f7` | tempo (page `$04`/0) = 7680 = 120 bpm |
| `f00020330000010004014628f7` | rig volume (page `$04`/1) = 9000 |
| `f0002033000001000a04360df7` | gain = 6925 |
| `f0002033000001003d000133f7` | module REV Type = 179 = "Easy Reverb" |

## Worked example — a multi-parameter block

`$02` applies N values to **consecutive numbers** starting at `<page>/<number>`,
up to 128 values in one message. It is both a device push and the reply to `$42`.

```
f0 00 20 33 00 00 02 00 7c 4e 00 64 01 48 02 2c 3e 40 5d 60 27 08
46 28 1f 20 00 00 2e 70 00 00 f7
                  │     │  │  └──────────── 11 value pairs
                  │     │  └── number $4E = 78
                  │     └───── page $7C = realtime / meters
                  └─────────── function $02
```

Unrolled, the eleven pairs address numbers 78 through 88:

| Number | Bytes | Value |
|---|---|---|
| 78 | `00 64` | 100 |
| 79 | `01 48` | 200 |
| 80 | `02 2c` | 300 |
| 81 | `3e 40` | 8000 |
| 82 | `5d 60` | 12000 |
| 83 | `27 08` | 5000 |
| 84 | `46 28` | 9000 |
| 85 | `1f 20` | 4000 |
| 86 | `00 00` | 0 |
| 87 | `2e 70` | 6000 |
| 88 | `00 00` | 0 |

This particular block is the realtime status push — see
[Realtime status & meters](07-realtime-status.md).

Two parsing rules:

- Values are consumed in pairs from `<number>` upward. A **trailing odd byte is
  ignored**: `number = 0, values = 01 02 07` yields exactly one pair, `(0, 130)`.
- `$42` must address the **first** controller number of a unit; addressed
  elsewhere it is ignored. Requesting page `$34`, number 0 —
  `f0002033027f42003400f7` — returns all of module C's parameters at once, which
  is far cheaper than a `$41` per number.

`$08` Morphed Multi Parameter Change has the same shape as `$02` followed by a
second, equally sized block holding the morph-B value for each address.

## Extended addresses

`$06` and `$07` replace the `<page> <number>` pair with a **5-byte address**:
big-endian, 7 bits per byte, giving a 35-bit field that comfortably holds a
32-bit address. `$06` encodes its value the same way.

```
address = (b0<<28) | (b1<<21) | (b2<<14) | (b3<<7) | b4
```

| Bytes | Address |
|---|---|
| `00 00 00 00 01` | 1 |
| `00 00 00 0a 00` | 1280 |
| `12 1a 15 4f 09` | 4886718345 |

The extended space is the *same* space, expressed as a single integer:

```
address = page * 128 + number
page    = address / 128
number  = address % 128
```

Addresses below 16384 therefore have an ordinary `(page, number)` form; larger
ones do not, and exist only in extended messages.

An extended string, decoded:

```
f0 00 20 33 02 00 07 00 00 00 00 00 01 41 43 33 30 00 f7
                  │     └──────┬─────┘ └────┬───┘ │
                  │        address = 1     "AC30" terminator
                  └─ function $07
```

Address 1 is page 0 / number 1 — the Rig Name again, this time via the extended
form. Rig-load dumps mix both encodings freely, so a decoder that handles only
`$03` will silently miss metadata.

## Requests and replies

Every request is a question with an answer at the same address, and the reply
carries no reference to the request that provoked it:

| Request | Reply | In `libkp` |
|---|---|---|
| `$41` page/number | `$01` at the same page/number | `request_param(page, number)` → the 14-bit value |
| `$43` page/number | `$03` at the same page/number | `request_string(page, number)` → the text |
| `$46` 5-byte address | `$06` at the same address (or `$01` if it fits 14 bits) | `request_ext_param(address)` → the value |
| `$47` 5-byte address | `$07` at the same address (or `$03`) | `request_ext_string(address)` → the text |
| `$7C` page/number/value | `$3C` with the same page, number and value | `request_render(page, number, value)` → the display text |

The model's **request lane** rides on that. A `request_*` call queues the
request, registers what it is waiting for — the flat address, or page, number
and value for a rendered string — and resolves with the value that next lands
there. An unsolicited push at the same address is indistinguishable from the
reply and resolves it too; it is equally current, and it lands in the tree the
same way. `refresh()` is the same lane run over every row the routing table
marks `request = true` — 46 requests: the string tags, each effect slot's type
and state, the bank preview, the position, and the tracked numerics — and is
the connect-time sync.

The timing is the device's, established by observed experimentation against a
Profiler Player (firmware 14.2.1): eight `$41` in flight all answered within
46 ms, fifteen `$47` within 39 ms, three `$43` within 27 ms, and the whole
46-request burst within 50 ms. So the lane keeps at most **16** requests on the
wire (`max_in_flight_requests`) and queues the rest, and a request unanswered
for **300 ms** (`request_timeout_ms`) is not coming: it is dropped, raised as
`RequestTimedOut`, returned as `Timeout`, and **never retried** — the device
ignores an address it cannot answer rather than saying so, so asking again asks
the same silence. One address is refused before it is sent: the morph position
answers nothing on the stream, and the routing table marks it the control
link's, so asking for it returns `Unreadable` at once.

## Rendered strings

`$7C` asks the device to render a parameter's value the way its own display
would, and the reply arrives as `$3C` with the same header plus the text. It is
expensive on the device — use it for display, not for polling.

```
request   f0 00 20 33 02 7f 7c 00 3c 35 40 00 f7
                            │     │  │  └─┬─┘
                            │     │  │  value 8192
                            │     │  └── number 53 (Ducking)
                            │     └───── page $3C = module DLY
                            └─────────── function $7C

reply     f0 00 20 33 02 7f 3c 00 3c 35 40 00 3c 30 2e 30 3e 00 f7
                                              └───────┬──────┘ │
                                                    "<0.0>"   terminator
```

## Beacon and sense

`$7E` is a bidirectional keep-alive and push subscription. The client asks the
device to stream a named parameter set and to report status; the device answers
with periodic **sense** messages.

```
F0 00 20 33 <product> 7F 7E 00 40 <param_set> <flags> <lease/2> F7
                               │       │         │        └── lease seconds ÷ 2
                               │       │         └─────────── flag bits
                               │       └───────────────────── parameter set
                               └───────────────────────────── subcommand $40
```

| Flag | Bit | Value |
|---|---|---|
| init | 0 | `0x01` |
| sysex | 1 | `0x02` |
| echo | 2 | `0x04` |
| nofe | 3 | `0x08` |
| noctr | 4 | `0x10` |
| tunemode | 5 | `0x20` |

Vectors from [`../spec/vectors/nrpn.json`](../spec/vectors/nrpn.json):

```
init + tuner mode, 30 s lease, set $02, Player product
f0 00 20 33 02 7f 7e 00 40 02 23 0f f7
                           │  │  └── 0x0F = 15 → 30 s
                           │  └───── 0x23 = init | sysex | tunemode
                           └──────── parameter set $02

no init, no tuner, 20 s lease, Profiler product
f0 00 20 33 00 7f 7e 00 40 02 02 0a f7
```

Semantics:

- **Parameter set `$02`** is the useful one: the device pushes effect Type and
  State for modules A, B, C, D, X and MOD, the rig name, and tuner
  mode/note/deviance. DLY and REV are *not* in set `$02` — request them
  explicitly.
- The device answers with **sense** messages roughly every 500 ms: function
  `$7E`, instance `$00`, page `$7F`. No sense for 1.5 s means the link is down.
- Re-send the beacon at **half the lease** to renew it. Init beacons are retried
  every 5 s until sensing starts.
- The realtime status block and the `$01` pushes for device-side changes arrive
  **without** any beacon, so a monitoring client can skip it entirely. It is needed
  only for the set-`$02` subscription and the sense heartbeat.

Constants: `[beacon]` in [`../spec/protocol.toml`](../spec/protocol.toml).

## Program Change feedback does not exist here

Program Change, Note On and Note Off are inert in the device's network MIDI
encoder — no Program Change is emitted when the rig changes, and nothing but
SysEx ever comes back on the stream. To display the current rig, request its
name (`$43`, page 0, number 1) after a switch rather than waiting for feedback.

Where the device *is* it says a different way: as `$06` Extended Parameters at
the two position addresses. See [the position report](#the-position-report).

## The position report

The device's current bank and rig slot live at two extended addresses, both
0-based:

| address | meaning |
|---|---|
| **100701** (`0x1895D`) | current bank |
| **100702** (`0x1895E`) | current rig slot within the bank |

Together they are a flat rig index, `bank × 5 + slot`, which is the only address
that names a rig outside the current bank.

Read them with `$46` (`request_ext_param`, or both at once with
`refresh_position`) and the device answers with `$06` in about 20 ms:

```
-> f0 00 20 33 00 7f 46 00  00 00 06 12 5d  f7          request address 100701
<- f0 00 20 33 02 00 06 00  00 00 06 12 5d  00 00 00 00 01  f7   = 1
```

Better, the device **pushes** an unsolicited `$06` for whichever of the two
changed on every rig change — from the front panel as readily as from a
controller — so a client reads them once at connect and then just listens.
Neither address is pushed when its value does not move: stepping a bank at the
same slot pushes 100701 alone.

Both fields use the 5×7-bit extended encoding, so a `$06` value spans 35 bits
rather than the 14 a `$01` carries. Other extended addresses share the stream —
102405 is a free-running counter the device emits every second — so match on the
address rather than assuming any `$06` is a position.

Established by observed experimentation; the function code `$46` is not in the
Kemper MIDI Parameter Documentation.

## The morph

Morph lives on page `$00`, at two numbers:

| page / number | address | meaning |
|---|---|---|
| `$00` / `$50` | 80 | **Morph Button** — 1 on press, 0 on release |
| `$00` / `$77` | 119 | **Morph Position** — 0 = base … 16383 = fully morphed |

A press ramps the position across the full 14-bit range in about two seconds at
roughly 40 Hz, the direction alternating per press, starting on the press rather
than the release. A state dump taken while morphed reports the position, so it
is part of persistent state and not merely a live event.

**The position is CBOR-only, and push-only.** Neither a `$41` nor a `$46`
request draws any reply, and it never appears on the MIDI3 stream — unframing a
45-second capture across nine morph cycles yields no message carrying address
119 at all. The [CBOR channel](06-cbor-channel.md) is the only route: its state
dump carries the position, and its live pushes move it at about 40 Hz while a
morph ramps. That is why a `DeviceModel` opens the CBOR channel as its control
link beside the stream: the dump and the pushes fold into the same tree, and
`state.morph` is the readout. The routing table marks the address the control
link's alone, so `request_param(0, 0x77)` returns `Unreadable` without a byte
on the wire, and a model whose control link is off, unavailable or lost never
learns the position — `state.connection` reads `Degraded` to say so.

So a MIDI3 client learns *that* a morph happened, from the button at `$50`, and
hears what it did to the audio parameters — but never where the fader sits. The
bidirectional beacon does not change this.

**`$0B` is not the morph.** It is a real address that answers a request with a
constant 0 whether the rig is morphed or at base, and is never pushed. Because
nothing errors and the value simply never moves, a client keyed on it looks like
a rig that is never morphed. Implementations carry a regression test against it.

Established by observed experimentation, measured with the front-panel MORPH
button across nine press/release cycles with a CBOR and a MIDI3 session open
side by side.

## Sources

The SysEx envelope, function codes, message layouts, the 14-bit encoding, the
extended 5-byte address form, and the page/number map follow the
[Kemper MIDI Parameter Documentation](https://www.kemper-amps.com/downloads/5/User-Manuals).
The beacon/sense mechanism and parameter set `$02` are credited to
[PySwitch](https://github.com/Tunetown/PySwitch). See
[../CREDITS.md](../CREDITS.md).
