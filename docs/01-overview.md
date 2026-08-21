# Overview

The Kemper Profiler speaks a layered network protocol: a UDP broadcast finds it,
a TCP handshake selects a wire dialect, that dialect frames MIDI, and the MIDI
carries a Kemper-specific SysEx/NRPN parameter language. Every layer is small;
the interest is in how they stack.

## One port

| | |
|---|---|
| Port | **5727** (`0x165F`) |
| UDP 5727 | broadcast discovery (`DSCV` poll → device reply) |
| TCP 5727 | the session (handshake, then the framed stream) |
| Connect timeout | 5 s |
| Socket read timeout | 15 s |

Discovery and the session share the same port number; the client broadcasts its
poll from UDP 5727 and reads replies there, then opens a TCP connection to the
discovered address on the same port. Only one controller may hold a session at a
time — a Profiler that is already connected refuses new sessions.

Source: [`../spec/protocol.toml`](../spec/protocol.toml), `[transport]`.

## The layers

```
  ┌───────────────────────────────────────────────────────────┐
  │  Application            DeviceModel / parameter registry   │  docs 08, 09
  ├───────────────────────────────────────────────────────────┤
  │  Message universe       Kemper SysEx  F0 00 20 33 … F7     │  docs 05, 07
  │                         + raw MIDI CC / Program Change     │
  ├───────────────────────────────────────────────────────────┤
  │  Framing                MIDI3  4-byte frames [tag][b0b1b2] │  doc 04
  ├───────────────────────────────────────────────────────────┤
  │  Session                8 zero bytes, then the stream      │  doc 03
  ├───────────────────────────────────────────────────────────┤
  │  Negotiation            protocol-GUID list → "+" / "-NO"   │  doc 03
  ├───────────────────────────────────────────────────────────┤
  │  Transport              TCP 5727                           │
  └───────────────────────────────────────────────────────────┘
              ▲
              │  address learned from
  ┌───────────────────────────────────────────────────────────┐
  │  Discovery              UDP 5727 broadcast, DSCV TagStream │  doc 02
  └───────────────────────────────────────────────────────────┘
```

## The connection, end to end

```
1.  UDP  → broadcast 34-byte "DSCV … POLL:)" every 500 ms
2.  UDP  ← DSCV TagStream reply: NAME, VSTR, MAC#, SER#, TYPE, …
3.  TCP  → connect to <device ip>:5727
4.  TCP  ← "{77DB6B28-…}\r\n{369F50E7-…}\r\n{2490272E-…}\r\n{774CDB9E-…}\r\n.\r\n"
5.  TCP  → "{369F50E7-750B-459A-BAEE-85ADD3F3798D}\r\n"
6.  TCP  ← "+{369F50E7-…}\r\n"
7.  TCP  → 00 00 00 00 00 00 00 00          (8-byte session preamble)
8.  TCP  ↔ MIDI3 frames in both directions, indefinitely
```

Step 8 is where everything else happens. The stream is **bidirectional**: the
device pushes the realtime status block at ~20 Hz without being asked, and
accepts requests, parameter writes, and Control Change messages on the same
socket, replying inline.

## The four protocol GUIDs

The device offers four dialects at step 4. `libkp` implements the first.

| GUID | Role | In `libkp` |
|---|---|---|
| `{369F50E7-750B-459A-BAEE-85ADD3F3798D}` | MIDI3 stream — meters, requests, replies, control | **implemented** |
| `{2490272E-CD92-4DBA-AE32-E8AF37ED3B0A}` | request/response channel; emits an 11-byte token on open | not implemented |
| `{774CDB9E-74ED-4740-AF09-AC96B3A69A11}` | native CBOR control channel | [experimental](06-cbor-channel.md), not implemented |
| `{77DB6B28-785E-4641-B840-42F0F06A11FC}` | reserved — offered, but rejects the handshake with `-NO` | n/a |

See [Handshake](03-handshake.md).

## The message universe

Once framing is stripped, the payload is ordinary MIDI. Two families matter.

**Kemper SysEx** — `F0 00 20 33 <product> <device> <function> <instance>
<page> <number> <values…> F7`. The `<function>` byte selects the message kind:

