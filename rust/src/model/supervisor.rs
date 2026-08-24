//! The supervisor: connect ordering, loss, reconnect, and channel health.
//!
//! [`Shared`] is what every task and every handle holds. It owns the core,
//! the lane, the options, and the current *life* — the task handles and the
//! command queue of the stream that is up now. A life ends when the stream
//! ends; if a reconnect policy is set the supervisor starts the next one on
//! the same `Shared`, so the receivers and the tree carry over, and the
//! core's epoch turns so that nothing left of the old life can write into the
//! new one.

use std::net::Ipv4Addr;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::Duration;

use tokio::sync::{mpsc, oneshot};
use tokio::task::{AbortHandle, JoinHandle};
use tokio::time::Instant;

use super::core::Core;
use super::lane::{self, RequestLane};
use super::links;
use super::nav::Navigator;
use super::{ChannelError, CommandError, ConnectOptions, ControlPolicy, SyncStrategy};
use crate::error::SessionError;
use crate::generated;
use crate::state::{Channel, ChannelState, Connection};

/// Everything the clones of a model, and its tasks, share.
pub(crate) struct Shared {
    pub(crate) core: Core,
    pub(crate) lane: RequestLane,
    /// The one way a rig is loaded: the aim, the pacing, the two timers.
    pub(crate) nav: Navigator,
    pub(crate) ip: Ipv4Addr,
    pub(crate) opts: ConnectOptions,
    life: Mutex<Life>,
    closed: AtomicBool,
    /// When the control link was last *attempted*, succeeded or not — the
    /// instant [`generated::CONTROL_REOPEN_MIN_GAP_MS`] counts from. An
    /// attempt the device refused still cost it a connection.
    control_attempt: Mutex<Option<Instant>>,
    /// The stream task sends the epoch of its life here when its socket ends,
    /// so the supervisor is woken by an ended stream rather than by owning its
    /// join handle — which now lives in [`Life`] so [`close`](Self::close) can
    /// await the socket closing. An aborted stream task never sends, so a
    /// teardown does not wake the supervisor spuriously.
    stream_ended: mpsc::Sender<u64>,
}

/// The handles of one life of the stream. Aborting them is how a life ends;
/// dropping the command sender is what tells callers the lane is closed. The
/// stream and control tasks are kept as join handles, not just abort handles,
/// so [`Shared::close`] can await them — and with them the sockets closing and
/// the ledger's close stamp — before it returns.
#[derive(Default)]
struct Life {
    commands: Option<mpsc::Sender<Vec<Vec<u8>>>>,
    stream: Option<JoinHandle<()>>,
    control: Option<JoinHandle<()>>,
    sync: Option<AbortHandle>,
    reopen: Option<AbortHandle>,
    supervisor: Option<AbortHandle>,
}

impl Life {
    /// End the links of this life: abort every task but the supervisor, and
    /// close the command queue. Does not wait for them; that is
    /// [`Shared::close`]'s job on the shutdown path.
    fn abort_links(&mut self) {
        if let Some(h) = self.stream.take() {
            h.abort();
        }
        if let Some(h) = self.control.take() {
            h.abort();
        }
        for handle in [self.sync.take(), self.reopen.take()].into_iter().flatten() {
            handle.abort();
        }
        self.commands = None;
    }
}

impl Shared {
    fn new(ip: Ipv4Addr, opts: ConnectOptions, stream_ended: mpsc::Sender<u64>) -> Self {
        Shared {
            core: Core::new(opts.control),
            lane: RequestLane::new(),
            nav: Navigator::new(),
            ip,
            opts,
            life: Mutex::new(Life::default()),
            closed: AtomicBool::new(false),
            control_attempt: Mutex::new(None),
            stream_ended,
        }
    }

    /// A sender the stream task signals when its socket ends.
    pub(crate) fn stream_ended(&self) -> mpsc::Sender<u64> {
        self.stream_ended.clone()
    }

