# Channels and data paths

How the device's two data channels differ, what each one carries, how `libkp`
uses them today, and what is still unresolved about that. Written as a working
brief: the reference pages ([04](04-midi3-framing.md), [05](05-sysex-nrpn.md),
[06](06-cbor-channel.md)) describe each channel on its own terms, and this one
describes them **against each other**.

## First: there are two channels, not three

"CBOR vs MIDI3 vs SysEx" is a natural way to say it and a misleading way to
think about it. SysEx is not a peer of the other two — it is the payload
language spoken *inside* MIDI3 framing:

```
  TCP 5727, one socket per channel
      │
      ├── protocol {369F50E7-…}  "MIDI3 stream"
      │     └── MIDI3 framing — 4-byte groups [tag][b0][b1][b2]   doc 04
      │           └── raw MIDI messages, which are either:
      │                 • Kemper SysEx  F0 00 20 33 … F7          doc 05  ← "NRPN dialect"
      │                 • channel-voice MIDI (CC, Program Change) doc 08
      │
      └── protocol {774CDB9E-…}  "CBOR control"
            └── RFC 8949 CBOR items, tag(1)([selector, address, …])  doc 06
```

So the two things that are genuinely alternatives are **the MIDI3 stream** and
**the CBOR channel**. Within MIDI3, SysEx and channel-voice are two message
families sharing one pipe. Getting this right matters because the failure mode
it hides is real: the rig-position bug fixed on this branch came from believing
the device announced its position in channel-voice MIDI, when in fact it used a
SysEx function (`$06`) — both "on MIDI3", but nothing alike.

Both channels use the same handshake ([doc 03](03-handshake.md)) and the same
8-byte preamble; they differ only in the GUID selected. The device advertises
four GUIDs; `libkp` speaks two of them. `{2490272E-…}` ("request/response") and
`{77DB6B28-…}` ("reserved") are named in `spec/protocol.toml` and never opened.

**Both address the same space.** A CBOR address and a SysEx page/number are the
same number: `address = page * 128 + number`. Extended addresses (≥ 16384) have
no page/number form and appear in both channels — as `$06`/`$07` on MIDI3, and
as plain addresses on CBOR. One parameter universe, two encodings of it.

## What each channel actually carries

Established by observed experimentation with both channels open across the same
gestures. **Neither channel is a superset of the other.**

| | MIDI3 stream | CBOR channel |
|---|---|---|
| Meter block, 11 values (page `$7C`) | all eleven, ~20 Hz | **one** — the tuner strobe phase (15953). The other ten are never sent, not even at zero, so this is not change-gating |
| Tuner note / deviance | yes | no |
| Morph position (119) | **never** — no push, and no reply to `$41` or `$46` | yes, pushed ~40 Hz while a morph ramps |
| Morph button (`$00`/`$50`) | yes, momentary | no |
| Current bank / rig slot (100701/2) | yes, as `$06` pushes | yes, in the dump and as pushes |
| Rig / amp / cabinet name strings | yes (`$03`, `$07`) | yes |
| Amplifier page (`$0A`) | yes | absent from the dump (see caveat below) |
| Whole-state read at session open | no — must be assembled from ~40 requests | yes, one non-mutating write returns ~1174 addresses |
| Device-supplied parameter names | no | yes |
| Program Change / Note On/Off inbound | inert — never emitted | n/a |

Outbound, the asymmetry is larger still. Every **command** `libkp` sends goes
over MIDI3: SysEx writes (`$01`, `$03`), SysEx requests (`$41`, `$42`, `$43`,
`$46`, `$47`, `$7C`), the beacon (`$7E`), and channel-voice CC for the momentary
controls (rig select, bank preselect, tap tempo, morph pedal, effect buttons).
On CBOR, `libkp` writes exactly one thing: the state-dump trigger at address
102528, which is non-mutating. The channel's wider command grammar — preset and
library management, backup, firmware transfer — is uncharacterized, and no code
path drives it.

**Caveat on the amp page.** The `$0A` absence is a single observation from one
dump on one device (a Profiler Player, product `$02`) on one firmware, while
effect slots, cabinet, rig settings, amp EQ (`$0B`), looper and system pages
were all present. It is odd enough to re-measure before anything depends on it.

## How `libkp` exposes the channels today

| Concern | Rust | Python | Swift |
|---|---|---|---|
| Handshake + preamble, either channel | `session::Session` | `libkp.session.Session` | `Session` |
| MIDI3 session, owns the state tree | `model::DeviceModel` | `libkp.model.DeviceModel` | `DeviceModel` |
| CBOR one-shot state dump | `cbor::StateSnapshot::fetch` | `cbor.fetch_state_snapshot` | `StateSnapshot.fetch` |
| CBOR live session | `cbor::CborSession` | `cbor.CborSession` | `CborSession` |
| Fold a MIDI3 message into state | `DeviceState::apply` | `DeviceState.apply` | `DeviceState.apply` |
| Fold a CBOR value into state | `DeviceState::apply_cbor` | `DeviceState.apply_cbor` | `DeviceState.applyCbor` |
| Fold CBOR into a live model | `DeviceModel::apply_cbor` | `DeviceModel.apply_cbor` | `DeviceModel.applyCbor` |
| Discovery (UDP 5727) | `discovery` | `libkp.discovery` | `Discovery` |

The shape is: **`DeviceModel` owns a `DeviceState` and one MIDI3 socket.
`CborSession` owns a second socket and owns nothing else.** A client that wants
both holds each separately and pipes the CBOR side into the model. The two
`apply` entry points converge on the same fields and raise the same events, so
downstream consumers see one tree regardless of which wire a value arrived on.

