//! The core: the single writer of the model's [`DeviceState`].
//!
//! Every value from either link, every connection or channel transition, and
//! every request settlement goes through here, under one lock, and out through
//! the two broadcasts. Nothing else in the model touches the tree. That is
//! what makes "at most one snapshot per chunk" a property rather than a hope:
//! a chunk — one read of the stream, or the whole state dump — is folded in
//! one call, and that call publishes once.
//!
//! The core also keeps the request lane's pending entries, because settling
//! them is a side effect of folding: a request is answered by whichever value
//! next lands at its address, and the core is the only place that sees every
//! value land.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Mutex, RwLock, RwLockReadGuard, RwLockWriteGuard};

use tokio::sync::{broadcast, oneshot};

use super::{ControlPolicy, DeviceEvent, RealtimeStatus};
use crate::generated;
use crate::state::{
    Channel, ChannelState, Connection, Decoded, DeviceState, Navigation, StreamMessage, Update,
    decode_stream,
};

/// What a pending request is waiting for.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum PendingKey {
    /// A numeric value at a flat address — a `$01` or `$06` reply.
    Num(u32),
    /// A string at a flat address — a `$03` or `$07` reply.
    Text(u32),
    /// The `$3C` rendered string for exactly this page, number and value.
    Render { page: u8, number: u8, value: u16 },
}

impl PendingKey {
    /// The flat address the request named, for [`DeviceEvent::RequestTimedOut`]
    /// and for the routing-table lookup that refuses unreadable rows.
    pub(crate) fn address(&self) -> u32 {
        match *self {
            PendingKey::Num(a) | PendingKey::Text(a) => a,
            PendingKey::Render { page, number, .. } => u32::from(page) * 128 + u32::from(number),
        }
    }
}

/// The answer to a request.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum Reply {
    Num(u64),
    Text(String),
}

impl Reply {
    /// The numeric answer. A `Num` key is only ever settled with a `Num`.
    pub(crate) fn num(self) -> u64 {
        match self {
            Reply::Num(v) => v,
            Reply::Text(_) => unreachable!("a numeric request settled with text"),
        }
    }

    /// The string answer. A `Text` or `Render` key is only ever settled with a
    /// `Text`.
    pub(crate) fn text(self) -> String {
        match self {
            Reply::Text(t) => t,
            Reply::Num(_) => unreachable!("a string request settled with a number"),
        }
    }
}

/// One folded chunk, before it is published: the events it raised, whether a
/// slow field moved, and the positions it reported for the Navigator. The
/// caller folds the positions and any dump-end into `events`/`slow` and then
/// publishes once — the whole point of holding the publish back.
#[derive(Default)]
pub(crate) struct Chunk {
    pub(crate) events: Vec<DeviceEvent>,
    pub(crate) slow: bool,
    pub(crate) positions: Vec<u16>,
}

/// One request waiting for its value.
struct PendingEntry {
    id: u64,
    key: PendingKey,
    tx: oneshot::Sender<Reply>,
}

/// The single writer. Held by [`super::supervisor::Shared`] and used by every
/// task through it.
pub(crate) struct Core {
    /// Needed to say whether a missing control link degrades the connection.
    policy: ControlPolicy,
    state: RwLock<DeviceState>,
    snapshots: broadcast::Sender<DeviceState>,
    events: broadcast::Sender<DeviceEvent>,
    pending: Mutex<Vec<PendingEntry>>,
    next_id: AtomicU64,
    /// Which life of the stream is current. Every task is handed the epoch it
    /// was spawned in and every write it makes carries it; a write from an
    /// earlier life — a task that was cancelled but had not yet yielded — is
    /// dropped here, so a late task can never touch the life that replaced it.
    epoch: AtomicU64,
}

impl Core {
    pub(crate) fn new(policy: ControlPolicy) -> Self {
        let (snapshots, _) = broadcast::channel(256);
        let (events, _) = broadcast::channel(1024);
        Core {
            policy,
            state: RwLock::new(DeviceState::new()),
            snapshots,
            events,
            pending: Mutex::new(Vec::new()),
            next_id: AtomicU64::new(1),
            epoch: AtomicU64::new(1),
        }
    }

    // ---- reads -----------------------------------------------------------