    fn life(&self) -> MutexGuard<'_, Life> {
        match self.life.lock() {
            Ok(g) => g,
            Err(poisoned) => poisoned.into_inner(),
        }
    }

    fn is_closed(&self) -> bool {
        self.closed.load(Ordering::SeqCst)
    }

    /// Put raw MIDI bytes on the current life's command queue.
    pub(crate) async fn enqueue(&self, bytes: Vec<u8>) -> Result<(), CommandError> {
        let sender = self
            .life()
            .commands
            .clone()
            .ok_or(CommandError::Disconnected)?;
        sender
            .send(vec![bytes])
            .await
            .map_err(|_| CommandError::Disconnected)
    }

    /// Put the Navigator's two-message rig load on the current life's
    /// command queue without waiting: a tap must return at once. Both
    /// messages go as one queue item, written back to back; a full queue is
    /// refused as [`CommandError::Disconnected`] rather than waited out —
    /// sixty-four unwritten commands mean the wire is not draining, and a
    /// rig load queued behind them would land who knows when — and a refusal
    /// queues neither, so no orphaned bank preselect is left arming the
    /// device for a load that never followed.
    pub(crate) fn try_enqueue_pair(
        &self,
        first: Vec<u8>,
        second: Vec<u8>,
    ) -> Result<(), CommandError> {
        let sender = self
            .life()
            .commands
            .clone()
            .ok_or(CommandError::Disconnected)?;
        // The pair travels as one queue item, but it is two commands: refuse
        // it unless the queue has room for both, the bound Swift's per-command
        // queue applies, so all three languages refuse at the same depth.
        if sender.capacity() < 2 {
            return Err(CommandError::Disconnected);
        }
        sender
            .try_send(vec![first, second])
            .map_err(|_| CommandError::Disconnected)
    }

    /// Close everything: cancel every task, drop both sockets with them, and
    /// let the core say [`Connection::Disconnected`]. Idempotent — the flag
    /// makes the second call a no-op before it touches anything.
    ///
    /// The synchronous form, for the last handle's [`Drop`]: it aborts and
    /// returns without waiting for the sockets to finish closing. A caller that
    /// needs the sockets gone — and the ledger's close stamp laid down — before
    /// it moves on uses [`close`](Self::close).
    pub(crate) fn shutdown(&self) {
        if self.closed.swap(true, Ordering::SeqCst) {
            return;
        }
        {
            let mut life = self.life();
            if let Some(supervisor) = life.supervisor.take() {
                supervisor.abort();
            }
            life.abort_links();
        }
        self.core.bump_epoch();
        self.nav.clear();
        self.core.closed();
    }

    /// Close everything and wait for the sockets to actually close before
    /// returning: cancel every task, await the aborted stream and control
    /// tasks (so their sessions drop and the ledger records the close), then
    /// let the core say [`Connection::Disconnected`]. Idempotent. This is what
    /// [`DeviceModel::close`](super::DeviceModel::close) awaits, so a connect
    /// issued straight after it cannot slip past the ledger before the old
    /// close is stamped.
    pub(crate) async fn close(&self) {
        if self.closed.swap(true, Ordering::SeqCst) {
            return;
        }
        let (stream, control) = {
            let mut life = self.life();
            if let Some(supervisor) = life.supervisor.take() {
                supervisor.abort();
            }
            if let Some(h) = life.sync.take() {
                h.abort();
            }
            if let Some(h) = life.reopen.take() {
                h.abort();
            }
            life.commands = None;
            (life.stream.take(), life.control.take())
        };
        if let Some(h) = &stream {
            h.abort();
        }
        if let Some(h) = &control {
            h.abort();
        }
        self.core.bump_epoch();
        self.nav.clear();
        // Await the aborted tasks: each drops its `Session` as it unwinds, and
        // the ledger is stamped in that drop, so it is laid down before this
        // returns.
        if let Some(h) = stream {
            let _ = h.await;
        }
        if let Some(h) = control {
            let _ = h.await;
        }
        self.core.closed();
    }

    pub(crate) fn note_control_attempt(&self) {
        *self
            .control_attempt
            .lock()
            .unwrap_or_else(|e| e.into_inner()) = Some(Instant::now());
    }

    fn last_control_attempt(&self) -> Option<Instant> {
        *self
            .control_attempt
            .lock()
            .unwrap_or_else(|e| e.into_inner())
    }

    /// Forget the last control attempt, so a reopen is not refused as
    /// [`ChannelError::TooSoon`]. A test hook: the reopen gap is thirty
    /// seconds, longer than any test may wait, and there is no way to reach the
    /// success path of `reopen_control` without shortening it.
    pub(crate) fn clear_control_attempt(&self) {
        *self
            .control_attempt
            .lock()
            .unwrap_or_else(|e| e.into_inner()) = None;
    }
}

