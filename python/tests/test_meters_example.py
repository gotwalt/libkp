"""The bundled ``meters`` example: its pure rendering helpers and one live run."""

from __future__ import annotations

import asyncio
import io
import os
import re
import sys

import pytest
from fake_device import FakeDevice

from libkp import _generated as gen
from libkp.examples import meters
from libkp.nrpn import PAGE_STRINGS, set_single, sysex, u14_split
from libkp.state import BeatPulse, DeviceState, ParamChanged, RealtimeStatus, Status

ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def plain(text: str) -> str:
    """Strip ANSI escapes so assertions read the visible content."""
    return ANSI.sub("", text)


def status(values: list[int]) -> RealtimeStatus:
    return RealtimeStatus(raw=tuple(values))


def meter_message(values: list[int]) -> bytes:
    payload = bytearray()
    for v in values:
        payload.extend(u14_split(v))
    return sysex(0x00, 0x00, 0x02, gen.PAGE_REALTIME, gen.METER_BLOCK_NUMBER, bytes(payload))


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_cli_parses_the_documented_flags():
    args = meters.build_parser().parse_args(["--ip", "10.0.0.5", "--all", "--width", "30"])
    assert (args.ip, args.all, args.width) == ("10.0.0.5", True, 30)

    default = meters.build_parser().parse_args([])
    assert default.ip is None
    assert default.all is False
    assert default.width >= 12


def test_help_is_available():
    with pytest.raises(SystemExit) as excinfo:
        meters.build_parser().parse_args(["--help"])
    assert excinfo.value.code == 0


def test_bar_fills_proportionally_and_marks_the_peak():
    empty = plain(meters.bar(0.0, 0.0, 10))
    assert empty == "[··········]"

    full = plain(meters.bar(1.0, 1.0, 10))
    assert full.count("█") == 9 and full.count("|") == 1

    half = plain(meters.bar(0.5, 0.5, 10))
    assert half.count("█") == 5 and half.count("|") == 1
    assert half.index("|") == 6  # the peak marker sits just past the fill


def test_bar_clamps_out_of_range_input():
    assert plain(meters.bar(-5.0, -5.0, 6)) == "[······]"
    assert plain(meters.bar(9.0, 9.0, 6)).count("·") == 0


def test_note_name_rendering():
    assert meters.note_name(None) == "--"
    assert meters.note_name(69) == "A4"
    assert meters.note_name(0) == "C-1"


def test_block_indicator_states():
    assert plain(meters.block_indicator(True, False)) == "●"
    assert plain(meters.block_indicator(False, False)) == "○"
    assert plain(meters.block_indicator(None, False)) == "·"
    assert plain(meters.block_indicator(True, True)) == "·"


def test_effect_block_rows_show_every_slot():
    state = DeviceState()
    state.effects[0].kind = 33
    state.effects[0].on = True
    state.effects[7].kind = 179
    state.effects[7].on = False
    rows = plain("\n".join(meters.effect_block_rows(state, 44)))
    for slot in ("A", "B", "C", "D", "X", "MOD", "DLY", "REV"):
        assert slot in rows
    assert "Green Scream" in rows
    assert "Easy Reverb" in rows
    assert "—" in rows  # empty slots


def test_strobe_verdicts_follow_the_drift_rate():
    view = meters.MeterView(ip="127.0.0.1", width=20, show_all=False)
    assert "idle" in plain(meters.strobe_row(view))

    now = meters._clock()
    view.status = status([1, 0, 0, 8000, 0, 0, 0, 0, 0, 0, 0])
    view.phase_history.extend([(now - 0.20, 8000), (now - 0.10, 8005), (now, 8010)])
    assert "in tune" in plain(meters.strobe_row(view))

    view.phase_history.clear()
    view.phase_history.extend([(now - 0.20, 8000), (now - 0.10, 7000), (now, 6000)])
    assert "sharp" in plain(meters.strobe_row(view))

    view.phase_history.clear()
    view.phase_history.extend([(now - 0.20, 6000), (now - 0.10, 7000), (now, 8000)])
    assert "flat" in plain(meters.strobe_row(view))


def test_strobe_rate_is_wrap_aware():
    view = meters.MeterView(ip="127.0.0.1", width=20, show_all=False)
    now = meters._clock()
    # 16300 -> 100 is a small step forward across the wrap, not a huge jump back.
    view.phase_history.extend([(now - 0.2, 16300), (now - 0.1, 16383), (now, 100)])
    rate = view.strobe_rate()
    assert rate is not None and 0 < rate < 3000


def test_strobe_rate_needs_enough_samples():
    view = meters.MeterView(ip="127.0.0.1", width=20, show_all=False)
    assert view.strobe_rate() is None


def test_view_tracks_status_peaks_and_ranges():
    view = meters.MeterView(ip="127.0.0.1", width=20, show_all=False)
    view.on_event(Status(status([0, 0, 0, 0, 9000, 0, 0, 0, 0, 0, 0])))
    view.on_event(Status(status([0, 0, 0, 0, 3000, 0, 0, 0, 0, 0, 0])))
    assert view.frames == 2
    assert view.peaks[4] == 9000.0
    assert (view.mins[4], view.maxs[4]) == (3000, 9000)
    assert view.message_rate() > 0


