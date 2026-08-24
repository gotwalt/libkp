//! The Navigator: the one way a rig is loaded through libkp.
//!
//! A rig load makes the device replay its whole parameter tree, and two of
//! them close together — 8 ms apart is enough — wedge it: it answers the
//! first normally, then closes the session some twenty seconds later and
//! stops accepting connections until it is power cycled. The fuse is delayed,
//! so nothing in the reply to a burst says it did any harm. The Navigator
//! makes overlapping loads structurally impossible from any client: a caller
//! only ever *aims* at a flat rig index, and this module rations the sending.
//!
//! Two halves:
//!
//! - [`NavigatorState`] is the pure state machine, pinned by
//!   `spec/vectors/navigation.json` so the three languages agree on it
//!   transition for transition. It knows nothing of time or sockets: each
//!   entry point returns the [`NavAction`]s the caller must carry out.
//! - `Navigator` (crate-private) is the runtime around it inside the model: it holds the
//!   machine under a lock, executes the actions — puts the load pair on the
//!   stream's command queue, arms and cancels the two timers, raises the
//!   events — and mirrors `{aim, in_flight}` into the snapshot.
//!
//! The pacing is measured (docs/11): after a load the device reports its
//! position within ~40 ms and has pushed the whole landed rig on both wires
//! by ~400 ms. So a move is "in flight" for a fixed
//! [`generated::RIG_LOAD_SETTLE_MS`] after it is sent — right at that edge,
//! and never shortened by the position report, since the rig's own pushes
//! are still streaming when the position lands — and there is no read-back
//! afterwards. An aim the device has not confirmed
//! [`generated::PENDING_WINDOW_MS`] after its move settled is dropped: it was
//! past the end, and the device stayed put and said so.

use std::sync::{Arc, Mutex, MutexGuard};
use std::time::Duration;

use tokio::task::AbortHandle;

use super::supervisor::Shared;
use super::{CC_CHANNEL, DeviceEvent};
use crate::control::Control;
use crate::generated;
use crate::state::{NavDrop, Navigation};

/// The pure Navigator state machine. Starts fresh — nothing aimed, nothing
/// sent, nothing in flight — and is driven by four entry points, each of
/// which returns the actions the caller must carry out, in order.
///
/// The rules it encodes: a burst of taps costs two rig loads however long it
/// is (the aim moves freely while a move is in flight; the pump sends the
/// final aim once the move settles); an index that was already sent is never
/// re-sent; a position report that matches the aim retires it, one that does
/// not is ignored; and a new aim while a window is open cancels the window
/// and is pumped at once.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct NavigatorState {
    /// Where the caller wants to be, until the device confirms it or the
    /// window runs out.
    pub aim: Option<u16>,
    /// The index of the last load put on the wire, remembered so that the
    /// same index is never sent twice: an aim past the end would otherwise
    /// be re-sent every time it settled.
    pub sent: Option<u16>,
    /// A move was sent less than [`generated::RIG_LOAD_SETTLE_MS`] ago.
    pub in_flight: bool,
    /// The sent move has settled, its aim is still unconfirmed, and the
    /// [`generated::PENDING_WINDOW_MS`] window is running.
    pub awaiting: bool,
}

/// What the runtime must do after a transition. `Send` carries the index to
/// load; the two `Start` actions arm one timer each; the last two are the
/// events to raise.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NavAction {
    /// Put the load pair for this flat index on the wire.
    Send(u16),
    /// Arm the [`generated::RIG_LOAD_SETTLE_MS`] timer; its expiry is
    /// [`NavigatorState::settle_elapsed`].
    StartSettle,
    /// Arm the [`generated::PENDING_WINDOW_MS`] timer; its expiry is
    /// [`NavigatorState::window_elapsed`].
    StartWindow,
    /// The device confirmed the aim: raise
    /// [`DeviceEvent::NavigationSettled`].
    Settled(u16),
    /// The aim went unconfirmed for the whole window: raise
    /// [`DeviceEvent::NavigationDropped`].
    Dropped(u16),
}

impl NavigatorState {
    /// Aim at `target`. Sent at once if nothing is in flight and it is not
    /// the index already on the wire; otherwise the aim simply moves, and
    /// the settle sends wherever it ended up. A window that was open for the
    /// previous aim is cancelled by the send.
    pub fn navigate(&mut self, target: u16) -> Vec<NavAction> {
        self.aim = Some(target);
        self.pump()
    }