    pub(crate) fn state(&self) -> DeviceState {
        self.read().clone()
    }

    pub(crate) fn status(&self) -> RealtimeStatus {
        self.read().status
    }

    pub(crate) fn stream_state(&self) -> ChannelState {
        self.read().channels.stream
    }

    pub(crate) fn control_state(&self) -> ChannelState {
        self.read().channels.control
    }

    /// Join the snapshot store. Joining republishes the current state to
    /// everyone, so the newcomer starts from now rather than from the next
    /// change.
    pub(crate) fn subscribe(&self) -> broadcast::Receiver<DeviceState> {
        let rx = self.snapshots.subscribe();
        let _ = self.snapshots.send(self.state());
        rx
    }

    pub(crate) fn events(&self) -> broadcast::Receiver<DeviceEvent> {
        self.events.subscribe()
    }

    pub(crate) fn epoch(&self) -> u64 {
        self.epoch.load(Ordering::SeqCst)
    }

    /// Start a new life: every task of the old one is now stale.
    pub(crate) fn bump_epoch(&self) -> u64 {
        self.epoch.fetch_add(1, Ordering::SeqCst) + 1
    }

    fn current(&self, epoch: u64) -> bool {
        epoch == self.epoch()
    }

    // ---- folding ---------------------------------------------------------

    /// Fold one chunk of updates from either link into the tree — every update
    /// through the funnel under one lock — and settle whatever requests the
    /// chunk answered. Returns the events, the slow flag, and the positions the
    /// chunk reported, but **does not publish**: the caller forwards the
    /// positions to the Navigator and ends the dump, then publishes the lot
    /// once with [`publish_chunk`](Self::publish_chunk), so a settled aim and a
    /// finished dump ride in the chunk's single snapshot rather than each in
    /// one of their own.
    pub(crate) fn fold_update_chunk(&self, epoch: u64, updates: &[Update]) -> Chunk {
        if !self.current(epoch) || updates.is_empty() {
            return Chunk::default();
        }
        let mut events = Vec::new();
        let mut slow = false;
        let mut positions = Vec::new();
        {
            let mut st = self.write();
            for update in updates {
                let outcome = st.apply_update(update);
                events.extend(outcome.events);
                slow |= outcome.slow_changed;
                positions.extend(outcome.positions);
            }
        }
        self.settle_updates(updates);
        Chunk {
            events,
            slow,
            positions,
        }
    }

    /// Fold one read chunk of unframed stream messages, the same way.
    pub(crate) fn fold_message_chunk(&self, epoch: u64, msgs: &[Vec<u8>]) -> Chunk {
        if !self.current(epoch) || msgs.is_empty() {
            return Chunk::default();
        }
        let decoded: Vec<StreamMessage> = msgs.iter().map(|m| decode_stream(m)).collect();
        let mut events = Vec::new();
        let mut slow = false;
        let mut positions = Vec::new();
        let mut updates = Vec::new();
        {
            let mut st = self.write();
            for message in decoded {
                match message {
                    StreamMessage::Update(update) => {
                        let outcome = st.apply_update(&update);
                        events.extend(outcome.events);
                        slow |= outcome.slow_changed;
                        positions.extend(outcome.positions);
                        updates.push(update);
                    }
                    StreamMessage::Rendered {
                        page,
                        number,
                        value,
                        text,
                    } => events.push(DeviceEvent::RenderedString {
                        page,
                        number,
                        value,
                        text,
                    }),
                    StreamMessage::Ignored => {}
                }
            }
        }
        self.settle_updates(&updates);
        self.settle_rendered(&events);
        Chunk {
            events,
            slow,
            positions,
        }
    }

    /// Publish one chunk's events, then its one snapshot if a slow field moved.
    /// Epoch-guarded: a chunk folded by a life that has since ended says
    /// nothing. The caller has already folded the tree and run the Navigator,
    /// so `slow` reflects the position rows, the settled aim and everything
    /// else together.
    pub(crate) fn publish_chunk(&self, epoch: u64, events: &[DeviceEvent], slow: bool) {
        if self.current(epoch) {
            self.publish(events, slow);
        }
    }