| Function | Name | Direction | Carries |
|---|---|---|---|
| `$01` | Single Parameter Change | both | one 14-bit value at (page, number) |
| `$02` | Multi Parameter Change | both | N values at consecutive numbers |
| `$03` | String Parameter | both | ASCII text at (page, number) |
| `$04` | BLOB | both | binary object |
| `$06` | Extended Parameter Change | both | 5-byte address + 5-byte value |
| `$07` | Extended String Parameter | both | 5-byte address + ASCII |
| `$08` | Morphed Multi Parameter Change | both | value block + morph-B block |
| `$3C` | Rendered String reply | device → client | display text for a value |
| `$41` | Request Single Parameter | client → device | reply is `$01` |
| `$42` | Request Multi Parameters | client → device | reply is `$02` |
| `$43` | Request String Parameter | client → device | reply is `$03` |
| `$47` | Request Extended String | client → device | reply is `$07` (or `$03`) |
| `$7C` | Request Rendered String | client → device | reply is `$3C` |
| `$7E` | Beacon / sense | both | keep-alive and push subscription |

**Channel-voice MIDI** — 7-bit Control Change (`$B0 | channel`) and Program
Change (`$C0 | channel`), the Profiler's performance-control surface: pedals,
tap tempo, tuner, effect buttons, rig navigation. Fire-and-forget; nothing is
read back. See [Control model](08-control-model.md).

Everything a controller does is one of those. Full grammar in
[SysEx / NRPN dialect](05-sysex-nrpn.md).

## The address space

A parameter is a **(page, number)** pair — the NRPN controller address, with
`page` as the MSB and `number` as the LSB — and a **14-bit value** (0–16383)
split into two 7-bit bytes. Pages group the device: `$04` rig settings, `$0A`
amplifier, `$32`–`$3D` the eight effect slots, `$7C` realtime/meters, `$7F`
system. The whole map lives in
[`../spec/parameters.toml`](../spec/parameters.toml) and is surfaced as a typed
registry — see [Parameter registry](09-parameter-registry.md).

## What arrives unasked

Even with no requests outstanding, an open MIDI3 stream delivers:

- the **realtime status block** at ~20 Hz — one `$02` message at page `$7C`,
  number `$4E`, eleven 14-bit values: the tuner strobe and the output meters
  ([Realtime status & meters](07-realtime-status.md));
- the **beat pulse** at page `$7C`, number 0, toggling 0/16383 with the tempo;
- a **`$01` echo of every parameter the device changes**, including changes made
  at the front panel — which is what lets a client keep a consistent state model;
- on a rig change, an **unprompted dump of the entire new rig**: name, author,
  comment, amp, cabinet, every effect-slot type and state, and the rig settings.

The `meters` example in each language (`rust/examples`,
`python/src/libkp/examples`, `swift/Sources/meters`) does exactly this and
nothing more: connect, subscribe, and render what arrives.

## How this documentation is held to the wire

Every constant quoted in these documents comes from
[`../spec/*.toml`](../spec), and the worked examples are taken from the
conformance vectors in [`../spec/vectors`](../spec/vectors), which all three
implementations execute. Beyond those synthetic vectors,
[`../spec/captures`](../spec/captures) holds sanitized recordings of real
protocol traffic — a discovery reply, live status and meter streams, and a
rig-load dump, gathered through observed experimentation — that each
implementation replays end to end to prove its decode path against genuine wire
data. See [Versioning & compatibility](10-versioning-and-compatibility.md).

## Where to go next

| If you want to… | Read |
|---|---|
| find a Profiler on the LAN | [Discovery](02-discovery.md) |
| open a session | [Handshake](03-handshake.md) |
| get bytes on and off the stream | [MIDI3 framing](04-midi3-framing.md) |
| read or write a parameter | [SysEx / NRPN dialect](05-sysex-nrpn.md) |
| draw meters or a tuner | [Realtime status & meters](07-realtime-status.md) |
| pick CC vs NRPN for a control | [Control model](08-control-model.md) |
| name an address | [Parameter registry](09-parameter-registry.md) |

## Sources

The transport envelope — ports, discovery, the handshake, MIDI3 framing, and the
session preamble — was characterized through observed experimentation. The
SysEx/NRPN grammar, function codes, and the CC map follow the
[Kemper MIDI Parameter Documentation](https://www.kemper-amps.com/downloads/5/User-Manuals),
cross-checked against [PySwitch](https://github.com/Tunetown/PySwitch). See
[../CREDITS.md](../CREDITS.md).
