# libkp (Python)

The Kemper Profiler network protocol in pure Python: LAN discovery, the TCP
session handshake, MIDI3 stream framing, the Kemper SysEx/NRPN message grammar,
the CC control vocabulary, and an observable async device model.

- **Python 3.11+**, `asyncio`.
- **Standard library only** at runtime — no third-party dependencies.
- Two example front-ends: a zero-dependency ANSI `meters`, and an optional
  **Textual** TUI `meters_tui` (installed via the `tui` extra).
- `pytest` for the test suite, including the shared cross-language conformance
  vectors and replay-capture fixtures in [`../spec`](../spec).

## Install

```sh
pip install -e '.[dev]'      # from python/
```

Or run straight from the source tree:

```sh
PYTHONPATH=src python -m libkp.examples.meters --help
```

## The live meters example

```sh
python -m libkp.examples.meters              # discover a device, then render
python -m libkp.examples.meters --ip 192.168.1.50 --all --width 48
libkp-meters --help                          # same thing, installed as a script
```

A full-screen ANSI view that updates straight off the stream:

```
KEMPER LIVE  192.168.1.50   frames 1284 (20.1/s)

  RIG  Crunchy Vox   ♪ 120 BPM
  by Someone
  AMP  Test Amp                CAB  Test Cab

  ● A   Green Scream    ○ B   Compressor    · C   —             · D   —
  · X   —               ● MOD Air Chorus    ● DLY Single Delay  ● REV Easy Reverb

  tuner A4            [·········◆··········]  ● in tune

  stack level         [████████·············]   9000  range     0-11020
  rig out level       [██████···············]   6224  range     0- 9310
  loudness            [██···················]    285  range     0-  980

  last param: Amplifier: Gain = 6925
  (play into the device — Ctrl-C to quit; --all shows every raw field)
```

Flags: `--ip` (optional; discovery runs when it is omitted), `--port`, `--all`
(show all eleven raw meter fields), `--width`, `--fps`, `--discover-timeout`.
Ctrl-C restores the cursor and exits.

## The Textual TUI example

