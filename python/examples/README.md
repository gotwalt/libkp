# libkp Python examples

Runnable front-ends over the `libkp` library, kept beside it rather than inside
it (mirroring `../../rust/examples` and Swift's separate executable targets) so
neither ships in the installed package.

- **`meters.py`** — a zero-dependency, full-screen ANSI view of the current
  rig, amp/cab, the eight effect blocks, the tuner strobe, and the realtime
  level meters.
- **`meters_tui.py`** — the same view as a richer [Textual](https://textual.textualize.io/)
  TUI (rounded panels, colored gauges). Needs the optional `tui` extra.
- **`homeassistant/`** — a Home Assistant custom integration: the loaded rig,
  amp and cabinet as sensors, plus an `active` binary sensor that turns the
  ~20 Hz meter lane into two state writes per playing session. Its own uv
  project, with its own README, tests and bundler.

## Running them

From `python/`, with [uv](https://docs.astral.sh/uv/) (no manual virtualenv
needed — it resolves `libkp` and any extras on the fly):

```sh
uv run examples/meters.py --help
uv run examples/meters.py --ip 192.168.1.50 --all --width 48

uv run --extra tui examples/meters_tui.py --help
uv run --extra tui examples/meters_tui.py --ip 192.168.1.50
```

Or, after `pip install -e .` (add `'.[tui]'` for the Textual example):

```sh
python examples/meters.py --help
python examples/meters_tui.py --help
```

Both discover a device over UDP broadcast when `--ip` is omitted; quit
Kemper's Rig Manager first if it holds the discovery port. Ctrl-C quits either
one and restores the terminal.
