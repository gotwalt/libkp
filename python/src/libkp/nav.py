"""The Navigator's state machine: the one way libkp loads a rig.

A rig load is the one command that can wedge the device: a second load
arriving while the first is still landing leaves it on a delayed fuse, and
the only cure is a power cycle. So loads are never sent directly. A client
*aims* -- :meth:`libkp.model.DeviceModel.navigate_to` and its siblings -- and
this machine decides what goes on the wire and when, holding every language's
model to the same four rules:

- **A burst of taps costs two loads however long it is.** The first tap is
  sent at once; while that move is in flight the aim moves freely and nothing
  else goes out; when the settle elapses the pump sends the final aim, once.
- **An index that was already sent is never re-sent.** An aim past the end of
  the rigs is the case: the device stays put and reports where it is, which
  is not the aim, so the aim waits out the pending window and is then dropped
  -- and only then is the same index sendable again.
- **A position that matches the aim retires it; one that does not is
  ignored.** The device may still be moving, or the aim may be past the end.
- **A new aim while awaiting confirmation cancels the window** and pumps if
  it can.

The machine is pure: it keeps four fields and returns the :class:`NavAction`
list each input calls for, and the model turns those into bytes, timers and
events. ``spec/vectors/navigation.json`` pins its behaviour in every language.
The timings are the spec's: a move is in flight for
:data:`libkp._generated.RIG_LOAD_SETTLE_MS` after it is sent -- the device
pushes the landed rig on both wires within ~400 ms -- and an aim the device
has not confirmed :data:`libkp._generated.PENDING_WINDOW_MS` after its move
settled is dropped. Neither is shortened by an early position report.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "NavigatorState",
    "NavAction",
    "Send",
    "StartSettle",
    "StartWindow",
    "Settled",
    "Dropped",
]


@dataclass(frozen=True, slots=True)
class NavAction:
    """One thing the model must do on the machine's behalf, in order."""


@dataclass(frozen=True, slots=True)
class Send(NavAction):
    """Put the load for ``index`` on the wire: the bank preselect, then the
    slot load that commits it."""

    index: int


@dataclass(frozen=True, slots=True)
class StartSettle(NavAction):
    """Arm the settle timer; its expiry is :meth:`NavigatorState.settle_elapsed`."""


@dataclass(frozen=True, slots=True)
class StartWindow(NavAction):
    """Arm the pending window; its expiry is :meth:`NavigatorState.window_elapsed`."""


@dataclass(frozen=True, slots=True)
class Settled(NavAction):
    """The device confirmed ``index``: raise
    :class:`~libkp.state.NavigationSettled`."""

    index: int


@dataclass(frozen=True, slots=True)
class Dropped(NavAction):
    """The device never confirmed ``index``: raise
    :class:`~libkp.state.NavigationDropped`."""

    index: int


@dataclass(slots=True)
class NavigatorState:
    """The four fields, and the four inputs that move them.

    ``aim`` is where the client wants to be; ``sent`` the index whose load
    was last put on the wire and not yet confirmed or dropped; ``in_flight``
    whether that load's settle is still running; ``awaiting`` whether the
    pending window is open for an aim the device has not confirmed.
    """

    aim: int | None = None
    sent: int | None = None
    in_flight: bool = False
    awaiting: bool = False

    def navigate(self, target: int) -> list[NavAction]:
        """Aim at ``target``. Sends it at once unless a move is in flight, in
        which case the settle will send whatever the aim is by then."""
        self.aim = target
        return self._pump()

    def settle_elapsed(self) -> list[NavAction]:
        """The settle timer fired: the move has landed, whatever the device
        said. An aim that is the sent index and still unconfirmed opens the
        pending window -- an aim that moved on during the flight is simply
        sent now, since a window for the abandoned index would be cancelled by
        that very send."""
        self.in_flight = False
        actions: list[NavAction] = []
        if self.aim is not None and self.aim == self.sent:
            self.awaiting = True
            actions.append(StartWindow())
        actions.extend(self._pump())
        return actions

    def window_elapsed(self) -> list[NavAction]:
        """The pending window fired. While one is open, the aim is dropped
        and the sent index forgotten, so the same index can be sent again.
        Otherwise it is a stale timer and nothing happens."""
        if not self.awaiting or self.aim is None:
            return []
        dropped = self.aim
        self.aim = None
        self.sent = None
        self.awaiting = False
        return [Dropped(dropped)]

    def position(self, index: int) -> list[NavAction]:
        """A position report from either wire. Only a report equal to the aim
        retires it; the settle keeps running so the next aim waits for the
        device all the same."""
        if self.aim is None or self.aim != index:
            return []
        self.aim = None
        self.sent = None
        self.awaiting = False
        return [Settled(index)]

    def _pump(self) -> list[NavAction]:
        """Send the aim when nothing is in flight and it is not the index
        already on the wire."""
        if self.in_flight or self.aim is None or self.aim == self.sent:
            return []
        self.sent = self.aim
        self.in_flight = True
        self.awaiting = False
        return [Send(self.aim), StartSettle()]