def test_peak_decays_but_never_below_the_current_value():
    view = meters.MeterView(ip="127.0.0.1", width=20, show_all=False)
    view.on_event(Status(status([0, 0, 0, 0, 16383, 0, 0, 0, 0, 0, 0])))
    view.on_event(Status(status([0, 0, 0, 0, 100, 0, 0, 0, 0, 0, 0])))
    # Decay is clamped per call, so several frames are needed to fall all the way.
    for _ in range(10):
        view.last_render = meters._clock() - 10.0
        view.decay()
    assert view.peaks[4] == 100.0


def test_view_records_the_last_param_and_beat_pulse():
    view = meters.MeterView(ip="127.0.0.1", width=20, show_all=False)
    view.on_event(ParamChanged(0x0A, 4, 6925))
    assert view.last_param == "Amplifier: Gain = 6925"
    view.on_event(BeatPulse(True))
    assert view.last_pulse is not None and view.last_pulse[1] is True


def test_render_shows_the_patch_header_blocks_and_bars():
    state = DeviceState()
    state.rig.name = "Test Rig"
    state.rig.author = "Author"
    state.rig.tempo_bpm = 120
    state.amp.name = "Test Amp"
    state.cabinet.name = "Test Cab"
    state.effects[0].kind = 33
    state.effects[0].on = True
    state.tuner.note = 69

    view = meters.MeterView(ip="192.168.1.50", width=24, show_all=False)
    view.on_event(Status(status([0, 0, 0, 0, 9000, 0, 12000, 0, 0, 3000, 0])))
    text = plain(meters.render(view, state))

    assert "Test Rig" in text
    assert "by Author" in text
    assert "120 BPM" in text
    assert "Test Amp" in text and "Test Cab" in text
    assert "Green Scream" in text
    assert "tuner A4" in text
    for label, _ident in meters.BAR_ROWS:
        assert label in text
    assert "last param:" in text


@pytest.mark.parametrize("columns", [72, 80, 100, 120, 160])
def test_default_width_keeps_every_row_inside_the_terminal(monkeypatch, columns):
    """The default bar width must leave the right margin clear.

    A meter row is ``LABEL_WIDTH + ROW_CHROME`` columns wide before the bar, so
    an over-generous default makes every row wrap and the full-screen frame
    tears apart.
    """
    monkeypatch.setattr(meters.os, "get_terminal_size", lambda *_a: os.terminal_size((columns, 40)))
    view = meters.MeterView(ip="127.0.0.1", width=meters._default_width(), show_all=True)
    view.on_event(Status(status([gen.FULL_SCALE] * gen.METER_COUNT)))

    frame = plain(meters.render(view, DeviceState()))
    rows = [line for line in frame.split("\n") if "range" in line]
    assert len(rows) == gen.METER_COUNT
    assert max(len(row) for row in rows) < columns


def test_render_all_shows_every_raw_field():
    view = meters.MeterView(ip="127.0.0.1", width=16, show_all=True)
    text = plain(meters.render(view, DeviceState()))
    assert len(meters.ALL_ROWS) == gen.METER_COUNT
    for label, _ident in meters.ALL_ROWS:
        assert label in text


# ---------------------------------------------------------------------------
# One live run against the stand-in device
# ---------------------------------------------------------------------------


def test_run_renders_a_frame_and_exits_when_the_device_hangs_up(monkeypatch):
    tail = [
        sysex(0x00, 0x00, 0x03, PAGE_STRINGS, 1, b"Test Rig\x00"),
        sysex(0x00, 0x00, 0x03, PAGE_STRINGS, 10, b"Test Amp\x00"),
        sysex(0x00, 0x00, 0x03, PAGE_STRINGS, 32, b"Test Cab\x00"),
        set_single(0x00, 0x00, 0x3D, 0, 179),
        set_single(0x00, 0x00, 0x3D, 3, 1),
        meter_message([0, 0, 0, 0, 9000, 0, 12000, 0, 0, 3000, 0]),
    ]

    async def scenario(buffer: io.StringIO) -> int:
        async with FakeDevice(tail_messages=tail, close_after_handshake=True) as device:
            args = meters.build_parser().parse_args(
                ["--ip", "127.0.0.1", "--port", str(device.port), "--width", "20", "--fps", "60"]
            )
            return await meters.run(args)

    buffer = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buffer)
    code = asyncio.run(scenario(buffer))
    monkeypatch.undo()

    text = plain(buffer.getvalue())
    assert code == 0
    assert "Test Rig" in text
    assert "Test Amp" in text and "Test Cab" in text
    assert "Easy Reverb" in text


def test_main_reports_no_device_found(monkeypatch, capsys):
    async def no_device(_timeout):
        return None

    monkeypatch.setattr(meters, "find_first", no_device)
    assert meters.main([]) == 1
    assert "no device found" in capsys.readouterr().err