    /// Broadcast the events of one chunk, then its one snapshot if a slow
    /// field moved.
    fn publish(&self, events: &[DeviceEvent], slow: bool) {
        for event in events {
            let _ = self.events.send(event.clone());
        }
        if slow {
            let _ = self.snapshots.send(self.state());
        }
    }

    // ---- dump phase ------------------------------------------------------

    pub(crate) fn begin_dump(&self, epoch: u64) {
        if self.current(epoch) {
            self.write().begin_dump();
        }
    }

    /// End the dump phase. `completed` says whether it ended by its marker or
    /// its settle time — in which case the sync is reported done — or because
    /// the link ended underneath it, in which case nothing completed and
    /// nothing is said. Used by the settle-timer and loss paths, which are not
    /// part of a chunk; a dump that ends *inside* a chunk uses
    /// [`end_dump_report`](Self::end_dump_report) so its
    /// [`DeviceEvent::SyncCompleted`] rides in that chunk's publish.
    pub(crate) fn end_dump(&self, epoch: u64, completed: bool) {
        if !self.current(epoch) {
            return;
        }
        self.write().end_dump();
        if completed {
            let _ = self.events.send(DeviceEvent::SyncCompleted {
                source: Channel::Control,
            });
        }
    }

    /// End the dump phase as part of a chunk: clear the bookkeeping and return
    /// the [`DeviceEvent::SyncCompleted`] for the caller to fold into the
    /// chunk's event list, so it is broadcast *before* the chunk's one
    /// snapshot rather than after it. An empty vector if the life has moved on.
    pub(crate) fn end_dump_report(&self, epoch: u64) -> Vec<DeviceEvent> {
        if !self.current(epoch) {
            return Vec::new();
        }
        self.write().end_dump();
        vec![DeviceEvent::SyncCompleted {
            source: Channel::Control,
        }]
    }

    /// The stream's sync burst finished (every reply landed or timed out).
    pub(crate) fn stream_synced(&self, epoch: u64) {
        if self.current(epoch) {
            let _ = self.events.send(DeviceEvent::SyncCompleted {
                source: Channel::Stream,
            });
        }
    }

    // ---- connection and channels -----------------------------------------

    /// The stream is open: the connection is up. One snapshot for the whole
    /// transition, after the [`DeviceEvent::ChannelChanged`], the legacy
    /// [`DeviceEvent::Connected`] and the [`DeviceEvent::ConnectionChanged`].
    pub(crate) fn stream_opened(&self, epoch: u64) {
        if !self.current(epoch) {
            return;
        }
        let mut events = Vec::new();
        {
            let mut st = self.write();
            st.channels.stream = ChannelState::Open;
            events.push(DeviceEvent::ChannelChanged {
                channel: Channel::Stream,
                state: ChannelState::Open,
            });
            let want = self.connected_or_degraded(&st);
            events.extend(self.transition(&mut st, want));
        }
        self.publish(&events, true);
    }

    /// The stream ended, and with it the control link (both sockets drop
    /// together): the stream is [`ChannelState::Lost`], the control link is
    /// back to [`ChannelState::Closed`] — not `Lost`, since it was closed on
    /// purpose — every pending request fails, and the connection is `next`:
    /// [`Connection::Disconnected`], or the first
    /// [`Connection::Reconnecting`]. One snapshot for all of it.
    ///
    /// Not epoch-guarded: only the supervisor calls it, and it is the
    /// supervisor that turns the epoch.
    pub(crate) fn stream_lost(&self, next: Connection) {
        let mut events = Vec::new();
        {
            let mut st = self.write();
            // The control link is closed *because* the stream went, so its
            // event comes first — the causal order, and the one Python and
            // Swift raise.
            if st.channels.control != ChannelState::Closed {
                st.channels.control = ChannelState::Closed;
                events.push(DeviceEvent::ChannelChanged {
                    channel: Channel::Control,
                    state: ChannelState::Closed,
                });
            }
            if st.channels.stream != ChannelState::Lost {
                st.channels.stream = ChannelState::Lost;
                events.push(DeviceEvent::ChannelChanged {
                    channel: Channel::Stream,
                    state: ChannelState::Lost,
                });
            }
            st.end_dump();
            // No wire, no aim: the Navigator forgets its own, and says nothing.
            st.navigation = Navigation::default();
            events.extend(self.transition(&mut st, next));
        }
        self.fail_pending();
        self.publish(&events, true);
    }