`meters_tui` is a richer, widget-based terminal UI (rounded panels, colored
gauges, an effect-block grid, a live tuner strobe) built on
[Textual](https://textual.textualize.io/). It needs the optional `tui` extra
(Textual); the library itself stays dependency-free. Run it with `uv` (below),
or `pip install -e '.[tui]'` then `libkp-meters-tui`. Same core flags as the
ANSI example; press `q` to quit and `a` to toggle the raw fields.

## Running the examples with uv

[uv](https://docs.astral.sh/uv/) runs either example without a manual
virtualenv — it resolves the package (and any extras) on the fly. From `python/`:

```sh
# Zero-dependency ANSI meters
uv run libkp-meters --help
uv run libkp-meters --ip 10.0.0.1 --all

# Textual TUI — the `--extra tui` pulls in Textual just for this run
uv run --extra tui libkp-meters-tui
uv run --extra tui libkp-meters-tui --ip 10.0.0.1
```

The console scripts above come from `pyproject.toml`; the equivalent module form
works too:

```sh
uv run python -m libkp.examples.meters
uv run --extra tui python -m libkp.examples.meters_tui
```

Pin the interpreter (anything 3.11+) when you want a specific one, and
materialize the environment once for repeated runs or editor tooling:

```sh
uv run --python 3.14 --extra tui libkp-meters-tui   # choose the interpreter
uv sync --extra tui                                 # create .venv with Textual
uv run libkp-meters-tui                             #   then reuse it
```

Only the Textual example needs the `tui` extra; `uv run libkp-meters` needs
nothing beyond the standard library.

## Quick start

```python
import asyncio
from libkp import DeviceModel, find_first


async def main():
    reply = await find_first()  # UDP broadcast discovery
    # A bare connect is the whole session: the stream, its read-only sync
    # burst, and the control link that carries the morph position.
    async with await DeviceModel.connect(reply.ip) as model:
        snapshots = model.subscribe()  # coalesced state snapshots
        state = await snapshots.get()
        print(state.connection.value, state.rig.name, state.morph)

        # A tracked parameter, then the read-back that confirms it: the device
        # applies a write silently, and request_param returns what it now holds.
        await model.set_effect_enabled("REV", False)
        rev_on = await model.request_param(0x3D, 3)  # 0

        await model.tap_tempo()  # a momentary action
        model.step_rig(1)  # aim at the next rig; the Navigator loads it

        state = await snapshots.get()
        print(state.navigation.aim, state.aimed_rig_index)


asyncio.run(main())
```

Discovery needs UDP 5727 **exclusively** — the device replies only to that
port, and the kernel hands each reply to just one bound socket, so a second
listener steals replies rather than copying them. Acquiring it fails fast if
another program (Kemper's Rig Manager, typically) holds it. Hold a
`DiscoveryPort` across a session rather than re-acquiring per attempt; see
[Discovery](../docs/02-discovery.md#owning-the-port).

```python
from libkp import DiscoveryPort

with DiscoveryPort.acquire() as port:  # raises PortUnavailableError if taken
    replies = await port.poll()
```

## Layout

| Module | What it does |
|---|---|
| `libkp.protocol` | The TagStream wire encoding and the 34-byte discovery poll. |
| `libkp.discovery` | Async UDP broadcast discovery (`discover`, `find_first`). |
| `libkp.session` | TCP connect plus the line-based protocol-selection handshake. |
| `libkp.midi3` | The 4-byte stream framing (`Unframer`, `frame`). |
| `libkp.cbor` | The native CBOR channel: codec, the `ControlLink` the model runs beside its stream, and two tooling wrappers over it — `fetch_state_snapshot` (a one-shot read of the current bank/rig/morph) and `CborSession` (the raw value stream). |
| `libkp.nrpn` | Kemper SysEx/NRPN builders and parsers, plus the beacon. |
| `libkp.control` | The 7-bit CC / PC / Bank Select vocabulary. |
| `libkp.params` | Offline `page/number → name` lookups. |
| `libkp.registry` | Typed descriptors and value formatting over those names. |
| `libkp.state` | The state tree and the pure `DeviceState.apply_update` fold. |
| `libkp.model` | `DeviceModel`, the async store over the stream and the control link. |
| `libkp.errors` | The exception family, all deriving from `LibKPError`. |
| `libkp._generated` | **Generated, data only** — constants and lookup tables. Do not edit. |

`_generated.py` is emitted from [`../spec`](../spec) by
[`../codegen/generate.py`](../codegen). Every constant and table in this package
comes from there; the protocol logic is hand-written and held to the shared
vectors.

## The device model is a store

`DeviceModel` is the only object in libkp that holds a socket to the device.
It owns a MIDI3 **stream link** and, by default, a CBOR **control link**, and
both feed one fold that is the single writer of the state tree — so an app sees
one handle, one tree, one event stream, and never a channel name except in the
`ChannelChanged` event.

```python
from libkp import ConnectOptions, ControlPolicy, DeviceModel, SyncStrategy

model = await DeviceModel.connect(ip)  # both links, the request burst
model = await DeviceModel.connect(
    ip, options=ConnectOptions(control=ControlPolicy.OFF, sync=SyncStrategy.OFF)
)  # the stream alone, and ask for nothing
```

`ConnectOptions` carries the `port`, the `control` policy (`BEST_EFFORT` opens
the control link beside the stream and degrades the connection if it fails;
`REQUIRED` fails the connect instead; `OFF` never opens it), the `sync`
strategy (`STREAM_BURST` asks for every `request = true` row of the routing
table — 46 read-only requests, all answered in ~50 ms; `OFF` asks for nothing),
and a `ReconnectPolicy` (see below). A `port` passed positionally overrides
`options.port`.

`DeviceModel` classifies state into two lanes:

- **FAST** — the eleven-value realtime meter block, the beat pulse, and tuner
  deviance. Poll `model.status()` once per animation frame.
- **SLOW** — rig, amp, cabinet, effect slots, output volumes, tempo, morph,
  tuner note, position, connection and channels. Each change queues one fresh
  `DeviceState` snapshot on every `model.subscribe()` queue, coalesced to at
  most one per ingested chunk on either wire; joining broadcasts one fresh
  snapshot so a new subscriber starts from the current tree.

`model.events()` gives the granular delta stream (every `DeviceEvent`, fast ones
included), and `model.add_event_listener(cb)` does the same through a callback.
Subscriber queues drop their oldest item rather than block the ingest task.

Commands split into two groups:

- **Parameters** (`set_gain`, `set_rig_volume`, `set_main_volume`,
  `set_monitor_volume`, `set_effect_enabled`, `set_effect_mix`, `set_tempo_bpm`,
  `set_param`) go out as 14-bit NRPN `$01` Single Parameter Changes. The device
  applies them silently and does not echo them, so follow a set with
  `request_param()` when `model.state()` should confirm the new value.
- **Actions** (`bank`, `tap_tempo`, `tuner_mode`, `morph_button`, `freeze`,
  `rotary_fast`, `delay_infinity`, `effect_button`, the pedals, and
  `send_control`) are momentary 7-bit Control Changes and are not reflected in
  state. `bank(n)` is the bank *preselect* (CC47) alone: it loads nothing.
- **Navigation** (`navigate_to`, `step_rig`, `step_bank`, `select_slot`) is the
  one way to load a rig; see below.

### The Navigator

A rig load is the one command that can wedge the device: a second load
arriving while the first is still landing leaves it on a delayed fuse that
only a power cycle clears. So nothing in libkp sends a load directly.
`send_control` refuses `LoadSlot`, `Up`, `Down`, `ProgramChange` and
`BankSelect`, and `send_raw` refuses a Program Change or a Control Change on
one of `RIG_LOAD_CONTROLLERS` (CC48–54), both with
`RigLoadRequiresNavigatorError` before a byte is written. A client *aims*
instead, and returns at once:

```python
model.navigate_to(123)  # flat, 0-based: bank 25, slot 4
model.step_rig(+1)  # from state.aimed_rig_index, floored at 0
model.step_bank(forward=False)  # one bank down, same slot
model.select_slot(2)  # slot 1-5 of the aimed bank
```

`step_rig`, `step_bank` and `select_slot` do nothing while no position is
known (there is nothing to step from), and a step that lands where the aim
already is sends nothing.

The Navigator serialises the loads. The first aim goes out at once as the
documented pair — the absolute bank preselect (CC47) then the slot load
(CC50–54) that commits it — and is *in flight* for `RIG_LOAD_SETTLE_MS`
(500); every aim that arrives meanwhile only moves the target, and when the
settle elapses the final target is sent, once. A burst of taps therefore costs
two loads however long it is, and an index already on the wire is never sent
again while it stands. The device reports its position on both wires as it
lands, and a report that matches the aim retires it (`NavigationSettled`); a
report that does not is ignored, and an aim the device never confirms — one
past the last rig, where it stays put and says so — is dropped
`PENDING_WINDOW_MS` (1500) after its move settled (`NavigationDropped`, reason
`NavDrop.UNCONFIRMED`). The settle is never shortened by an early report: it
is the measured time the device needs. An aim while the stream is down is
dropped the same way rather than raising; a stream loss or `close()` clears
the aim silently.

`state.navigation` carries the `aim` and whether a load is `in_flight`, and
changing either is a slow change (a snapshot). `state.aimed_rig_index` is the
aim while there is one, else `current_rig_index` — what a rig browser should
highlight, and what `step_rig` steps from, so a run of taps counts from the
last tap.

The machine behind all of this is `libkp.nav.NavigatorState`, pure and
public: four fields (`aim`, `sent`, `in_flight`, `awaiting`) and four inputs
(`navigate`, `settle_elapsed`, `window_elapsed`, `position`), each returning
the `NavAction` list the model carries out (`Send`, `StartSettle`,
`StartWindow`, `Settled`, `Dropped`). `spec/vectors/navigation.json` pins it in
every language.

### Requests

`request_param`, `request_string`, `request_ext_param`, `request_ext_string` and
`request_render` are request/reply: each returns the value that answers it, and
the reply folds into the tree on its way. They go through a request lane that
keeps at most `MAX_IN_FLIGHT_REQUESTS` (16) on the wire — the rest queue, none
are dropped — and a request unanswered after `REQUEST_TIMEOUT_MS` (300) raises
`RequestTimeoutError`, raises a `RequestTimedOut` event, and is never retried:
the device ignores an address it cannot answer. The morph position is the one
address the stream cannot read, so asking for it raises
`RequestUnreadableError` without a byte on the wire — checked before the
stream's own state, since it is true of the address whether or not the stream
is up. `request_param` also raises it after the fact for a reply wider than
the 14 bits a `$01` carries: only a value from the other wire resolving the
same address could be, and it is not the stream's answer.

`refresh()` is the whole burst on demand; `refresh_rig()`, `refresh_bank()` and
`refresh_position()` are its subsets (the rig strings and each slot's
Type/On-Off; the bank's five-slot name preview; the current bank and rig slot).
All return once every reply has landed, or raise the first timeout after the
rest have.

### The two channels

The device's **current bank and rig position** lives at two extended
addresses; `refresh_position()` reads both, and the device pushes a `$06` for
whichever changes on every rig change — including changes made at the front
panel — so `state.current_bank` / `state.current_rig_slot` stay live on their
own. The **morph position** is CBOR-only and never appears on the MIDI3 stream,
so it comes from the control link: when the link opens it writes the one item
that asks for the state dump, folds the dump (the morph, the position and a
great deal else) into the tree, and then folds the live pushes that keep the
morph moving. A dump has two sections -- the system state, then the loaded rig
-- and each closes with a run at `DUMP_END_ADDRESS`, so it is recognised as
finished by the second such run (`DUMP_END_RUNS`), with `DUMP_SETTLE_MS` as
the fallback; `SyncCompleted` is raised for each channel when its sync is
done. The control link takes `PROTOCOL_CBOR_CONTROL` or nothing: a greeting
that does not offer it is a `ProtocolRejectedError` before any selection is
written, never a link on some other protocol.

`state.connection` summarises both: `CONNECTED` (the stream is up and the
control link is open, still on its way, or off by policy), `DEGRADED` (the
stream is up but the control link was asked for and is `UNAVAILABLE` or
`LOST` — the morph is stale or unknown, nothing else is affected),
`RECONNECTING` (with `state.reconnect_attempt`), or `DISCONNECTED`.
`state.channels.stream` / `.control` carry each socket's `ChannelState`, and
`ConnectionChanged` / `ChannelChanged` events report every transition
(`Connected` and `Disconnected` are still raised alongside).

A lost control link is never reopened on its own: `reopen_control()` does it on
request, refused with `ChannelTooSoonError` inside `CONTROL_REOPEN_MIN_GAP_MS`
of the last control open (a link already open or opening is left alone and the
call returns at once), and `ReconnectPolicy(control_reopen=seconds)` opts
into one attempt per gap while the stream is up. A lost **stream** closes both
links and reports `Disconnected` — unless `ReconnectPolicy(stream=Backoff(...))`
was given, in which case the model reports `RECONNECTING`, waits out the
backoff (`Backoff.default_stream()` is 4 s doubling to 30 s), and dials the
whole sequence again on the same handle: same receivers, same tree.

Every socket goes through `Session.connect`, whose per-peer ledger waits out
`CONNECTION_COOLDOWN` from the last open or close to that `(ip, port)` before
dialing, so neither the control link, a reconnect, `fetch_state_snapshot` nor
`CborSession.connect` can open a socket inside the cooldown of the last one —
the connection churn the device does not survive (docs/06, docs/11). The model
adds no sleeps of its own. Once dialed, `Session.handshake` waits up to
`HANDSHAKE_TIMEOUT` (`HANDSHAKE_TIMEOUT_MS`, 2000) for the first byte of the
greeting, and again for the first byte of the reply to the protocol selection,
before reading the rest of each with the short idle gap: a device that has
served a few sessions can take most of a second to greet, and a connect must
not fail on that. A device that never greets raises `TimeoutErrorLibKP` for the
`"greeting"` phase, reporting that full wait.

Both wires feed one fold. `DeviceState.apply` (a MIDI3 message) and
`DeviceState.apply_cbor` / `apply_cbor_text` (a CBOR numeric or string) are thin
decoders that build an `Update` — the wire it came from, whether it was pushed
live or is an item of the CBOR state dump, the flat address, and the decoded
value — and hand it to `DeviceState.apply_update`, which routes it by the table
generated from `spec/state.toml`. The table decides everything: which field an
address writes, how the value decodes and is range-checked, which wire may write
the row (the control channel's copies of the meter block, beat pulse and tuner
are dropped; the morph position is the control channel's), whether a repeated
value is a no-op, and whether the row is FAST (event only) or SLOW (event plus
snapshot). `begin_dump()` / `end_dump()` bracket a state dump so a value pushed
live on the stream while the dump streams is not overwritten by the dump's
stale copy of it.

`fetch_state_snapshot(ip)` (a one-shot read of the position and morph) and
`CborSession` (the raw `(address, value)` stream) remain as tooling; both are
the model's own `ControlLink` with a different sink. `model.apply_cbor()` is
deprecated — the model folds the channel itself.

## Decoding without a device

`DeviceState.apply` is pure — no sockets, no clock — and so are `apply_cbor`,
`apply_cbor_text` and the `apply_update` they feed:

```python
from libkp import Unframer
from libkp.state import DeviceState

state, unframer = DeviceState(), Unframer()
for message in unframer.push(raw_stream_bytes):
    outcome = state.apply(message)
    for event in outcome.events:
        ...
```

## Tests

```sh
python -m pytest              # from python/
```

The suite covers:

- **Conformance** (`tests/test_conformance.py`) — every file in
  `../spec/vectors`: `u14`, `discovery`, `midi3`, `nrpn`, `controls`, `params`,
  `state`, `cbor` and `navigation` (the Navigator's state machine), plus a
  guard that no vector file is left uncovered.
- **Replay captures** (`tests/test_captures.py`) — every fixture in
  `../spec/captures`: TagStream discovery replies, and whole MIDI3 streams
  checked for message count, pending bytes, exact messages, decoded status
  frames, the per-function histogram, and the resulting rig/amp/cab names.
- **Unit tests** for each module, and async tests that drive `Session` and
  `DeviceModel` against an in-process stand-in device (`tests/fake_device.py`).

## Provenance

Parameter maps, the SysEx/NRPN grammar, effect types, string tags, the CC map,
and the beacon follow the **Kemper MIDI Parameter Documentation** and
**PySwitch**. Discovery, the handshake, MIDI3 framing, session encapsulation,
and the realtime status field identities come from **observed experimentation**.
See [`../CREDITS.md`](../CREDITS.md).
