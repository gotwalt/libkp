# Channels and data paths

How the device's two data channels differ, what each one carries, how `libkp`
holds both behind one model, and why the model is shaped the way it is. The
reference pages ([04](04-midi3-framing.md), [05](05-sysex-nrpn.md),
[06](06-cbor-channel.md)) describe each channel on its own terms; this one
describes them **against each other**, and records the design that came out of
measuring them side by side.

Every figure below was established by observed experimentation against a
Profiler Player (firmware 14.2.1) with one MIDI3 session and one CBOR session
held open together, repeated over several dumps and rig loads.

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
it hides is real: an earlier rig-position bug came from believing the device
announced its position in channel-voice MIDI, when in fact it used a SysEx
function (`$06`) — both "on MIDI3", but nothing alike.

Both channels use the same handshake ([doc 03](03-handshake.md)) and the same
8-byte preamble; they differ only in the GUID selected. The device advertises
four GUIDs; `libkp` speaks two of them. `{2490272E-…}` ("request/response") and
`{77DB6B28-…}` ("reserved") are named in `spec/protocol.toml` and never opened.

**Both address the same space.** A CBOR address and a SysEx page/number are the
same number: `address = page * 128 + number`. Extended addresses (≥ 16384) have
no page/number form and appear in both channels — as `$06`/`$07` on MIDI3, and
as plain addresses on CBOR. One parameter universe, two encodings of it.

## What each channel actually carries

**Neither channel is a superset of the other.**

| | MIDI3 stream | CBOR channel |
|---|---|---|
| Meter block, 11 values (page `$7C`) | all eleven, ~20 Hz | **one** — the tuner strobe phase (15953, ~20 Hz). The other ten are never sent, not even at zero, so this is not change-gating |
| Beat pulse (15872) | yes, page `$7C` number 0 | yes — as a single item with a leading `-1` source flag |
| Tuner note / deviance | yes | no |
| Morph position (119) | **never** — no push, and no reply to `$41` or `$46` | yes: in the dump, and pushed ~40 Hz while a morph ramps |
| Morph button (`$00`/`$50`) | yes, momentary | no |
| Current bank / rig slot (100701/2) | yes, as `$06` pushes and `$46` replies | yes, in the dump and as pushes |
| Rig / amp / cabinet name strings | yes (`$03`, `$07`) | yes |
| Bank preview (page `$96`, 15 strings) | yes (`$07`) | yes, in the dump |
| Amplifier page (`$0A`) | yes | yes — 24 numbers in the dump |
| A 1 Hz counter (102405) | yes, as `$06` | yes |
| Whole-state read | no — assembled from the 46-request burst | yes: one non-mutating write returns the dump (below) |
| Device-supplied parameter names | no | yes |
| Program Change / Note On/Off inbound | inert — never emitted | n/a |

An earlier version of this page reported the amp page absent from the dump.
That was a bad observation from a single dump: measured again over three dumps,
`$0A` is present with 24 numbers, alongside `$00`, `$04`, `$05`, `$09`, `$0B`,
`$0C`, all eight effect pages, `$76`, `$7C`, `$7D`, `$7E`, `$7F` and 334
extended addresses. The dump is a complete sync, not a partial one.

Outbound, the asymmetry is larger still. Every **command** `libkp` sends goes
over MIDI3: SysEx writes (`$01`), SysEx requests (`$41`, `$43`, `$46`, `$47`,
`$7C`), the beacon (`$7E`), and channel-voice CC for the momentary controls and
the Navigator's rig loads. On CBOR, `libkp` writes exactly one thing: the
state-dump trigger at address 102528, which is non-mutating. There is no command
queue on the control link, so nothing in the library *can* write anything else
to it. The channel's wider command grammar — preset and library management,
backup, firmware transfer — remains uncharacterized.

### The state dump, measured

One `tag(1)([1, 102528, 1])` on the control link makes the device send its
whole parameter state: **202 items** — 95 consecutive runs (`[2, …]`), 95
strings (`[4, …]`), 8 singles (`[1, …]`) and 4 opaque blobs (`[5, addr,
bytes]`) — carrying about 1500 numeric values. The first item lands 30–90 ms
after the trigger and the last at ~340 ms; the longest gap inside a dump is
130 ms. The order is deterministic across dumps: the first item is the run at
102544, and the dump has **two sections** — the system section (identity, global
settings, the position) and then the rig section, which opens with the position
run again — each closed by a run based at **100800**. That run sits at item 32
(~80 ms) and at item 201 (~340 ms) of 202, so the model ends the dump phase when
it has folded the second one (`dump_end_address`, `dump_end_runs = 2`), and
falls back to `dump_settle_ms` (1 s) for a device that never sends it.