    /// A reconnect attempt failed: count it. Nothing but the connection moves.
    pub(crate) fn set_connection(&self, next: Connection) {
        let events = {
            let mut st = self.write();
            self.transition(&mut st, next)
        };
        if !events.is_empty() {
            self.publish(&events, true);
        }
    }

    /// One link moved. A control-link transition may take the connection
    /// between [`Connection::Connected`] and [`Connection::Degraded`]; the
    /// stream's own transitions are made by the two methods above, which know
    /// what else moves with them.
    pub(crate) fn set_channel(&self, epoch: u64, channel: Channel, state: ChannelState) {
        if !self.current(epoch) {
            return;
        }
        let events = {
            let mut st = self.write();
            self.move_channel(&mut st, channel, state)
        };
        if !events.is_empty() {
            self.publish(&events, true);
        }
    }

    /// Claim the control link for one open attempt: it becomes
    /// [`ChannelState::Connecting`] unless it is already being opened or is
    /// open, in which case `false` — the check and the claim are one write
    /// under the lock, so two callers cannot both start an attempt.
    pub(crate) fn claim_control(&self, epoch: u64) -> bool {
        if !self.current(epoch) {
            return false;
        }
        let events = {
            let mut st = self.write();
            if matches!(
                st.channels.control,
                ChannelState::Connecting | ChannelState::Open
            ) {
                return false;
            }
            self.move_channel(&mut st, Channel::Control, ChannelState::Connecting)
        };
        self.publish(&events, true);
        true
    }

    /// Move one link under the lock, returning the events that say so: none
    /// if it is already there. A control-link move may take the connection
    /// between [`Connection::Connected`] and [`Connection::Degraded`].
    fn move_channel(
        &self,
        st: &mut DeviceState,
        channel: Channel,
        state: ChannelState,
    ) -> Vec<DeviceEvent> {
        let slot = match channel {
            Channel::Stream => &mut st.channels.stream,
            Channel::Control => &mut st.channels.control,
        };
        if *slot == state {
            return Vec::new();
        }
        *slot = state;
        let mut events = vec![DeviceEvent::ChannelChanged { channel, state }];
        // A move *to* `Connecting` does not re-derive the connection: a reopen
        // of a control link that failed or was lost is still a missing link
        // until it is `Open`, so the connection stays `Degraded` through the
        // attempt rather than flicking to `Connected` for the seconds a redial
        // takes and back to `Degraded` if it fails. `Open`, `Unavailable` and
        // `Lost` all re-derive. (On the first connect the connection is already
        // `Connected` and the control link's `Connecting` leaves it there.)
        if state != ChannelState::Connecting
            && matches!(st.connection, Connection::Connected | Connection::Degraded)
        {
            let want = self.connected_or_degraded(st);
            events.extend(self.transition(st, want));
        }
        events
    }

    /// The model is closed: both links [`ChannelState::Closed`], the
    /// connection [`Connection::Disconnected`], every pending request failed.
    /// Says nothing that is already true, so a second close is silent.
    pub(crate) fn closed(&self) {
        let mut events = Vec::new();
        {
            let mut st = self.write();
            // Control before stream, the same order stream loss uses.
            for channel in [Channel::Control, Channel::Stream] {
                let slot = match channel {
                    Channel::Stream => &mut st.channels.stream,
                    Channel::Control => &mut st.channels.control,
                };
                if *slot != ChannelState::Closed {
                    *slot = ChannelState::Closed;
                    events.push(DeviceEvent::ChannelChanged {
                        channel,
                        state: ChannelState::Closed,
                    });
                }
            }
            st.end_dump();
            st.navigation = Navigation::default();
            events.extend(self.transition(&mut st, Connection::Disconnected));
        }
        self.fail_pending();
        if !events.is_empty() {
            self.publish(&events, true);
        }
    }

    // ---- the Navigator's mirror -------------------------------------------

