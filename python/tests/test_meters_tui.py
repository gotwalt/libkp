"""The bundled Textual ``meters_tui`` example.

The textual-free helpers are tested unconditionally; the widget-level layout
check needs the optional ``tui`` extra and skips without it.
"""

from __future__ import annotations

import meters
import meters_tui
import pytest

from libkp import _generated as gen
from libkp.state import RealtimeStatus

# ---------------------------------------------------------------------------
# Pure helpers — no Textual required
# ---------------------------------------------------------------------------


def _view(raw: tuple[int, ...]) -> meters.MeterView:
    view = meters.MeterView(ip="127.0.0.1", width=48, show_all=False)
    view.status = RealtimeStatus(raw=raw)
    return view


def test_verdict_is_idle_until_the_strobe_moves():
    assert meters_tui.verdict_key(_view((0,) * gen.METER_COUNT)) == "idle"


def test_verdict_is_unknown_until_enough_samples_land():
    view = _view((1, 0, 0, 8000, *(0,) * 7))
    assert meters_tui.verdict_key(view) == "unknown"


@pytest.mark.parametrize(
    "phases,expected",
    [
        ([8000, 8005, 8010], "intune"),
        ([8000, 7000, 6000], "sharp"),
        ([6000, 7000, 8000], "flat"),
    ],
)
def test_verdicts_follow_the_drift_rate(phases, expected):
    view = _view((1, 0, 0, phases[-1], *(0,) * 7))
    now = meters_tui._clock()
    view.phase_history.extend([(now - 0.2, phases[0]), (now - 0.1, phases[1]), (now, phases[2])])
    assert meters_tui.verdict_key(view) == expected


def test_every_verdict_key_has_a_label():
    for key in ("idle", "unknown", "intune", "sharp", "flat"):
        assert key in meters_tui._VERDICTS


def test_level_color_matches_the_ansi_thresholds():
    assert meters_tui.level_color(0.0) == "green"
    assert meters_tui.level_color(0.7) == "yellow"
    assert meters_tui.level_color(0.99) == "red"


def test_cli_mirrors_the_ansi_example():
    args = meters_tui.build_parser().parse_args(["--ip", "10.0.0.5", "--all"])
    assert (args.ip, args.all, args.port) == ("10.0.0.5", True, gen.PORT)


# ---------------------------------------------------------------------------
# Layout — needs the optional `tui` extra
# ---------------------------------------------------------------------------


#: What a ``Panel`` spends before its content: ``border: round`` on both sides
#: plus the ``padding: 0 1`` declared in ``MetersApp.CSS``.
PANEL_CHROME = 4


@pytest.mark.parametrize("columns", [72, 80, 100, 120, 160])
@pytest.mark.parametrize("show_all", [False, True])
def test_meter_bars_are_never_truncated_by_the_panel(monkeypatch, columns, show_all):
    """The bar has to fit the METERS panel's content width.

    Rich crops an over-wide grid cell with an ellipsis rather than wrapping, so
    an over-generous bar width silently chops the right-hand end of every meter
    — including the peak-hold marker.
    """
    pytest.importorskip("textual")
    from rich.console import Console
    from textual.geometry import Size

    app_class = meters_tui._build_app_class()
    app = app_class(meters_tui.build_parser().parse_args([]), None)
    app.view.show_all = show_all
    app.view.status = RealtimeStatus(raw=(gen.FULL_SCALE,) * gen.METER_COUNT)
    app.view.peaks = [float(gen.FULL_SCALE)] * gen.METER_COUNT
    app.view.maxs = [gen.FULL_SCALE] * gen.METER_COUNT
    app.view.mins = [0] * gen.METER_COUNT
    monkeypatch.setattr(app_class, "size", property(lambda _self: Size(columns, 40)))

    console = Console(width=columns - PANEL_CHROME, no_color=True)
    with console.capture() as capture:
        console.print(app._meters(app))
    lines = capture.get().rstrip("\n").split("\n")

    rows = gen.METER_COUNT if show_all else len(meters.BAR_ROWS)
    assert len(lines) == rows, "a wrapped row means the bar overflowed the panel"
    for line in lines:
        assert "…" not in line, f"bar truncated at {columns} columns: {line!r}"