/// The whole connect: open the stream (its errors propagate — the stream is
/// required), start the first life, and start the supervisor.
pub(crate) async fn connect(
    ip: Ipv4Addr,
    opts: ConnectOptions,
) -> Result<Arc<Shared>, SessionError> {
    let (session, tail) = links::open_stream(ip, opts.port).await?;
    let (loss_tx, loss_rx) = mpsc::channel(8);
    let shared = Arc::new(Shared::new(ip, opts, loss_tx));
    let epoch = shared.core.epoch();
    start_life(&shared, epoch, session, tail, Connection::Disconnected).await?;
    let supervisor = tokio::spawn(supervise(shared.clone(), loss_rx));
    shared.life().supervisor = Some(supervisor.abort_handle());
    Ok(shared)
}

/// Bring one life up on an open stream session: ingest and writer, the
/// connected transition, the sync burst, the control link per policy. The
/// stream task's join handle is kept in [`Life`] so a close can await it; the
/// supervisor learns of the stream's end through [`Shared::stream_ended`].
///
/// Under [`ControlPolicy::Required`] the control link's *open* is awaited (not
/// its whole life), and its failure ends the life again — the stream is closed
/// first — with the connection left at `on_failure` and the error returned.
async fn start_life(
    shared: &Arc<Shared>,
    epoch: u64,
    session: crate::session::Session,
    tail: Vec<u8>,
    on_failure: Connection,
) -> Result<(), SessionError> {
    let (commands, stream) = links::spawn_stream(shared.clone(), epoch, session, tail);
    {
        let mut life = shared.life();
        life.commands = Some(commands);
        life.stream = Some(stream);
    }
    shared.core.stream_opened(epoch);

    if shared.opts.sync == SyncStrategy::StreamBurst {
        let s = shared.clone();
        let sync = tokio::spawn(async move {
            // Its failures are the individual requests' — each already raised
            // its own event; the burst is done either way.
            let _ = lane::refresh(&s, |_| true).await;
            s.core.stream_synced(epoch);
        });
        shared.life().sync = Some(sync.abort_handle());
    }

    match shared.opts.control {
        ControlPolicy::Off => {}
        ControlPolicy::BestEffort => {
            let (task, _opened) = spawn_control(shared, epoch);
            shared.life().control = Some(task);
        }
        ControlPolicy::Required => {
            let (task, opened) = spawn_control(shared, epoch);
            match opened.await {
                // Open, or being opened by a sibling attempt: keep the task so
                // the close path can end it, and go on.
                Ok(Ok(())) | Err(_) => {
                    shared.life().control = Some(task);
                }
                Ok(Err(e)) => {
                    // The open failed: end the half-up life, wait for the
                    // stream socket to close, and hand the error back.
                    let stream = shared.life().stream.take();
                    shared.life().abort_links();
                    if let Some(h) = stream {
                        h.abort();
                        let _ = h.await;
                    }
                    shared.core.bump_epoch();
                    shared.nav.clear();
                    shared.core.stream_lost(on_failure);
                    return Err(e);
                }
            }
        }
    }

    if shared.opts.control != ControlPolicy::Off {
        if let Some(every) = shared.opts.reconnect.control_reopen {
            let reopen = tokio::spawn(reopen_loop(shared.clone(), epoch, every));
            shared.life().reopen = Some(reopen.abort_handle());
        }
    }
    Ok(())
}

/// Start one control attempt, returning its task and a one-shot that resolves
/// when the link is *open* — `Ok(())` when it opened (or a sibling attempt is
/// opening it), `Err` when the open failed — rather than when the whole life
/// ends. The task claims the link for itself as its first act, atomically in
/// the core so two attempts cannot both proceed, and stamps the attempt for
/// the reopen gap.
fn spawn_control(
    shared: &Arc<Shared>,
    epoch: u64,
) -> (JoinHandle<()>, oneshot::Receiver<Result<(), SessionError>>) {
    let (opened_tx, opened_rx) = oneshot::channel();
    let task = tokio::spawn(links::run_control(shared.clone(), epoch, opened_tx));
    (task, opened_rx)
}