    /// The Navigator moved: mirror its `{aim, in_flight}` into the snapshot
    /// and raise the events its transition produced. The mirror is a slow
    /// field — a UI binds the aim — so a change to it republishes; the events
    /// go out either way, before the snapshot, like every other chunk's.
    pub(crate) fn set_navigation(
        &self,
        epoch: u64,
        navigation: Navigation,
        events: Vec<DeviceEvent>,
    ) {
        if !self.current(epoch) {
            return;
        }
        let slow = {
            let mut st = self.write();
            let changed = st.navigation != navigation;
            st.navigation = navigation;
            changed
        };
        if slow || !events.is_empty() {
            self.publish(&events, slow);
        }
    }

    /// Mirror the Navigator's `{aim, in_flight}` into the tree without
    /// publishing, returning whether it moved. Used while folding a chunk: a
    /// position that settles an aim changes the mirror, and that change must
    /// ride in the chunk's one snapshot rather than trigger a second one.
    pub(crate) fn set_navigation_silent(&self, navigation: Navigation) -> bool {
        let mut st = self.write();
        let changed = st.navigation != navigation;
        st.navigation = navigation;
        changed
    }

    /// With the stream open, the connection is [`Connection::Degraded`] when a
    /// control link that was asked for is not there, and
    /// [`Connection::Connected`] otherwise — a policy of
    /// [`ControlPolicy::Off`] never degrades.
    fn connected_or_degraded(&self, st: &DeviceState) -> Connection {
        let missing = matches!(
            st.channels.control,
            ChannelState::Unavailable | ChannelState::Lost
        );
        if self.policy != ControlPolicy::Off && missing {
            Connection::Degraded
        } else {
            Connection::Connected
        }
    }

    /// Move the connection to `next`, returning the events that says it: the
    /// [`DeviceEvent::ConnectionChanged`] every transition raises, preceded by
    /// the legacy [`DeviceEvent::Connected`] when a session comes up and
    /// [`DeviceEvent::Disconnected`] when it goes.
    ///
    /// "Comes up" is the rule Python and Swift apply: a move *to*
    /// [`Connection::Connected`] from anywhere but [`Connection::Degraded`].
    /// A control link that merely recovers is not a session coming up, and a
    /// session that lands already degraded raises no `Connected` either —
    /// its `ConnectionChanged` says exactly what it is.
    fn transition(&self, st: &mut DeviceState, next: Connection) -> Vec<DeviceEvent> {
        let prev = st.connection;
        if prev == next {
            return Vec::new();
        }
        st.connection = next;
        let mut events = Vec::new();
        if next == Connection::Connected && prev != Connection::Degraded {
            events.push(DeviceEvent::Connected);
        }
        if next == Connection::Disconnected {
            events.push(DeviceEvent::Disconnected);
        }
        events.push(DeviceEvent::ConnectionChanged(next));
        events
    }

    // ---- the request lane's pending entries -------------------------------

    /// Register a request; the receiver resolves when a matching value lands.
    pub(crate) fn register(&self, key: PendingKey) -> (u64, oneshot::Receiver<Reply>) {
        let (tx, rx) = oneshot::channel();
        let id = self.next_id.fetch_add(1, Ordering::SeqCst);
        self.pending_lock().push(PendingEntry { id, key, tx });
        (id, rx)
    }

    /// Forget a request that timed out. Its receiver is simply never settled.
    pub(crate) fn unregister(&self, id: u64) {
        self.pending_lock().retain(|e| e.id != id);
    }

    /// Fail every pending request: their senders drop, and each waiter sees
    /// the lane closed under it.
    pub(crate) fn fail_pending(&self) {
        self.pending_lock().clear();
    }

    /// Raise the event a timed-out request leaves behind.
    pub(crate) fn request_timed_out(&self, address: u32) {
        let _ = self.events.send(DeviceEvent::RequestTimedOut { address });
    }

    /// Settle every pending request a chunk's values answer. Any value at the
    /// address counts, from either wire: an unsolicited push there is as
    /// current as a reply.
    fn settle_updates(&self, updates: &[Update]) {
        if self.pending_lock().is_empty() {
            return;
        }
        for update in updates {
            match &update.decoded {
                Decoded::Num(v) => self.deliver(PendingKey::Num(update.address), Reply::Num(*v)),
                Decoded::Text(t) => {
                    // The fold redacts a secret before it stores; a request's
                    // reply must not hand out what the tree refuses to.
                    let text = if crate::cbor::is_sensitive(update.address) {
                        generated::REDACTED_PLACEHOLDER.to_string()
                    } else {
                        t.clone()
                    };
                    self.deliver(PendingKey::Text(update.address), Reply::Text(text))
                }
                Decoded::Block(values) => {
                    for (i, v) in values.iter().enumerate() {
                        self.deliver(
                            PendingKey::Num(update.address + i as u32),
                            Reply::Num(u64::from(*v)),
                        );
                    }
                }
            }
        }
    }