The dump has a side effect worth knowing: the trigger also makes the device
push a full rig dump — about a thousand `$01` messages plus the strings — on
the **MIDI3** session. It is not free for the device, which is one reason the
model's connect-time sync is the request burst and not the dump (below).

## The model that owns both

`DeviceModel` is the only object in `libkp` that holds a socket to the device.
It owns two links and shows an application neither of them:

```
                        DeviceModel
   ┌───────────────────────────────────────────────────────┐
   │  stream link (MIDI3, required)     control link (CBOR) │
   │   ingest ─┐  writer ◄─ command      ingest ─┐  the one  │
   │           │            queue                │  write:   │
   │           │            + request lane       │  the dump │
   │           │                                 │  trigger  │
   │           ▼                                 ▼           │
   │        ┌──────────── one funnel ──────────────┐         │
   │        │  DeviceState::apply_update, one lock, │         │
   │        │  one snapshot per chunk               │         │
   │        └───────────────────────────────────────┘         │
   │                 │                    │                   │
   │        subscribe() snapshots    events() deltas          │
   └───────────────────────────────────────────────────────┘
```

- The **stream link** is the MIDI3 session: one task reads the socket into the
  unframer and hands each read chunk to the funnel; one writer drains a bounded
  command queue to the wire. Every parameter write, request, control and rig
  load goes through that queue. The stream is required: `connect` fails
  without it, and losing it is losing the device.
- The **control link** is the CBOR channel: one task, one decoder, and exactly
  one write — the dump trigger, sent as the link opens. It is opened after the
  stream, by default in the background (`ControlPolicy::BestEffort`), and its
  dump and live pushes fold into the same tree. Failing to open it, or losing
  it, *degrades* the connection and nothing more.

What an app sees is one handle, one tree, one event stream:

| | |
|---|---|
| `state.connection` | `Disconnected`, `Reconnecting { attempt }`, `Connected`, or `Degraded` — the stream is up but a control link that was asked for is not. Only what the control link alone carries (the morph) goes stale there. |
| `state.channels` | each link as it really is: `Closed`, `Connecting`, `Open`, `Unavailable` (the open failed), `Lost` (it was open, then ended). |
| `ConnectionChanged`, `ChannelChanged` | every transition of either. `ChannelChanged` is the only place a channel is named. `Connected` / `Disconnected` are still raised at the two ends of a session. |
| `SyncCompleted { source }` | the stream's request burst finished (its last reply landed or timed out), or the control link's dump ended. |

Which wire a value came from is not part of the tree. The tree carries the
value; the routing table decided whether that wire was allowed to write it.

## The fold

