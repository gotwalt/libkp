"""The activity detector — the one consumer of the device's fast lane.

The Profiler pushes a meter frame about twenty times a second, unrequested,
for as long as the stream is open (``docs/07``). That rate is right for a
level meter and wrong for Home Assistant: an entity written per frame would
put 72,000 states an hour into the recorder to say "someone is playing".

So the meter lane is read here and nowhere else, and it produces exactly two
state writes per playing session however long the session runs:

- a plain callback on the model's event stream does two integer comparisons
  per frame and stores a timestamp — no awaits, no state writes, no work that
  scales with how long the note lasts;
- the first crossing of the threshold flips the detector **on** and arms one
  timer;
- when that timer fires, the detector settles **off** if the window has gone
  by with no crossing, and otherwise re-arms itself for the remainder. The
  timer is never re-armed per sample, so a two-hour rehearsal costs the same
  one timer a single chord does.

The level read is ``rig_out_level`` (meter v6), the tap *after* rig volume.
``docs/07`` describes the four candidates: the strobe fields say nothing about
level, ``stack_level`` (v4) ignores rig volume — so a rig deliberately turned
down still reads loud — and ``loudness`` (v9) is a slow RMS that both lags the
first note and tails off after the last. v6 follows playing dynamics
immediately, respects the rig's own volume, and is deliberately blind to the
main/monitor/headphone knobs, so turning the monitors down to practise
quietly does not read as "stopped playing".
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .libkp import _generated as gen
from .libkp.model import DeviceModel
from .libkp.state import DeviceEvent, Status


def raw_threshold(percent: float) -> int:
    """The 14-bit meter value a ``percent``-of-full-scale threshold means."""
    return max(0, min(gen.FULL_SCALE, round(gen.FULL_SCALE * percent / 100.0)))


class ActivityDetector:
    """Whether sound is currently passing through the rig, and when it last did.

    Owns its own subscription to the model (:meth:`start` / :meth:`stop`) and
    tells its listeners only when one of those two answers changes, which is
    what the ``active`` binary sensor and the ``last_activity`` sensor write on.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        model: DeviceModel,
        *,
        window: float,
        threshold: float,
    ) -> None:
        self._hass = hass
        self._model = model
        #: How long without a crossing ends a session, in seconds.
        self._window = window
        #: The threshold as the meter lane reports it: a 14-bit integer, so the
        #: per-frame test is one comparison and no arithmetic.
        self._threshold = raw_threshold(threshold)
        self._active = False
        self._last_signal: datetime | None = None
        self._last_activity: datetime | None = None
        self._cancel_timer: CALLBACK_TYPE | None = None
        self._listeners: list[Callable[[], None]] = []
        self._attached = False

    # -- what the entities read ------------------------------------------

    @property
    def active(self) -> bool:
        """True while the threshold has been crossed inside the window."""
        return self._active

    @property
    def window(self) -> float:
        """The quiet window currently in force, in seconds."""
        return self._window

    @property
    def threshold(self) -> int:
        """The level threshold currently in force, as the meter reports it."""
        return self._threshold

    @property
    def last_activity(self) -> datetime | None:
        """When signal was last seen, as of the most recent transition.

        While :attr:`active` is on this is when the current session began;
        when it goes off it becomes the moment of the last crossing — the last
        note heard — and stays there until the next session starts.
        """
        return self._last_activity

    @callback
    def add_listener(self, callback_: Callable[[], None]) -> CALLBACK_TYPE:
        """Register a callback for transitions; returns its remover."""
        self._listeners.append(callback_)

        @callback
        def remove() -> None:
            if callback_ in self._listeners:
                self._listeners.remove(callback_)

        return remove

    # -- lifecycle -------------------------------------------------------

    @callback
    def start(self) -> None:
        """Begin watching the model's events."""
        if self._attached:
            return
        self._model.add_event_listener(self._on_event)
        self._attached = True

    @callback
    def stop(self) -> None:
        """Stop watching and disarm the timer. Idempotent."""
        if self._attached:
            self._model.remove_event_listener(self._on_event)
            self._attached = False
        self._disarm()

    @callback
    def update_options(self, *, window: float, threshold: float) -> None:
        """Apply new options in place — no reconnect, no reload.

        Every socket to the device costs it something (``docs/11``), so
        changing a number in the options form must not cost a session. A
        shorter window is honoured immediately: the armed timer is re-evaluated
        against the new one, which can settle the detector off on the spot.
        """
        self._window = window
        self._threshold = raw_threshold(threshold)
        if self._active:
            self._disarm()
            self._expire(dt_util.utcnow())

    # -- the fast lane ---------------------------------------------------

    @callback
    def _on_event(self, event: DeviceEvent) -> None:
        """Called for every event the model decodes, ~20 Hz of them meters.

        Keep this trivial: it runs inside the model's ingest path.
        """
        if not isinstance(event, Status):
            return
        if event.status.rig_out_level <= self._threshold:
            return
        self._last_signal = dt_util.utcnow()
        if self._active:
            return
        self._active = True
        self._last_activity = self._last_signal
        self._arm(self._window)
        self._notify()

    @callback
    def _arm(self, delay: float) -> None:
        self._cancel_timer = async_call_later(self._hass, delay, self._expire)

    @callback
    def _disarm(self) -> None:
        if self._cancel_timer is not None:
            self._cancel_timer()
            self._cancel_timer = None

    @callback
    def _expire(self, now: datetime) -> None:
        """The window may have run out — settle off, or re-arm for the rest."""
        self._cancel_timer = None
        if not self._active or self._last_signal is None:
            return
        idle = (now - self._last_signal).total_seconds()
        if idle < self._window:
            self._arm(self._window - idle)
            return
        self._active = False
        self._last_activity = self._last_signal
        self._notify()

    @callback
    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()