## How MetersApp uses them today

MetersApp (`swift/Sources/MetersApp`) is the only client that drives everything,
so it is the worked example — and the place any redesign will be felt first.

```
UDP 5727 ──── DiscoveryPort, acquired once and held for the process lifetime
                (the kernel gives a reply to exactly one bound socket, so this
                 must be exclusive; Rig Manager holding it looks like "no device")
    │
    ▼  host address
TCP #1 ─────── DeviceModel.connect          → meters, tuner, rig strings, effects,
                 + refreshRig / refreshBank      position ($06), and every command
                 + refreshPosition ($46 ×2)
    │
    │  … wait Session.connectionCooldown …
    ▼
TCP #2 ─────── CborSession.connect          → morph position, and nothing else
                 → DeviceStore pipes each update into DeviceModel.applyCbor
```

Both sockets stay open for the life of the connection. The CBOR side is
**best-effort**: if it fails to open, the app logs it and runs on MIDI3 alone,
losing the morph readout and nothing else. It is deliberately not retried in a
loop, because reconnection churn is the thing most likely to kill the device
(below), and a missing morph is not worth risking the socket carrying the meters.

Before this branch the app also ran `StateSnapshot.fetch` *before* connecting, to
learn the rig position. That is gone: `$46` reads the position on the streaming
session in ~20 ms, which removed a fragile extra connection and about 1.3 s from
every connect. The one-shot fetch remains in the library for clients that want a
position before any session exists.

## Device hazards that constrain any design

These are empirical, and every one of them was found the hard way.

- **Connection churn kills the device.** Not concurrency — churn. It stops
  accepting TCP and does not recover without a power cycle. Space connections by
  `connection_cooldown_ms` (1 s) and never reopen in a tight loop.
- **Concurrency is fine.** Two read-only sessions coexist indefinitely; this was
  held for minutes with both channels streaming. Note that
  [doc 01](01-overview.md) still says "only one controller may hold a session at
  a time" — that claim predates the measurement and needs revisiting.
- **Overlapping rig loads kill it, on a delayed fuse.** Two rig loads issued
  ~8 ms apart are answered normally, and then the device closes the session about
  20 s later and refuses TCP until power-cycled. MetersApp serializes navigation
  behind a settle gate for exactly this reason (`DeviceStore.navigate`/`pump`).
  Nothing in the immediate response tells you harm was done.
- **A write is not echoed.** The device applies a `$01` write without reporting
  it, so a client that must confirm follows with a `$41` request.
- **A request for a nonexistent or unreadable address is silently ignored.** No
  error reply — apply your own timeout. Address 119 answers neither `$41` nor
  `$46`, which is exactly what "unreadable" looks like.

## What is unresolved

The questions a holistic pass should answer. Roughly in order of how much they
would change.

1. **Should the model own both channels?** Today `DeviceModel` owns MIDI3 and
   the state tree, `CborSession` owns a socket, and the *client* wires them
   together. That is honest layering, but it means every client reimplements the
   same plumbing, and MetersApp's `DeviceStore` is currently the only place that
   knows the two belong together. The alternative — a model that opens both and
   presents one stream of events — is friendlier but hides a real socket, a real
   failure mode, and a real ordering constraint.

2. **Which channel is authoritative when both carry a value?** Rig volume,
   effect state and the position indices appear on both. Today whichever arrives
   last wins, because both paths write the same field. That is fine while the
   two agree; nothing verifies that they do, and nothing decides what should
   happen if they diverge.

3. **The CBOR dump is a far better sync than the MIDI3 request burst.** Connect
   currently costs ~40 SysEx requests (`refreshRig` + `refreshBank` +
   `refreshPosition`) to assemble a partial picture, when one non-mutating CBOR
   write returns ~1174 addresses including the strings. Given that request floods
   are implicated in wedging the device, "dump once over CBOR, then keep MIDI3
   for the fast lane" may be both faster and safer. It would need the dump's
   addresses routed into the state tree, which today models only three of them.

4. **`apply_cbor` models three addresses out of ~1174.** The dump carries the
   whole parameter state; the state tree understands the morph and the two
   position indices and drops everything else on the floor. There is no shared
   routing table between the two `apply` paths — `apply` routes by page/number,
   `apply_cbor` routes by flat address, and the overlap between them is by hand.

5. **Reconnect policy is per-channel and inconsistent.** MIDI3 reconnects through
   the store's retry loop; CBOR is opened once and never retried. Deliberate, and
   defensible, but it means a transient CBOR failure silently costs the morph for
   the rest of the session with only a log line to say so.

6. **The other two protocol GUIDs are unexplored.** `{2490272E-…}` is named
   "request/response" and may be the natural home for the things that are awkward
   over MIDI3 — but nothing has opened it.

7. **Nothing tests the multi-channel path.** The conformance vectors cover the
   CBOR codec, the SysEx grammar and the state routing, all offline. There is no
   vector, and no fake device, that exercises two channels converging on one
   state tree — the exact place this branch introduced new behavior.

## Where to look

| Question | File |
|---|---|
| Framing and the MIDI3 wrapper | [`04-midi3-framing.md`](04-midi3-framing.md) |
| SysEx function codes, the position report, the morph | [`05-sysex-nrpn.md`](05-sysex-nrpn.md) |
| CBOR items, the state dump, the channel comparison | [`06-cbor-channel.md`](06-cbor-channel.md) |
| CC vs NRPN, and which is right for what | [`08-control-model.md`](08-control-model.md) |
| The two-channel client, worked | `swift/Sources/MetersApp/DeviceStore.swift` |