Both links feed one funnel, `DeviceState::apply_update`, under one lock. A
chunk — one read of the stream, or the whole state dump — is folded in one call
and republishes the snapshot at most once. The decoders in front of it
(`apply` for a MIDI3 message, `apply_cbor` / `apply_cbor_text` for a CBOR item)
know nothing about which addresses the tree tracks; they produce an `Update`
(source wire, live or dump phase, flat address, decoded value) and hand it over.
What happens next is one spec-declared table, `spec/state.toml`, generated into
every language as `STATE_ROUTES` ([doc 09](09-parameter-registry.md#state-routing)),
and eight rules applied in a fixed order:

1. **Lookup.** The row for the address, if any. A `$02` block at the meter base
   is one unit (the status frame); any other block is folded element by element.
2. **No route.** A numeric on the stream at a page/number address is still
   reported as a fast `ParamChanged`; anything else untracked is silent — a
   control-channel value, a string, an extended address.
3. **Wire authority.** A `stream` row refuses the control channel: its copies
   of the strobe, the beat pulse and the momentaries are a different, unwanted
   feed, so CBOR 15953 never writes `status`. A `control` row (the morph
   position) accepts the stream, because if the value ever appeared there it
   would be real. Everything else is `both`, last writer wins — measured
   identical on both wires for every shared address (rig volume, amp on, gain,
   the output volumes), so no disagreement handling exists.
4. **Kind mismatch** — text at a numeric row, or the reverse — is untracked.
5. **Range / decode.** A `u14` value past 16383 or a `u16` past 65535 is
   dropped, not truncated; `u7` keeps the low seven bits; a sensitive address
   stores the redaction placeholder.
6. **Live beats dump.** Between the trigger and the dump's end, a live update
   from either wire marks its address; a dump item for a marked address is
   dropped, because the dump's copy predates the push. Outside a dump, a dump
   item folds like a live one.
7. **Dedupe.** A row with `dedupe` set is a no-op when it already holds the
   decoded value — no event, no snapshot. The momentaries and the meter frame
   never dedupe: every arrival is the information. This is what guarantees
   exactly one `CurrentPosition` event per position change, however many
   sources report it, which the Navigator depends on.
8. **Store and report.** Write the field, raise the row's event; a `fast` row is
   event only, a `slow` row also flags the snapshot.

Every rule is pinned by the `steps` cases of `spec/vectors/state.json`, which
drive the funnel with transport-tagged updates and assert the ordered events, so
the three implementations agree on the outcome of every case, not only the
final tree. The sanitized CBOR dump in `spec/captures/cbor-state-dump.json`
replays a real dump through the same path and asserts that the meter items land
nowhere.

## The request lane

Every `request_*` call goes out through the stream's request lane, which
registers what it is waiting for (the flat address, or page/number/value for a
rendered string) and resolves with the value that next lands there — a reply,
or an unsolicited push at the same address, which is equally current. The reply
folds into the tree on its way to the caller, so `state()` agrees with what was
returned.

The pacing is the device's, measured: eight `$41` in flight all answered within
46 ms, fifteen `$47` within 39 ms, three `$43` within 27 ms, and the whole
connect-time burst — **46 requests**, every `request = true` row of the routing
table — answered within 50 ms. So the lane keeps at most **16** requests on the
wire (`max_in_flight_requests`), queues the rest, and treats one unanswered for
**300 ms** (`request_timeout_ms`) as not coming: it is dropped, reported as
`RequestTimedOut`, returned as `Timeout`, and **never retried**. The device
ignores an address it cannot answer rather than saying so, so a retry would only
ask the same silence again.

One address is refused before it is sent. The morph position answers neither
`$41` nor `$46` — confirmed again in the same measurement — and the routing
table marks it the control link's alone, so `request_param(0, 0x77)` returns
`Unreadable` at once instead of waiting out a timeout.

The burst is the default sync (`SyncStrategy::StreamBurst`) because it is
cheaper for the device than the dump: 46 small replies inside 50 ms, against a
dump that also provokes a thousand-message rig replay on the stream. A
dump-based sync strategy was considered and dropped on that measurement; the
dump still folds, in full, whenever the control link opens.

## The Navigator

A rig load is the one command that can harm the device (below), so nothing in
`libkp` sends one directly. A caller *aims* at a flat rig index —
`navigate_to`, or the `step_rig` / `step_bank` / `select_slot` conveniences —
and the Navigator rations the sending:

- The first aim goes out at once as the documented pair, the bank preselect
  (CC47) then the slot load (CC50–54) that commits it, and is *in flight* for a
  fixed `rig_load_settle_ms` (**500 ms**). Every aim that arrives meanwhile
  only moves the target; when the settle elapses the final target is sent,
  once. A burst of taps therefore costs **two loads** however long it is, and
  two loads can never overlap.
- The settle is measured, not guessed. After a load the device reports its
  position on MIDI3 at 38–45 ms and on CBOR at 205–221 ms, then pushes the
  entire landed rig unsolicited on **both** wires — about 900 `ParamChanged`
  plus 38 strings on MIDI3, done by ~345 ms; 88 items on CBOR, done by
  ~390 ms. The pushes end at ~400 ms, so 500 ms is the edge, and the flight is
  never shortened by the early position report, since the rig's own pushes are
  still streaming when it lands.
- Because the device pushes the whole landed rig itself, there is **no
  read-back** after a load: an earlier design re-requested the rig strings and
  effect states after every move, and the measurement showed the device had
  already sent them.
- The aim lands in `state.navigation` at once, so a slot highlight answers every
  tap, and `aimed_rig_index()` — the aim while there is one, else the device's
  own position — is what the steppers step from. A position report that matches
  the aim, from either wire, retires it (`NavigationSettled`). An aim the device
  never confirms — one past the end of its rigs, where it stays put and says so
  — is dropped `pending_window_ms` (1.5 s) after its move settled
  (`NavigationDropped`). An index already on the wire is never sent again while
  it stands.
- Every other road to a load is closed: `send_control` refuses the load-slot,
  up and down controllers, Program Change and Bank Select; `send_raw` refuses
  any buffer carrying a Program Change status or a Control Change on
  `rig_load_controllers` (CC48–54). Both fail with `RigLoadRequiresNavigator`
  before a byte goes out. The bare preselect (`bank`) passes: it loads nothing.

The state machine is pure (`NavigatorState`) and pinned by
`spec/vectors/navigation.json` — the tap burst, the aim past the end, the early
confirmation that does not shorten the settle, the stale timer that does
nothing — so the three languages make exactly the same moves.

## The connection ledger

The device does not tolerate connection *churn*: a socket opened too soon after
another one opened or closed to the same device is not greeted, and enough of
them stop it accepting TCP until it is power-cycled. Rather than ask every
caller to space its own connections, `Session::connect` keeps a process-wide
ledger keyed by `(address, port)`: every open is stamped as it dials (a refused
dial counts — the device saw the SYN), every close and every drop is stamped,
and the next open to the same peer sleeps until `connection_cooldown_ms` (1 s)
has passed since the later of the two. Opens to different peers never wait on
each other, so a test against a fake on an ephemeral port pays nothing.

Everything passes it — the model's two links, a reconnect, `reopen_control`,
`StateSnapshot::fetch`, `CborSession` — so no code path in the library can open
a socket inside the cooldown of the last one, and the model adds no sleeps of
its own. The control link opens after the stream through the same ledger, which
is what spaces the two.

## Reconnect is a policy

By default the model does nothing on its own when a link goes away. A lost
stream closes both links and reports `Disconnected`, and every example exits on
that; a lost control link reports `Degraded` and is left there. Both are
choices the caller makes, because every socket to the device is a cost it pays:

- `ReconnectPolicy.stream = Some(Backoff)` redials the stream after a loss —
  4 s, doubling to 30 s (`reconnect_delay_ms`, `reconnect_max_delay_ms`) —
  running the whole connect sequence again on the same handle, same receivers,
  same tree, with the connection reading `Reconnecting { attempt }` between.
  That backoff is the spacing MetersApp ran on for a year without wedging a
  device; nothing faster has been tried. MetersApp opts in.
- `ReconnectPolicy.control_reopen = Some(interval)` reopens a failed or lost
  control link while the stream is up, at most once per interval and never
  closer than `control_reopen_min_gap_ms` (30 s) to the last attempt.
  `reopen_control()` does the same on request, refused with `TooSoon` inside
  the gap. Neither is on by default; the reopen cadence a device tolerates was
  deliberately not measured (below).

## Device hazards, and what makes each one structural

These are empirical, and every one of them was found the hard way. Each is now
paired with the mechanism that makes the safe behaviour a property of the
library rather than a discipline of its callers.

| Hazard | Mechanism |
|---|---|
| **Connection churn wedges the device.** Not concurrency — churn. It stops accepting TCP and does not recover without a power cycle. | The connection ledger inside `Session::connect`: every open to a peer waits out the cooldown from the last open or close, whoever is opening. |
| **Concurrency is fine.** Two read-only sessions coexist indefinitely, and the model holds two by design. | The model opens the control link after the stream, through the ledger, and never opens a third. The tooling sessions pass the same ledger. |
| **Overlapping rig loads wedge it, on a delayed fuse.** Two loads ~8 ms apart are answered normally; the device closes the session ~20 s later and refuses TCP until power-cycled. Nothing in the immediate response says harm was done. | The Navigator: one load in flight at a time, a fixed 500 ms settle, and every other route to a load (`send_control`, `send_raw`) refused before a byte goes out. |
| **A write is not echoed.** The device applies a `$01` without reporting it. | `request_param` returns the value that answers it and folds it into the tree; the device answers inside 50 ms. |
| **A request for an unreadable address is silently ignored.** No error reply. | The lane's 300 ms timeout, never retried; the morph refused as `Unreadable` without sending. |
| **The greeting can be slow.** A device that has served a few sessions has taken ~800 ms to send its first byte. | `handshake_timeout_ms` (2 s) bounds the first byte of the greeting and of the selection reply separately from the short read idle ([doc 03](03-handshake.md)). |
| **A dump's copy of a value can be stale by the time it lands.** | Rule 6 of the fold: a live update during the dump marks its address, and the dump's item for it is dropped. |

## What was measured, and what deliberately was not

Measured, and now built in: the dump's contents, timing, order and end marker;
the control channel's live feeds (strobe, beat pulse with its source flag, the
1 Hz counter); request latency under load, for every request type; the timing
of a rig load's position reports and rig pushes on both wires; that every
shared address carries the same value on both wires; and that the morph
position is unreadable over the stream.

Deliberately not measured, because each would have meant churning or wedging
the device on purpose:

- **The control reopen cadence** — how often the CBOR link may be reopened
  before the device objects. `control_reopen` therefore stays off by default and
  the floor stays at 30 s, MetersApp's historical spacing.
- **Open-to-open spacing** — whether two opens may be closer than a close and an
  open. The ledger keeps 1 s for both.
- **The fuse** — what a wedged device does with an already-open control link.
  The model closes the control link itself when the stream is lost, so it never
  finds out.

## What is deferred

- **The `{2490272E-…}` request/response and `{77DB6B28-…}` reserved GUIDs.**
  Neither is opened. The control link is deliberately a one-write channel; if
  the request/response GUID turns out to be the natural home for reads with a
  reply, it slots in as a third link behind the same funnel.
- **The CBOR command grammar** beyond the dump trigger. Nothing in the library
  drives it, and by construction nothing can.

## How earlier questions were resolved

An earlier version of this page ended with a list of open questions. Each has
an answer now.

1. *Should the model own both channels?* **Yes.** `DeviceModel` opens both,
   folds both, and reports each link's health in `state.channels` rather than
   hiding it. The real socket, the real failure mode and the real ordering
   constraint are all still visible — as `Degraded`, `ChannelChanged`, and the
   ledger — but no client reimplements the plumbing.
2. *Which channel is authoritative when both carry a value?* **The routing
   table's `wire` column**, per address: `stream` for the realtime page and the
   momentaries, `control` for the morph, `both` (last writer wins) elsewhere —
   and the measurement showed the two wires agree on every `both` address, so
   nothing further is needed. Live beats dump by rule 6.
3. *Is the CBOR dump a better sync than the request burst?* **No.** The burst is
   46 requests answered in 50 ms; the dump provokes a thousand-message rig
   replay on the stream. The burst stays the default; the dump folds too.
4. *`apply_cbor` models three addresses.* **Retired as a router.** It is a thin
   decoder in front of the one fold, and the table routes every address the tree
   tracks — the amp page, the bank preview, the morph, the position, the effect
   slots — whichever wire carried it.
5. *Reconnect policy is per-channel and inconsistent.* **It is a policy now**,
   `ReconnectPolicy`, off by default for both links, with the control link's
   reopen floored at 30 s. A transient control failure is reported as
   `Degraded` and `ChannelChanged`, not a log line.
6. *The other two GUIDs.* Deferred, above.
7. *Nothing tests the multi-channel path.* The `steps` cases in `state.json`
   drive both wires into one tree; `navigation.json` pins the Navigator; the
   `cbor_stream` capture replays a real dump; and each language drives its
   model against a fake device that serves both GUIDs, answers requests, serves
   the dump, and can hang up either link.

## How MetersApp uses the model

MetersApp (`swift/Sources/MetersApp`) is the client that drives everything, so it
is the worked example.

```
UDP 5727 ──── DiscoveryPort, acquired once and held for the process lifetime
                (the kernel gives a reply to exactly one bound socket, so this
                 must be exclusive; Rig Manager holding it looks like "no device")
    │
    ▼  host address
DeviceModel.connect(host:, options: ConnectOptions(reconnect: .init(stream: .defaultStream())))
    │
    ├── stream link      → meters, tuner, rig strings, effects, position, every command
    ├── control link     → the morph position, folded into the same tree
    ├── the sync burst   → the tree is whole within ~50 ms of the stream opening
    └── reconnect        → a dropped stream is dialled again by the library, 4 s → 30 s
```

The store forwards every navigation tap to the model's Navigator
(`navigateTo` / `stepRig` / `stepBank` / `selectSlot`) and highlights
`state.aimedRigIndex` — the aim until the device confirms it, then the device's
own position. Toggling an effect block is a `setEffectEnabled` followed by a
`requestParam` whose returned value is the state the card shows. The morph
readout is `state.morph`, shown as `—` while the connection is `degraded`. The
Reconnect button closes the model and connects afresh; the ledger spaces the new
dial from that close. Everything the app used to own — the retry loop, the
navigation serializer and its settle timers, the second CBOR session and the
cooldown sleeps around it — is the library's now.

## Where to look

| Question | File |
|---|---|
| Framing and the MIDI3 wrapper | [`04-midi3-framing.md`](04-midi3-framing.md) |
| SysEx function codes, the request lane, the position report, the morph | [`05-sysex-nrpn.md`](05-sysex-nrpn.md) |
| CBOR items, the state dump, the control link | [`06-cbor-channel.md`](06-cbor-channel.md) |
| CC vs NRPN, the Navigator, the model's surface | [`08-control-model.md`](08-control-model.md) |
| The routing table | [`09-parameter-registry.md`](09-parameter-registry.md#state-routing) |
| The two-channel client, worked | `swift/Sources/MetersApp/DeviceStore.swift` |
