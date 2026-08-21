"""Live terminal view of the Profiler's current patch and realtime stream.

Connects (discovering the device if no ``--ip`` is given), performs the
read-only initial rig sync, then renders a full-screen ANSI view that updates
straight off the stream:

- the current rig name, author and tempo,
- the amp and cabinet names,
- the eight effect blocks with on/off and effect type,
- the tuner strobe with an in-tune / sharp / flat verdict,
- level bars for stack, rig output and loudness, with peak-hold,
- a tempo pulse indicator and the last parameter seen.

Run it with ``python -m libkp.examples.meters`` (Ctrl-C quits and restores the
terminal). The realtime field identities it renders come from observed
experimentation and are described by the shared spec.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys
import time
from collections import deque
from dataclasses import dataclass, field

from .. import _generated as gen
from .. import params
from ..discovery import find_first
from ..errors import LibKPError
from ..model import DeviceModel
from ..state import (
    BeatPulse,
    DeviceEvent,
    DeviceState,
    ParamChanged,
    RealtimeStatus,
    RenderedString,
    Status,
    TempoBpm,
)

# --- ANSI --------------------------------------------------------------------

CLEAR = "\x1b[2J"
HOME = "\x1b[H"
CLEAR_EOL = "\x1b[K"
CLEAR_EOS = "\x1b[J"
HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"
RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
RED = "\x1b[31m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
CYAN = "\x1b[36m"
WHITE = "\x1b[97m"
AMBER = "\x1b[93m"

# --- tuning knobs ------------------------------------------------------------

#: Render this many frames per second.
FPS = 25.0
#: Trailing window for the messages/second estimate, in seconds.
RATE_WINDOW = 2.0
#: Window of strobe-phase samples used for the drift-rate estimate, in seconds.
STROBE_WINDOW = 0.4
#: Below this absolute drift rate (counts/sec) the strobe reads as in tune.
IN_TUNE_RATE = 600.0
#: A full-scale peak-hold marker falls to zero in roughly this many seconds.
PEAK_FALL_SECS = 1.25
#: How long the tempo indicator stays lit after a pulse edge, in seconds.
PULSE_FLASH_SECS = 0.15
#: Label column width in the meter rows.
LABEL_WIDTH = 19

FULL_SCALE = float(gen.FULL_SCALE)
PHASE_SPAN = gen.FULL_SCALE + 1

#: The bar rows, as ``(label, meter field id)``.
BAR_ROWS = [
    (ident.replace("_", " "), ident)
    for _index, _number, ident, _name, render in gen.METER_FIELDS
    if render == "bar"
]

#: Every raw field in NRPN order, for ``--all``.
ALL_ROWS = [
    (f"v{index} {ident.replace('_', ' ')}", ident)
    for index, _number, ident, _name, _render in gen.METER_FIELDS
]

#: Note names for the tuner readout, indexed by MIDI note number mod 12.
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def _clock() -> float:
    """A monotonic wall clock for rates, decay and flashes."""
    return time.monotonic()


# --- view state --------------------------------------------------------------


@dataclass
class MeterView:
    """Everything the renderer needs that is not already in the state tree."""

    ip: str
    width: int
    show_all: bool

    peaks: list[float] = field(default_factory=lambda: [0.0] * gen.METER_COUNT)
    mins: list[int] = field(default_factory=lambda: [gen.FULL_SCALE] * gen.METER_COUNT)
    maxs: list[int] = field(default_factory=lambda: [0] * gen.METER_COUNT)
    frames: int = 0
    recent: deque = field(default_factory=deque)
    phase_history: deque = field(default_factory=deque)
    last_pulse: tuple[float, bool] | None = None
    last_param: str | None = None
    last_render: float = field(default_factory=_clock)
    status: RealtimeStatus = field(default_factory=RealtimeStatus)

    # -- ingest ------------------------------------------------------------

    def on_event(self, event: DeviceEvent) -> None:
        """Fold one granular device event into the view."""
        now = _clock()
        if isinstance(event, Status):
            self.status = event.status
            self.frames += 1
            self.recent.append(now)
            for i, value in enumerate(self.status.raw):
                self.peaks[i] = max(self.peaks[i], float(value))
                self.mins[i] = min(self.mins[i], value)
                self.maxs[i] = max(self.maxs[i], value)
            if self.status.strobe_active:
                self.phase_history.append((now, self.status.strobe_phase))
            else:
                self.phase_history.clear()
        elif isinstance(event, BeatPulse):
            self.last_pulse = (now, event.on)
        elif isinstance(event, ParamChanged):
            self.last_param = (
                f"{params.describe_numeric(event.page, event.number)} = {event.value}"
            )
        elif isinstance(event, TempoBpm):
            self.last_param = f"Rig Settings: Tempo bpm = {event.bpm}"
        elif isinstance(event, RenderedString):
            self.last_param = (
                f"{params.describe_numeric(event.page, event.number)} -> {event.text!r}"
            )

    def decay(self) -> None:
        """Let the peak-hold markers fall, never below the current value."""
        now = _clock()
        dt = min(now - self.last_render, 0.5)
        self.last_render = now
        drop = FULL_SCALE / PEAK_FALL_SECS * dt
        for i, value in enumerate(self.status.raw):
            self.peaks[i] = max(self.peaks[i] - drop, float(value))

    # -- derived readings ---------------------------------------------------

    def message_rate(self) -> float:
        """Status messages per second over the trailing window."""
        cutoff = _clock() - RATE_WINDOW
        while self.recent and self.recent[0] < cutoff:
            self.recent.popleft()
        return len(self.recent) / RATE_WINDOW

    def strobe_rate(self) -> float | None:
        """Wrap-aware strobe drift rate in counts/sec over the recent window.

        Positive = phase rising (flat direction), negative = falling (sharp).
        Returns ``None`` until enough samples span a usable interval.
        """
        cutoff = _clock() - STROBE_WINDOW
        while self.phase_history and self.phase_history[0][0] < cutoff:
            self.phase_history.popleft()
        if len(self.phase_history) < 3:
            return None
        total = 0.0
        for (_t0, a), (_t1, b) in zip(self.phase_history, list(self.phase_history)[1:]):
            # Shortest wrapped step from a to b on the 0..16383 circle.
            total += ((b - a + PHASE_SPAN // 2) % PHASE_SPAN) - PHASE_SPAN // 2
        dt = self.phase_history[-1][0] - self.phase_history[0][0]
        return total / dt if dt > 0.05 else None


# --- rendering ---------------------------------------------------------------


def bar(fraction: float, peak_fraction: float, width: int) -> str:
    """A colored bar with a peak-hold marker. Green → yellow → red by position."""
    fraction = min(max(fraction, 0.0), 1.0)
    filled = round(fraction * width)
    peak_cell = min(round(min(max(peak_fraction, 0.0), 1.0) * width), max(width - 1, 0))

    cells = [f"{DIM}[{RESET}"]
    for i in range(width):
        position = (i + 0.5) / width
        if i == peak_cell and peak_fraction > 0.01:
            cells.append(f"{WHITE}|{RESET}")
        elif i < filled:
            color = GREEN if position < 0.6 else (YELLOW if position < 0.85 else RED)
            cells.append(f"{color}█{RESET}")
        else:
            cells.append(f"{DIM}·{RESET}")
    cells.append(f"{DIM}]{RESET}")
    return "".join(cells)


def strobe_row(view: MeterView) -> str:
    """The drifting strobe marker plus an in-tune / sharp / flat verdict."""
    active = view.status.strobe_active
    rate = view.strobe_rate()
    if not active:
        verdict, color = f"{DIM}idle{RESET}", DIM
    elif rate is None:
        verdict, color = f"{DIM}...{RESET}", DIM
    elif abs(rate) < IN_TUNE_RATE:
        verdict, color = f"{GREEN}● in tune{RESET}", GREEN
    elif rate < 0.0:
        verdict, color = f"{RED}<- sharp{RESET}", RED
    else:
        verdict, color = f"{CYAN}-> flat{RESET}", CYAN

    position = int(view.status.strobe_phase / FULL_SCALE * (view.width - 1))
    cells = [f"{DIM}[{RESET}"]
    for i in range(view.width):
        if active and i == position:
            cells.append(f"{color}◆{RESET}")
        else:
            cells.append(f"{DIM}·{RESET}")
    cells.append(f"{DIM}]{RESET}")
    return "".join(cells) + f"  {verdict}"


def note_name(note: int | None) -> str:
    """A MIDI note index rendered as a name with octave."""
    if note is None:
        return "--"
    return f"{NOTE_NAMES[note % 12]}{note // 12 - 1}"


def block_indicator(on: bool | None, empty: bool) -> str:
    """The on/off glyph for one effect block."""
    if empty:
        return f"{DIM}·{RESET}"
    if on is True:
        return f"{GREEN}●{RESET}"
    if on is False:
        return f"{DIM}○{RESET}"
    return f"{DIM}·{RESET}"


def _pad(text: str, width: int) -> str:
    """Left-align ``text`` in ``width`` visible characters, truncating if needed."""
    if len(text) > width:
        text = text[: max(width - 1, 0)] + "…"
    return text + " " * (width - len(text))


def effect_block_rows(state: DeviceState, width: int) -> list[str]:
    """The eight effect blocks, four per row."""
    per_row = 4
    # The blocks span the same visual width as a meter row (label + bar).
    cell_width = max((width + LABEL_WIDTH + 8) // per_row, 20)
    rows: list[str] = []
    for start in range(0, len(state.effects), per_row):
        cells = []
        for effect in state.effects[start : start + per_row]:
            empty = effect.kind is None or effect.is_empty
            name = "—" if empty else (effect.type_name or f"type {effect.kind}")
            label = f"{effect.slot:<3} {name}"
            cells.append(f"{block_indicator(effect.on, empty)} {_pad(label, cell_width - 2)}")
        rows.append("  " + "".join(cells).rstrip())
    return rows


def render(view: MeterView, state: DeviceState) -> str:
    """Build the whole frame as one string, cleared line by line."""
    lines: list[str] = []

    def line(text: str = "") -> None:
        lines.append(text + CLEAR_EOL)

    rig = state.rig
    tempo = f"{rig.tempo_bpm} BPM" if rig.tempo_bpm is not None else "-- BPM"
    pulse = view.last_pulse
    lit = (
        pulse is not None and pulse[1] and (_clock() - pulse[0]) < PULSE_FLASH_SECS
    )
    beat = f"{AMBER}♪{RESET}" if lit else f"{DIM}♪{RESET}"

    line(
        f"{BOLD}KEMPER LIVE{RESET}  {DIM}{view.ip}{RESET}   "
        f"frames {view.frames} ({view.message_rate():.1f}/s)"
    )
    line()
    line(f"  {BOLD}RIG{RESET}  {BOLD}{rig.name or '(unknown)'}{RESET}   {beat} {tempo}")
    author = rig.author or "—"
    line(f"  {DIM}by {author}{RESET}")
    line(
        f"  AMP  {_pad(state.amp.name or '—', 24)}"
        f"CAB  {state.cabinet.name or '—'}"
    )
    line()
    for row in effect_block_rows(state, view.width):
        line(row)
    line()

    tuner_note = note_name(state.tuner.note)
    line(f"  {_pad(f'tuner {tuner_note}', LABEL_WIDTH)} {strobe_row(view)}")
    line()

    rows = ALL_ROWS if view.show_all else BAR_ROWS
    for label, ident in rows:
        index = _field_index(ident)
        value = view.status.raw[index]
        low, high = view.mins[index], view.maxs[index]
        if low > high:
            low = high = 0
        line(
            f"  {_pad(label, LABEL_WIDTH)} "
            f"{bar(value / FULL_SCALE, view.peaks[index] / FULL_SCALE, view.width)}  "
            f"{value:>5}  {DIM}range {low:>5}-{high:<5}{RESET}"
        )

    line()
    line(f"  last param: {view.last_param or '(none)'}")
    line(
        f"  {DIM}(play into the device — Ctrl-C to quit; "
        f"--all shows every raw field){RESET}"
    )
    return HOME + "\n".join(lines) + "\n" + CLEAR_EOS


_FIELD_INDEX = {ident: index for index, _n, ident, _name, _r in gen.METER_FIELDS}


def _field_index(ident: str) -> int:
    return _FIELD_INDEX[ident]


# --- the run loop ------------------------------------------------------------


async def run(args: argparse.Namespace) -> int:
    """Connect, sync, and render until the device hangs up or the user quits."""
    ip = args.ip
    if ip is None:
        print("No --ip given; discovering...", file=sys.stderr)
        reply = await find_first(args.discover_timeout)
        if reply is None:
            print("no device found; pass --ip <addr>", file=sys.stderr)
            return 1
        name = reply.name
        print(f'Discovered {name or "a device"} at {reply.ip}', file=sys.stderr)
        ip = reply.ip

    width = min(max(args.width, 8), 512)
    print(f"Connecting to {ip}...", file=sys.stderr)

    model = await DeviceModel.connect(ip, args.port)
    view = MeterView(ip=ip, width=width, show_all=args.all)
    events = model.events()

    out = sys.stdout
    out.write(CLEAR + HIDE_CURSOR)
    out.flush()
    try:
        frame_time = 1.0 / max(args.fps, 1.0)
        loop = asyncio.get_running_loop()
        while True:
            deadline = loop.time() + frame_time
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    event = await asyncio.wait_for(events.get(), remaining)
                except asyncio.TimeoutError:
                    break
                view.on_event(event)
            view.decay()
            out.write(render(view, model.state()))
            out.flush()
            if not model.connected:
                break
    finally:
        await model.close()
        out.write(SHOW_CURSOR + "\n")
        out.flush()

    print("device closed the connection", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """The command-line interface."""
    parser = argparse.ArgumentParser(
        prog="python -m libkp.examples.meters",
        description=(
            "Live terminal view of a Kemper Profiler: rig, amp/cab, the eight "
            "effect blocks, the tuner strobe, and the realtime level meters."
        ),
    )
    parser.add_argument(
        "--ip", help="device IPv4 address; if omitted, discovery finds one"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=gen.PORT,
        help=f"device TCP port (default: {gen.PORT})",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="show all 11 raw meter fields, not just the level bars",
    )
    parser.add_argument(
        "--width",
        type=int,
        default=_default_width(),
        help="bar width in characters (default: fits the terminal)",
    )
    parser.add_argument(
        "--fps", type=float, default=FPS, help=f"render rate (default: {FPS:g})"
    )
    parser.add_argument(
        "--discover-timeout",
        type=float,
        default=3.0,
        metavar="SECONDS",
        help="how long to listen for devices when --ip is omitted (default: 3)",
    )
    return parser


def _default_width() -> int:
    """A bar width that fits the terminal, with a sane fallback when piped."""
    try:
        columns = os.get_terminal_size().columns
    except OSError:
        columns = 100
    return min(max(columns - LABEL_WIDTH - 26, 12), 60)


def main(argv: list[str] | None = None) -> int:
    """Entry point: parse arguments, run the view, restore the terminal."""
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        sys.stdout.write(SHOW_CURSOR + "\n")
        sys.stdout.flush()
        return 130
    except LibKPError as exc:
        sys.stdout.write(SHOW_CURSOR)
        sys.stdout.flush()
        print(f"\nerror: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - manual entry point
    with contextlib.suppress(BrokenPipeError):
        raise SystemExit(main())