/// Wait for a life's stream to end, then either report the loss or run the
/// connect sequence again until it succeeds or the model is closed. Woken by
/// [`Shared::stream_ended`]; an aborted stream task never signals, so a
/// teardown does not wake this.
async fn supervise(shared: Arc<Shared>, mut loss_rx: mpsc::Receiver<u64>) {
    while loss_rx.recv().await.is_some() {
        if shared.is_closed() {
            return;
        }
        // The old life is over: nothing of it may write again, both sockets
        // go together, and an aim with no wire to confirm it on is forgotten
        // without a word.
        shared.core.bump_epoch();
        shared.life().abort_links();
        shared.nav.clear();

        let Some(backoff) = shared.opts.reconnect.stream else {
            shared.core.stream_lost(Connection::Disconnected);
            return;
        };
        let mut attempt = 1u32;
        let mut delay = backoff.initial;
        shared
            .core
            .stream_lost(Connection::Reconnecting { attempt });
        loop {
            // The backoff is the only wait here; the ledger adds its own
            // cooldown inside the dial, and nothing else.
            tokio::time::sleep(delay).await;
            if shared.is_closed() {
                return;
            }
            let failure = Connection::Reconnecting {
                attempt: attempt + 1,
            };
            let epoch = shared.core.epoch();
            // The stream goes Connecting before each dial and Unavailable on a
            // failed one, so a subscriber sees Lost → Connecting → Open (or
            // Lost → Connecting → Unavailable → Connecting …) across a
            // reconnect, matching the other two languages.
            shared
                .core
                .set_channel(epoch, Channel::Stream, ChannelState::Connecting);
            let opened = match links::open_stream(shared.ip, shared.opts.port).await {
                Ok((session, tail)) => start_life(&shared, epoch, session, tail, failure)
                    .await
                    .is_ok(),
                Err(_) => false,
            };
            if opened {
                break;
            }
            shared
                .core
                .set_channel(epoch, Channel::Stream, ChannelState::Unavailable);
            attempt += 1;
            delay = backoff.next(delay);
            shared.core.set_connection(failure);
        }
    }
}

/// While the stream is up, reopen the control link after it fails or is lost,
/// at most once per `every` — floored at the device's minimum gap — since the
/// last attempt. Polled rather than event-driven: the wait is tens of
/// seconds, and a poll cannot lag.
async fn reopen_loop(shared: Arc<Shared>, epoch: u64, every: Duration) {
    let gap = every.max(Duration::from_millis(generated::CONTROL_REOPEN_MIN_GAP_MS));
    let tick = Duration::from_secs(1);
    loop {
        tokio::time::sleep(tick).await;
        if shared.core.epoch() != epoch || shared.core.stream_state() != ChannelState::Open {
            return;
        }
        let down = matches!(
            shared.core.control_state(),
            ChannelState::Unavailable | ChannelState::Lost
        );
        let due = shared
            .last_control_attempt()
            .is_none_or(|t| t.elapsed() >= gap);
        if down && due {
            let (task, _opened) = spawn_control(&shared, epoch);
            shared.life().control = Some(task);
        }
    }
}

/// An explicit reopen of the control link. See
/// [`DeviceModel::reopen_control`](super::DeviceModel::reopen_control).
pub(crate) async fn reopen_control(shared: &Arc<Shared>) -> Result<(), ChannelError> {
    if shared.opts.control == ControlPolicy::Off {
        return Err(ChannelError::Off);
    }
    if shared.is_closed() || shared.core.stream_state() != ChannelState::Open {
        return Err(ChannelError::Disconnected);
    }
    if matches!(
        shared.core.control_state(),
        ChannelState::Connecting | ChannelState::Open
    ) {
        return Ok(());
    }
    let gap = Duration::from_millis(generated::CONTROL_REOPEN_MIN_GAP_MS);
    if shared
        .last_control_attempt()
        .is_some_and(|t| t.elapsed() < gap)
    {
        return Err(ChannelError::TooSoon);
    }
    let epoch = shared.core.epoch();
    let (task, opened) = spawn_control(shared, epoch);
    // If another caller claims the link first, the one-shot is dropped and this
    // returns `Ok` at once: it is being opened, which is what was asked.
    let result = match opened.await {
        Ok(Ok(())) | Err(_) => Ok(()),
        Ok(Err(e)) => Err(ChannelError::Session(e)),
    };
    shared.life().control = Some(task);
    result
}
