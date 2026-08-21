"""A polished **Textual** live view of the Profiler's current patch and stream.

This is the rich, widget-based counterpart to the zero-dependency
:mod:`libkp.examples.meters` ANSI view. It connects (discovering the device if
no ``--ip`` is given), performs the read-only initial rig sync, then drives a
full-screen Textual UI straight off the async :class:`~libkp.model.DeviceModel`:

- the **slow lane** — rig name / author / tempo / morph, amp and cabinet, and
  the eight effect blocks with their on/off state and type — bound to
  :meth:`~libkp.model.DeviceModel.subscribe`, which emits a fresh snapshot only
  when snapshot-visible state actually changed; and
- the **fast lane** — the realtime status block (tuner strobe, level bars, beat
  pulse) — folded from :meth:`~libkp.model.DeviceModel.events` and redrawn each
  animation frame, with :meth:`~libkp.model.DeviceModel.status` polled as the
  authoritative latest meter frame.

Textual is an **optional, example-only** dependency: the library itself is pure
standard library, and this module keeps every ``textual``/``rich`` import inside
:func:`main` so importing or collecting the package never requires it. Install
it with ``pip install 'libkp[tui]'`` and run::

    python -m libkp.examples.meters_tui --ip 10.0.0.1
    python -m libkp.examples.meters_tui --all --fps 60

The reusable view logic — the wrap-aware strobe drift-rate verdict, the
peak-hold decay, the tempo-pulse handling and the field labels — is shared with
:mod:`libkp.examples.meters`. The realtime field identities come from observed
experimentation and are described by the shared spec. Press ``q`` / ``Ctrl-C``
to quit and ``a`` to toggle the full set of raw meter fields.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

from .. import _generated as gen
from ..discovery import find_first
from ..errors import LibKPError
from ..model import DeviceModel
from ..state import Connection, DeviceState
from .meters import (
    ALL_ROWS,
    BAR_ROWS,
    FPS,
    FULL_SCALE,
    IN_TUNE_RATE,
    PULSE_FLASH_SECS,
    MeterView,
    note_name,
)

# --- shared, textual-free view helpers ---------------------------------------

#: Accent color for the panel frames and titles (indigo-400).
ACCENT = "#818cf8"

#: Map a meter field id to its raw-block index (v0..v10).
_FIELD_INDEX = {ident: index for index, _n, ident, _name, _r in gen.METER_FIELDS}

#: The five drift verdicts, as ``key -> (label, textual color name/hex)``.
_VERDICTS = {
    "idle": ("idle", "grey50"),
    "unknown": ("…", "grey50"),
    "intune": ("● in tune", "green"),
    "sharp": ("← sharp", "red"),
    "flat": ("→ flat", "cyan"),
}


def _clock() -> float:
    return time.monotonic()


def verdict_key(view: MeterView) -> str:
    """The in-tune / sharp / flat / idle verdict from the strobe drift rate.

    Reuses the wrap-aware :meth:`MeterView.strobe_rate` and the same threshold
    the ANSI example applies, so both views agree on the reading.
    """
    if not view.status.strobe_active:
        return "idle"
    rate = view.strobe_rate()
    if rate is None:
        return "unknown"
    if abs(rate) < IN_TUNE_RATE:
        return "intune"
    return "sharp" if rate < 0.0 else "flat"


def level_color(fraction: float) -> str:
    """Green → yellow → red by level, matching the ANSI bars."""
    if fraction < 0.6:
        return "green"
    if fraction < 0.85:
        return "yellow"
    return "red"


def _short_label(label: str) -> str:
    """Trim a meter field id to a compact, lower-case label."""
    return label.replace("_", " ")


#: Width of the meters table's label column. Derived from the widest label the
#: table can ever show — the ``--all`` set carries a ``vN`` prefix, so a fixed
#: guess that fits the bar rows wraps the raw ones onto a second line.
LABEL_WIDTH = max(len(_short_label(label)) for label, _ident in ALL_ROWS)

#: Columns a meter row spends on everything except the bar itself: the label
#: column, the 6-column value, the 17-column ``range …`` readout, the bar's two
#: brackets, the three single-space gaps the grid puts between the four
#: columns, and the panel's border plus horizontal padding. One extra column is
#: left spare so a full-scale bar never reaches the right edge — Rich crops an
#: over-wide cell with an ellipsis rather than wrapping it.
METER_ROW_CHROME = LABEL_WIDTH + 6 + 17 + 2 + 3 + 4 + 1


# --- the run loop ------------------------------------------------------------


async def _resolve_ip(args: argparse.Namespace) -> str | None:
    """Return the target IP, discovering one if ``--ip`` was omitted."""
    if args.ip is not None:
        return args.ip
    print("No --ip given; discovering...", file=sys.stderr)
    reply = await find_first(args.discover_timeout)
    if reply is None:
        print("no device found; pass --ip <addr>", file=sys.stderr)
        return None
    print(f"Discovered {reply.name or 'a device'} at {reply.ip}", file=sys.stderr)
    return reply.ip


def _build_app_class():
    """Import Textual and Rich (lazily) and return the ``MetersApp`` class.

    Kept inside a function so the module imports with only the standard library
    available — the package and its test suite never need ``textual`` installed.
    """
    from rich.console import Group
    from rich.table import Table
    from rich.text import Text
    from textual.app import App, ComposeResult
    from textual.containers import Vertical
    from textual.widgets import Static

    def bar_text(fraction: float, peak_fraction: float, width: int) -> Text:
        """A colored level bar with a peak-hold marker, as a Rich ``Text``."""
        fraction = min(max(fraction, 0.0), 1.0)
        peak_fraction = min(max(peak_fraction, 0.0), 1.0)
        filled = round(fraction * width)
        peak_cell = min(round(peak_fraction * width), max(width - 1, 0))
        text = Text()
        text.append("[", style="grey37")
        for i in range(width):
            if i == peak_cell and peak_fraction > 0.01:
                text.append("|", style="bold white")
            elif i < filled:
                position = (i + 0.5) / width
                text.append("█", style=level_color(position))
            else:
                text.append("·", style="grey37")
        text.append("]", style="grey37")
        return text

    def on_dot(on: bool | None) -> tuple[str, str]:
        """On/off/unknown state as a colored dot glyph and style."""
        if on is True:
            return "●", "green"
        if on is False:
            return "○", "grey50"
        return "·", "grey37"

    class Panel(Static):
        """A bordered, titled panel that renders from a builder callback."""

        def __init__(self, title: str, builder, **kwargs) -> None:
            super().__init__("", **kwargs)
            self._builder = builder
            self.border_title = title

        def redraw(self, app: MetersApp) -> None:
            self.update(self._builder(app))

    class MetersApp(App):
        """The live Textual meters application."""

        CSS = """
        Screen {
            layout: vertical;
            background: $background;
        }
        Panel {
            border: round #818cf8;
            border-title-color: #818cf8;
            border-title-style: bold;
            padding: 0 1;
            width: 1fr;
        }
        #rig { height: 5; }
        #chain { height: 4; }
        #blocks { height: 4; }
        #tuner { height: 4; }
        #meters { height: auto; }
        #status { height: 1fr; min-height: 5; }
        """

        BINDINGS = [
            ("q", "quit", "Quit"),
            ("ctrl+c", "quit", "Quit"),
            ("a", "toggle_raw", "Raw fields"),
        ]

        def __init__(self, args: argparse.Namespace, ip: str | None) -> None:
            super().__init__()
            self._args = args
            self._ip = ip or "(discovering)"
            self.view = MeterView(ip=self._ip, width=48, show_all=args.all)
            self.view.snapshot = DeviceState()
            self.model: DeviceModel | None = None
            self._error: str | None = None

        # -- layout --------------------------------------------------------

        def compose(self) -> ComposeResult:
            with Vertical():
                yield Panel("RIG", self._rig, id="rig")
                yield Panel("SIGNAL CHAIN", self._chain, id="chain")
                yield Panel("EFFECT BLOCKS", self._blocks, id="blocks")
                yield Panel("TUNER", self._tuner, id="tuner")
                yield Panel("METERS", self._meters, id="meters")
                yield Panel("STATUS", self._footer, id="status")

        def on_mount(self) -> None:
            fps = min(max(self._args.fps, 5.0), 240.0)
            self.set_interval(1.0 / fps, self._tick)
            self.run_worker(self._connect(), exclusive=True, name="connect")

        async def on_unmount(self) -> None:
            await self._shutdown()

        # -- data plumbing -------------------------------------------------

        async def _connect(self) -> None:
            """Open the model and spawn the stream-draining workers."""
            if self._ip == "(discovering)":
                self._error = "no device address"
                return
            try:
                self.model = await DeviceModel.connect(self._ip, self._args.port)
            except (LibKPError, OSError) as exc:
                self._error = f"connect failed: {exc}"
                return
            self.view.snapshot = self.model.state()
            self.run_worker(self._drain_events(), name="events")
            self.run_worker(self._drain_snapshots(), name="snapshots")

        async def _drain_events(self) -> None:
            """Fold every granular event (incl. FAST Status frames) into the view."""
            assert self.model is not None
            queue = self.model.events()
            try:
                while True:
                    self.view.on_event(await queue.get())
            except asyncio.CancelledError:
                raise

        async def _drain_snapshots(self) -> None:
            """Track the newest coalesced slow-state snapshot."""
            assert self.model is not None
            queue = self.model.subscribe()
            try:
                while True:
                    self.view.snapshot = await queue.get()
            except asyncio.CancelledError:
                raise

        def _tick(self) -> None:
            """Per-frame render clock: decay peaks, poll the fast lane, redraw."""
            if self.model is not None:
                # The authoritative latest meter frame (the fast lane); the
                # events worker also folds these, so this only guards against a
                # lagged/dropped queue between redraws.
                self.view.status = self.model.status()
            self.view.decay()
            for panel in self.query(Panel):
                panel.redraw(self)
            if self.model is not None and not self.model.connected:
                self._error = "device closed the connection"

        async def _shutdown(self) -> None:
            if self.model is not None:
                await self.model.close()
                self.model = None

        # -- actions -------------------------------------------------------

        def action_toggle_raw(self) -> None:
            self.view.show_all = not self.view.show_all

        # -- panel builders (pure over view + snapshot) --------------------

        def _bar_width(self) -> int:
            return min(max(self.size.width - METER_ROW_CHROME, 12), 72)

        def _rig(self, app: MetersApp) -> Group:
            state: DeviceState = self.view.snapshot
            rig = state.rig
            name = Text(rig.name or "(no rig yet)", style="bold white")

            meta = Text()
            if rig.author:
                meta.append(f"by {rig.author}", style="grey50")
                meta.append("   ")
            if rig.tempo_bpm is not None:
                meta.append(f"{rig.tempo_bpm} BPM", style="yellow")
                meta.append("   ")
            if state.morph is not None:
                meta.append(f"morph {state.morph / FULL_SCALE * 100:.0f}%", style="magenta")

            connected = state.connection is Connection.CONNECTED
            conn = Text()
            if connected:
                conn.append("● ", style="green")
                conn.append("connected  ", style="grey50")
            else:
                conn.append("○ ", style="grey50")
                conn.append("disconnected  ", style="grey50")
            conn.append(f"{self._ip}:{self._args.port}   ", style="cyan")
            conn.append(
                f"{self.view.frames} frames ({self.view.message_rate():.0f}/s)",
                style="grey50",
            )
            return Group(name, meta, conn)

        def _chain(self, app: MetersApp) -> Group:
            state = self.view.snapshot
            amp, cab = state.amp, state.cabinet

            dot, style = on_dot(amp.on)
            amp_line = Text()
            amp_line.append(f"{dot} ", style=style)
            amp_line.append("AMP  ", style="bold")
            amp_line.append(amp.name or "—", style="white")
            if amp.gain is not None:
                amp_line.append(f"   gain {amp.gain / FULL_SCALE * 100:.0f}%", style="grey50")

            dot, style = on_dot(cab.on)
            cab_line = Text()
            cab_line.append(f"{dot} ", style=style)
            cab_line.append("CAB  ", style="bold")
            cab_line.append(cab.name or "—", style="white")
            return Group(amp_line, cab_line)

        def _blocks(self, app: MetersApp) -> Table:
            state = self.view.snapshot
            table = Table.grid(expand=True)
            for _ in range(4):
                table.add_column(ratio=1)
            cells = []
            for fx in state.effects:
                dot, style = on_dot(fx.on)
                empty = fx.kind is None or fx.is_empty
                if empty or fx.kind is None:
                    label = "—"
                elif fx.type_name:
                    label = fx.type_name
                else:
                    label = f"type {fx.kind}"
                name_style = "white" if (fx.on is True and not empty) else "grey50"
                cell = Text()
                cell.append(f"{dot} ", style=style)
                cell.append(f"{fx.slot:<3} ", style="bold")
                cell.append(label, style=name_style)
                cells.append(cell)
            table.add_row(*cells[0:4])
            table.add_row(*cells[4:8])
            return table

        def _tuner(self, app: MetersApp) -> Group:
            view = self.view
            key = verdict_key(view)
            label, color = _VERDICTS[key]
            active = view.status.strobe_active

            width = self._bar_width()
            position = int(view.status.strobe_phase / FULL_SCALE * max(width - 1, 1))
            strip = Text()
            strip.append("[", style="grey37")
            for i in range(width):
                if active and i == position:
                    strip.append("◆", style=color)
                else:
                    strip.append("·", style="grey37")
            strip.append("]", style="grey37")

            verdict = Text()
            verdict.append(
                label, style=f"bold {color}" if key in ("intune", "sharp", "flat") else color
            )
            if view.snapshot.tuner.note is not None:
                verdict.append("     ")
                verdict.append(note_name(view.snapshot.tuner.note), style="bold white")
            return Group(strip, verdict)

        def _meters(self, app: MetersApp) -> Table:
            view = self.view
            width = self._bar_width()
            rows = ALL_ROWS if view.show_all else BAR_ROWS
            table = Table.grid(padding=(0, 1))
            table.add_column(justify="left", style="grey50", width=LABEL_WIDTH)
            table.add_column()
            table.add_column(justify="right", width=6)
            table.add_column(justify="left", style="grey37")
            for label, ident in rows:
                index = _FIELD_INDEX[ident]
                value = view.status.raw[index]
                low, high = view.mins[index], view.maxs[index]
                if low > high:
                    low = high = 0
                bar = bar_text(value / FULL_SCALE, view.peaks[index] / FULL_SCALE, width)
                val = Text(f"{value:>5}", style="white")
                rng = Text(f"range {low:>5}-{high:<5}")
                table.add_row(_short_label(label), bar, val, rng)
            return table

        def _footer(self, app: MetersApp) -> Group:
            view = self.view
            pulse = view.last_pulse
            lit = pulse is not None and pulse[1] and (_clock() - pulse[0]) < PULSE_FLASH_SECS
            tempo = Text()
            tempo.append("tempo   ", style="grey50")
            if lit:
                tempo.append("♩ ●", style="bold yellow")
            elif pulse is not None:
                tempo.append("♩ ·", style="grey37")
            else:
                tempo.append("♩ (no pulse seen)", style="grey37")

            param = Text()
            param.append("param   ", style="grey50")
            param.append(view.last_param or "(none)", style="white")

            if self._error:
                help_line = Text(self._error, style="bold red")
            else:
                help_line = Text(
                    "q / Ctrl-C quit    a toggle raw fields    "
                    "play into the device to see the meters move",
                    style="grey50",
                )
            return Group(tempo, param, Text(""), help_line)

    return MetersApp


def build_parser() -> argparse.ArgumentParser:
    """The command-line interface (mirrors :mod:`libkp.examples.meters`)."""
    parser = argparse.ArgumentParser(
        prog="python -m libkp.examples.meters_tui",
        description=(
            "Polished Textual live view of a Kemper Profiler: rig, amp/cab, the "
            "eight effect blocks, the tuner strobe, and the realtime meters."
        ),
    )
    parser.add_argument("--ip", help="device IPv4 address; if omitted, discovery finds one")
    parser.add_argument(
        "--port",
        type=int,
        default=gen.PORT,
        help=f"device TCP port (default: {gen.PORT})",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="start with all 11 raw meter fields visible (toggle with 'a')",
    )
    parser.add_argument(
        "--fps",
        type=float,
        default=FPS,
        help=f"redraw rate, 5-240 (default: {FPS:g})",
    )
    parser.add_argument(
        "--discover-timeout",
        type=float,
        default=3.0,
        metavar="SECONDS",
        help="how long to listen for devices when --ip is omitted (default: 3)",
    )
    return parser


async def _amain(args: argparse.Namespace) -> int:
    ip = await _resolve_ip(args)
    if ip is None and args.ip is None:
        return 1
    app_class = _build_app_class()
    app = app_class(args, ip)
    await app.run_async()
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point: parse arguments, resolve the device, run the Textual app.

    The ``textual``/``rich`` imports live inside :func:`_build_app_class`, so
    this module (and the test suite that collects it) never requires Textual to
    be installed; the app only demands it once :func:`main` actually runs.
    """
    args = build_parser().parse_args(argv)
    try:
        import textual  # noqa: F401
    except ModuleNotFoundError:
        print(
            "This example needs Textual. Install it with:\n"
            "    pip install 'libkp[tui]'\n"
            "(the libkp library itself has no third-party dependencies).",
            file=sys.stderr,
        )
        return 1
    try:
        return asyncio.run(_amain(args))
    except KeyboardInterrupt:
        return 130
    except LibKPError as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - manual entry point
    raise SystemExit(main())
