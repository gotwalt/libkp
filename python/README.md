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
    async with await DeviceModel.connect(reply.ip) as model:
        snapshots = model.subscribe()  # coalesced state snapshots
        state = await snapshots.get()
        print(state.rig.name, state.amp.name, state.status.loudness)

        await model.set_effect_enabled("REV", False)  # a tracked parameter
        await model.tap_tempo()  # a momentary action


asyncio.run(main())
```
## Layout

| Module | What it does |
|---|---|
| `libkp.protocol` | The TagStream wire encoding and the 34-byte discovery poll. |
| `libkp.discovery` | Async UDP broadcast discovery (`discover`, `find_first`). |
| `libkp.session` | TCP connect plus the line-based protocol-selection handshake. |
| `libkp.midi3` | The 4-byte stream framing (`Unframer`, `frame`). |
| `libkp.cbor` | The native CBOR channel: codec plus `fetch_state_snapshot`, the state-dump read of the current bank/rig. |
| `libkp.nrpn` | Kemper SysEx/NRPN builders and parsers, plus the beacon. |
| `libkp.control` | The 7-bit CC / PC / Bank Select vocabulary. |
| `libkp.params` | Offline `page/number → name` lookups. |
| `libkp.registry` | Typed descriptors and value formatting over those names. |
| `libkp.state` | The state tree and the pure `DeviceState.apply` decode routing. |
| `libkp.model` | `DeviceModel`, the async store over a live session. |
| `libkp.errors` | The exception family, all deriving from `LibKPError`. |
| `libkp._generated` | **Generated, data only** — constants and lookup tables. Do not edit. |

`_generated.py` is emitted from [`../spec`](../spec) by
[`../codegen/generate.py`](../codegen). Every constant and table in this package
comes from there; the protocol logic is hand-written and held to the shared
vectors.

## The device model is a store

`DeviceModel` classifies state into two lanes:

- **FAST** — the eleven-value realtime meter block, the beat pulse, and tuner
  deviance. Poll `model.status()` once per animation frame.
- **SLOW** — rig, amp, cabinet, effect slots, output volumes, tempo, morph,
  tuner note, connection. Each change queues one fresh `DeviceState` snapshot on
  every `model.subscribe()` queue, coalesced to at most one per ingested chunk.

`model.events()` gives the granular delta stream (every `DeviceEvent`, fast ones
included), and `model.add_event_listener(cb)` does the same through a callback.
Subscriber queues drop their oldest item rather than block the ingest task.

Commands split into two groups:

- **Parameters** (`set_gain`, `set_rig_volume`, `set_main_volume`,
  `set_monitor_volume`, `set_effect_enabled`, `set_effect_mix`, `set_tempo_bpm`,
  `set_param`) go out as 14-bit NRPN `$01` Single Parameter Changes. The device
  applies them silently and does not echo them, so follow a set with
  `request_param()` when `model.state()` should confirm the new value.
- **Actions** (`select_rig`, `rig_up`, `rig_down`, `bank`, `tap_tempo`,
  `tuner_mode`, `morph_button`, `freeze`, `rotary_fast`, `delay_infinity`,
  `effect_button`, the pedals, and `send_control`) are momentary 7-bit Control
  Changes and are not reflected in state.

`refresh_rig()` and `refresh_bank()` are neither: they only issue read-only value
*requests* — the rig strings and each effect slot's Type/On-Off, and the current
bank's five-slot rig/amp/cabinet name preview — and are what `connect()` runs as
the initial sync. The bank preview lands in `state.bank`; the master-volume knob
reads back through `state.output.master_volume`.

The device's **current bank and rig position** is not on the MIDI3 stream at all.
`await fetch_state_snapshot(ip)` reads it over the native CBOR channel (docs/06):
it opens its own short-lived session, triggers the device's state dump, and
returns the 0-based `current_bank` / `current_rig_slot`. Run it at boot *before*
`DeviceModel.connect` — the device dislikes concurrent connections — then feed
the result in with `model.set_current_position(bank, slot)`, which folds the
indices into `state.current_bank` / `state.current_rig_slot`.

## Decoding without a device

`DeviceState.apply` is pure — no sockets, no clock:

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
  and `state`, plus a guard that no vector file is left uncovered.
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