    /// Settle the render requests a chunk's `$3C` replies answer.
    fn settle_rendered(&self, events: &[DeviceEvent]) {
        if self.pending_lock().is_empty() {
            return;
        }
        for event in events {
            if let DeviceEvent::RenderedString {
                page,
                number,
                value,
                text,
            } = event
            {
                self.deliver(
                    PendingKey::Render {
                        page: *page,
                        number: *number,
                        value: *value,
                    },
                    Reply::Text(text.clone()),
                );
            }
        }
    }

    /// Hand `reply` to every waiter on `key` and forget them.
    fn deliver(&self, key: PendingKey, reply: Reply) {
        let mut pending = self.pending_lock();
        let mut i = 0;
        while i < pending.len() {
            if pending[i].key == key {
                let entry = pending.swap_remove(i);
                let _ = entry.tx.send(reply.clone());
            } else {
                i += 1;
            }
        }
    }

    // ---- locks -----------------------------------------------------------

    /// Acquire the write guard, recovering from a poisoned lock rather than
    /// panicking.
    fn write(&self) -> RwLockWriteGuard<'_, DeviceState> {
        match self.state.write() {
            Ok(g) => g,
            Err(poisoned) => poisoned.into_inner(),
        }
    }

    /// Acquire the read guard, recovering from a poisoned lock rather than
    /// panicking.
    fn read(&self) -> RwLockReadGuard<'_, DeviceState> {
        match self.state.read() {
            Ok(g) => g,
            Err(poisoned) => poisoned.into_inner(),
        }
    }

    fn pending_lock(&self) -> std::sync::MutexGuard<'_, Vec<PendingEntry>> {
        match self.pending.lock() {
            Ok(g) => g,
            Err(poisoned) => poisoned.into_inner(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::Phase;

    /// The legacy `Connected` event follows the rule the other two languages
    /// hand-write: a session coming up, not a control link recovering, and
    /// not a session that lands already degraded.
    #[test]
    fn the_legacy_connected_event_matches_the_other_languages() {
        let core = Core::new(ControlPolicy::BestEffort);
        let mut st = DeviceState::new();
        // Coming up degraded is not the legacy "connected".
        assert_eq!(
            core.transition(&mut st, Connection::Degraded),
            vec![DeviceEvent::ConnectionChanged(Connection::Degraded)]
        );
        // Nor is the control link recovering.
        assert_eq!(
            core.transition(&mut st, Connection::Connected),
            vec![DeviceEvent::ConnectionChanged(Connection::Connected)]
        );
        // A reconnect landing is.
        let _ = core.transition(&mut st, Connection::Reconnecting { attempt: 1 });
        assert_eq!(
            core.transition(&mut st, Connection::Connected),
            vec![
                DeviceEvent::Connected,
                DeviceEvent::ConnectionChanged(Connection::Connected)
            ]
        );
    }

    /// The request lane hands out what the fold would store: a text reply at
    /// a sensitive address is the placeholder, never the secret.
    #[test]
    fn request_replies_are_redacted_at_sensitive_addresses() {
        let core = Core::new(ControlPolicy::Off);
        let secret = generated::SENSITIVE_ADDRESSES[0];
        let (_id, mut redacted) = core.register(PendingKey::Text(secret));
        let (_id, mut clear) = core.register(PendingKey::Text(1));
        let text = |address, text: &str| Update {
            source: Channel::Control,
            phase: Phase::Live,
            address,
            decoded: Decoded::Text(text.to_string()),
        };
        core.settle_updates(&[text(secret, "hunter2"), text(1, "AC30")]);
        assert_eq!(
            redacted.try_recv().unwrap(),
            Reply::Text(generated::REDACTED_PLACEHOLDER.to_string())
        );
        assert_eq!(clear.try_recv().unwrap(), Reply::Text("AC30".to_string()));
    }
}
