# Kemper Profiler — Home Assistant integration

A custom integration that puts a Kemper Profiler on the local network into
Home Assistant: what rig is loaded, and whether anyone is playing through it.

It is an **example** of using `libkp` in an application, and it is a real
integration: it holds one MIDI3 session for as long as Home Assistant runs,
takes everything it shows from what the device pushes unrequested, and never
polls the device or reconnects in a loop.

A Profiler is identified by the **serial number** it advertises, not by its
address. Every setup broadcasts once to ask where that serial is now, so a
device whose DHCP lease moves it to another address is followed automatically:
the entry, the device and all five entities stay exactly as they were, history
included. Discovery finding nothing — the port held by Rig Manager, a quiet
network — is not an error; the last known address is used as it stands.

```
Kemper Profiler
├─ sensor.<device>_rig            Rig name        "Crunchy Vox"
├─ sensor.<device>_amp            Amp name        "Vintage Twin"
├─ sensor.<device>_cabinet        Cabinet name    "2x12 Alnico"
├─ binary_sensor.<device>_active  Playing?        on / off
└─ sensor.<device>_last_activity  Last activity   timestamp
```

## The entities

| Entity | What it is |
|---|---|
| `sensor` Rig / Amp / Cabinet | The names the device pushes on a rig change. They follow the front panel, a MIDI controller, Rig Manager — anything that loads a rig. |
| `binary_sensor` Active | On while signal is passing through the rig. See below. |
| `sensor` Last activity | When signal was last heard. While *Active* is on it is when the current session began; when *Active* goes off it is the moment of the last note. |

Everything else the device says — the effect slots, the tempo, the volumes,
the tuner, the bank preview, both channels' states — is in the integration's
**diagnostics** download rather than in entities. Adding an entity for any of
it is one row in the table in `sensor.py`.

### Activity detection

The Profiler pushes a meter frame about twenty times a second. Writing an
entity per frame would put 72,000 states an hour into the recorder to say
"someone is playing", so the meter lane is read by one plain callback that
compares a single 14-bit integer per frame and writes Home Assistant state
only when the answer changes: **two state writes per playing session**,
however long the session runs.

The level it reads is the **rig output** meter — after the rig's own volume,
before the master/monitor/headphone volumes — so a rig turned down reads
quiet, but practising with the monitors off still reads as playing.

Two options (Settings → Devices & services → Kemper Profiler → Configure):

- **Quiet window** — how long the output must stay below the threshold before
  *Active* turns off. Default 5 minutes.
- **Level threshold** — how loud counts as playing, as a percentage of full
  scale. Default 2%.

Saving them retunes the running detector; it does **not** reconnect to the
device.

### Losing the connection

When the stream ends — the amp switched off, the network dropped — the
integration does not redial the address it was using, because that address is
the part that can change. It reloads the config entry instead, which starts
again at discovery: find the serial, follow it to wherever it is now, connect
once. A Profiler that is simply off fails that setup and Home Assistant retries
on its own widening schedule; a session that ends within a minute of opening
waits half a minute before reloading, so nothing can spin.

## Install

Build the bundle and copy it into your Home Assistant configuration
directory, next to `configuration.yaml`:

```sh
uv run python build.py                      # dist/custom_components/kemper + a zip
uv run python build.py --install ~/homeassistant
```

For a Home Assistant OS or supervised install, take
`dist/kemper-<version>.zip` and unpack it into the configuration directory
with the **Samba share**, **Terminal & SSH**, or **File editor** add-on — its
paths are already `custom_components/kemper/…`.

Then:

1. Restart Home Assistant.
2. **Settings → Devices & services → Add integration → "Kemper Profiler"**.
3. The flow broadcasts for Profilers on the LAN and lists what answers. If
   nothing answers — Rig Manager holds the discovery port exclusively, and so
   does a running `meters` example — choose *Enter a host manually* and give
   the Profiler's IP address.

The bundle vendors `libkp` itself, so the integration has no `pip`
requirements and works on an install with no internet access.

## Development

The integration is developed against the library beside it:
`custom_components/kemper/libkp` is a relative symlink to `python/src/libkp`,
and the integration imports it as `from .libkp import …`. The same code
therefore runs against the working tree here and against the vendored copy in
a bundle — `build.py` dereferences the symlink and copies the library in.

```sh
uv sync                     # a Python 3.14 environment with Home Assistant 2026.8
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
uv run python build.py
```

The tests are end-to-end over a real loopback socket: they drive libkp's own
`FakeDevice` (`python/tests/fake_device.py`), push the same bytes a Profiler
pushes, and assert on entity states. Nothing below the config entry is mocked
except the discovery broadcast.