    /// The settle timer fired: the move is no longer in flight. If the aim
    /// is still the index that was sent — the device has not confirmed it —
    /// the window opens; then whatever the aim is now is pumped. An aim that
    /// moved on during the flight is sent here without a window, because the
    /// window would only be abandoned by that send.
    pub fn settle_elapsed(&mut self) -> Vec<NavAction> {
        self.in_flight = false;
        let mut actions = Vec::new();
        if self.aim.is_some() && self.aim == self.sent {
            self.awaiting = true;
            actions.push(NavAction::StartWindow);
        }
        actions.extend(self.pump());
        actions
    }

    /// The window timer fired. While a window is open, the aim it was opened
    /// for is dropped and the sent index forgotten, so the same index may be
    /// sent again later. A timer from a window that was since cancelled, or
    /// one firing with nothing aimed, does nothing — which is why the runtime
    /// may cancel a timer but never has to.
    pub fn window_elapsed(&mut self) -> Vec<NavAction> {
        if !self.awaiting {
            return Vec::new();
        }
        let Some(aim) = self.aim else {
            return Vec::new();
        };
        self.aim = None;
        self.sent = None;
        self.awaiting = false;
        vec![NavAction::Dropped(aim)]
    }

    /// A position report from either wire. Only a report equal to the aim
    /// retires it (and closes any window); any other is ignored, because the
    /// device may still be moving, or the aim is past the end and the device
    /// is saying where it stayed. The flight is not shortened: the landed
    /// rig's own pushes are still streaming when the position lands.
    pub fn position(&mut self, index: u16) -> Vec<NavAction> {
        if self.aim != Some(index) {
            return Vec::new();
        }
        self.aim = None;
        self.sent = None;
        self.awaiting = false;
        vec![NavAction::Settled(index)]
    }

    /// Send the aim if nothing is in flight and it is not the index already
    /// on the wire. Sending closes any open window: the new move gets its own.
    fn pump(&mut self) -> Vec<NavAction> {
        if self.in_flight {
            return Vec::new();
        }
        let Some(aim) = self.aim else {
            return Vec::new();
        };
        if self.sent == Some(aim) {
            return Vec::new();
        }
        self.sent = Some(aim);
        self.in_flight = true;
        self.awaiting = false;
        vec![NavAction::Send(aim), NavAction::StartSettle]
    }

    /// The `{aim, in_flight}` the snapshot shows of this.
    pub(crate) fn navigation(&self) -> Navigation {
        Navigation {
            aim: self.aim,
            in_flight: self.in_flight,
        }
    }
}

// ---------------------------------------------------------------------------
// The runtime inside the model
// ---------------------------------------------------------------------------

/// The machine plus its two timers, under one lock. Held by
/// [`Shared`]; every entry point below takes the lock, drives the machine,
/// and carries out the actions before releasing it, so two callers — a tap
/// and a timer, say — cannot interleave between a transition and its send.
pub(crate) struct Navigator {
    inner: Mutex<Inner>,
}

/// What the lock guards: the machine and the handles of whichever timers
/// are armed. A handle is kept so that the timer can be cancelled — a window
/// timer from a cancelled window would otherwise fire into the *next*
/// window and cut it short.
#[derive(Default)]
struct Inner {
    state: NavigatorState,
    settle: Option<AbortHandle>,
    window: Option<AbortHandle>,
}

impl Inner {
    fn cancel_settle(&mut self) {
        if let Some(handle) = self.settle.take() {
            handle.abort();
        }
    }

    fn cancel_window(&mut self) {
        if let Some(handle) = self.window.take() {
            handle.abort();
        }
    }
}

impl Navigator {
    pub(crate) fn new() -> Self {
        Navigator {
            inner: Mutex::new(Inner::default()),
        }
    }

    fn lock(&self) -> MutexGuard<'_, Inner> {
        match self.inner.lock() {
            Ok(g) => g,
            Err(poisoned) => poisoned.into_inner(),
        }
    }

    /// Forget everything and cancel both timers, saying nothing: the stream
    /// is gone (or the model is closing), so there is no wire the aim could
    /// be confirmed on. The snapshot's mirror is reset by the core in the
    /// same write as the connection transition.
    pub(crate) fn clear(&self) {
        let mut inner = self.lock();
        inner.cancel_settle();
        inner.cancel_window();
        inner.state = NavigatorState::default();
    }
}

/// Aim at `target`: drive the machine and carry out what it says.
pub(crate) fn navigate(shared: &Arc<Shared>, target: u16) {
    let epoch = shared.core.epoch();
    let mut inner = shared.nav.lock();
    let actions = inner.state.navigate(target);
    execute(shared, epoch, &mut inner, actions);
}

/// A position report folded by the core, from either wire.
pub(crate) fn position(shared: &Arc<Shared>, epoch: u64, index: u16) {
    let mut inner = shared.nav.lock();
    let actions = inner.state.position(index);
    execute(shared, epoch, &mut inner, actions);
}

/// The settle timer's expiry. Stale if the life it was armed in is over.
fn settle_elapsed(shared: &Arc<Shared>, epoch: u64) {
    if shared.core.epoch() != epoch {
        return;
    }
    let mut inner = shared.nav.lock();
    inner.settle = None;
    let actions = inner.state.settle_elapsed();
    execute(shared, epoch, &mut inner, actions);
}

/// The window timer's expiry. Stale if the life it was armed in is over.
fn window_elapsed(shared: &Arc<Shared>, epoch: u64) {
    if shared.core.epoch() != epoch {
        return;
    }
    let mut inner = shared.nav.lock();
    inner.window = None;
    let actions = inner.state.window_elapsed();
    execute(shared, epoch, &mut inner, actions);
}

/// Carry out one transition's actions under the lock, then mirror the
/// machine into the snapshot and raise the events — one publish for the
/// whole transition.
///
/// A `Send` puts the documented pair on the stream's command queue: the
/// bank preselect (CC47) for `index / BANK_SLOTS`, then the slot load
/// (CC50–54) for `index % BANK_SLOTS`, which commits it. The queue is
/// written without waiting, because a tap must return at once: if the
/// stream is down, or the queue is full, the load cannot go and the aim is
/// dropped there and then, with the event to say so.
fn execute(shared: &Arc<Shared>, epoch: u64, inner: &mut Inner, actions: Vec<NavAction>) {
    let mut events = Vec::new();
    for action in actions {
        match action {
            NavAction::Send(index) => {
                // A new move gets its own window; the old one is over.
                inner.cancel_window();
                if send_load(shared, index).is_err() {
                    inner.cancel_settle();
                    inner.state = NavigatorState::default();
                    events.push(DeviceEvent::NavigationDropped {
                        index,
                        reason: NavDrop::Unconfirmed,
                    });
                    break;
                }
            }
            NavAction::StartSettle => {
                inner.cancel_settle();
                inner.settle = Some(arm(
                    shared,
                    Duration::from_millis(generated::RIG_LOAD_SETTLE_MS),
                    move |s| settle_elapsed(&s, epoch),
                ));
            }
            NavAction::StartWindow => {
                inner.cancel_window();
                inner.window = Some(arm(
                    shared,
                    Duration::from_millis(generated::PENDING_WINDOW_MS),
                    move |s| window_elapsed(&s, epoch),
                ));
            }
            NavAction::Settled(index) => {
                inner.cancel_window();
                events.push(DeviceEvent::NavigationSettled { index });
            }
            NavAction::Dropped(index) => {
                inner.cancel_window();
                events.push(DeviceEvent::NavigationDropped {
                    index,
                    reason: NavDrop::Unconfirmed,
                });
            }
        }
    }
    shared
        .core
        .set_navigation(epoch, inner.state.navigation(), events);
}

/// The two messages a load is: bank preselect, then the slot load that
/// commits it. Exactly what a controller sends by hand, and the only place
/// in libkp that puts a rig-load controller on the wire.
fn send_load(shared: &Shared, index: u16) -> Result<(), super::CommandError> {
    let slots = generated::BANK_SLOTS as u16;
    let bank = Control::BankPreselect((index / slots) as u8);
    let slot = Control::LoadSlot((index % slots) as u8 + 1);
    shared.try_enqueue(bank.message(CC_CHANNEL))?;
    shared.try_enqueue(slot.message(CC_CHANNEL))
}

/// Arm one timer: a task that sleeps `after` and then runs `expire` with the
/// shared handle. Its abort handle is what cancels it.
fn arm(
    shared: &Arc<Shared>,
    after: Duration,
    expire: impl FnOnce(Arc<Shared>) + Send + 'static,
) -> AbortHandle {
    let shared = shared.clone();
    tokio::spawn(async move {
        tokio::time::sleep(after).await;
        expire(shared);
    })
    .abort_handle()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The one thing the vectors cannot pin: the machine's mirror is exactly
    /// its aim and flight, nothing of the sent index or the window.
    #[test]
    fn the_mirror_shows_aim_and_flight_only() {
        let mut m = NavigatorState::default();
        assert_eq!(m.navigation(), Navigation::default());
        assert_eq!(
            m.navigate(14),
            vec![NavAction::Send(14), NavAction::StartSettle]
        );
        assert_eq!(
            m.navigation(),
            Navigation {
                aim: Some(14),
                in_flight: true
            }
        );
        assert_eq!(m.settle_elapsed(), vec![NavAction::StartWindow]);
        assert!(m.awaiting);
        assert_eq!(
            m.navigation(),
            Navigation {
                aim: Some(14),
                in_flight: false
            }
        );
    }
}
