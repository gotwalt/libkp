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

use tokio::sync::mpsc;
use tokio::task::{AbortHandle, JoinHandle};
use tokio::time::Instant;

use super::core::Core;
use super::lane::{self, RequestLane};
use super::links;
use super::nav::Navigator;
use super::{ChannelError, CommandError, ConnectOptions, ControlPolicy, SyncStrategy};
use crate::error::SessionError;
use crate::generated;
use crate::state::{ChannelState, Connection};

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
}

/// The handles of one life of the stream. Aborting them is how a life ends;
/// dropping the command sender is what tells callers the lane is closed.
#[derive(Default)]
struct Life {
    commands: Option<mpsc::Sender<Vec<u8>>>,
    stream: Option<AbortHandle>,
    control: Option<AbortHandle>,
    sync: Option<AbortHandle>,
    reopen: Option<AbortHandle>,
    supervisor: Option<AbortHandle>,
}

impl Life {
    /// End the links of this life: abort every task but the supervisor, and
    /// close the command queue.
    fn abort_links(&mut self) {
        for handle in [
            self.stream.take(),
            self.control.take(),
            self.sync.take(),
            self.reopen.take(),
        ]
        .into_iter()
        .flatten()
        {
            handle.abort();
        }
        self.commands = None;
    }
}

impl Shared {
    fn new(ip: Ipv4Addr, opts: ConnectOptions) -> Self {
        Shared {
            core: Core::new(opts.control),
            lane: RequestLane::new(),
            nav: Navigator::new(),
            ip,
            opts,
            life: Mutex::new(Life::default()),
            closed: AtomicBool::new(false),
            control_attempt: Mutex::new(None),
        }
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
            .send(bytes)
            .await
            .map_err(|_| CommandError::Disconnected)
    }

    /// Put raw MIDI bytes on the current life's command queue without
    /// waiting: what a caller that must return at once — the Navigator's
    /// tap — uses. A full queue is refused as [`CommandError::Disconnected`]
    /// rather than waited out; sixty-four unwritten commands mean the wire is
    /// not draining, and a rig load queued behind them would land who knows
    /// when.
    pub(crate) fn try_enqueue(&self, bytes: Vec<u8>) -> Result<(), CommandError> {
        let sender = self
            .life()
            .commands
            .clone()
            .ok_or(CommandError::Disconnected)?;
        sender
            .try_send(bytes)
            .map_err(|_| CommandError::Disconnected)
    }

    /// Close everything: cancel every task, drop both sockets with them, and
    /// let the core say [`Connection::Disconnected`]. Idempotent — the flag
    /// makes the second call a no-op before it touches anything.
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
}

/// The whole connect: open the stream (its errors propagate — the stream is
/// required), start the first life, and hand the stream to the supervisor.
pub(crate) async fn connect(
    ip: Ipv4Addr,
    opts: ConnectOptions,
) -> Result<Arc<Shared>, SessionError> {
    let (session, tail) = links::open_stream(ip, opts.port).await?;
    let shared = Arc::new(Shared::new(ip, opts));
    let epoch = shared.core.epoch();
    let stream = start_life(&shared, epoch, session, tail, Connection::Disconnected).await?;
    let supervisor = tokio::spawn(supervise(shared.clone(), stream));
    shared.life().supervisor = Some(supervisor.abort_handle());
    Ok(shared)
}

/// Bring one life up on an open stream session: ingest and writer, the
/// connected transition, the sync burst, the control link per policy. Returns
/// the stream task's handle, which completes when the stream ends.
///
/// Under [`ControlPolicy::Required`] the control attempt is awaited, and its
/// failure ends the life again — the stream is closed first — with the
/// connection left at `on_failure` and the error returned.
async fn start_life(
    shared: &Arc<Shared>,
    epoch: u64,
    session: crate::session::Session,
    tail: Vec<u8>,
    on_failure: Connection,
) -> Result<JoinHandle<()>, SessionError> {
    let (commands, stream) = links::spawn_stream(shared.clone(), epoch, session, tail);
    {
        let mut life = shared.life();
        life.commands = Some(commands);
        life.stream = Some(stream.abort_handle());
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
            spawn_control(shared, epoch);
        }
        ControlPolicy::Required => {
            let outcome = match spawn_control(shared, epoch).await {
                Ok(outcome) => outcome,
                Err(_) => Err(SessionError::Closed),
            };
            if let Err(e) = outcome {
                shared.life().abort_links();
                // Wait for the stream task to actually be gone, so that its
                // session — and the socket — is closed before this returns.
                let _ = stream.await;
                shared.core.bump_epoch();
                shared.nav.clear();
                shared.core.stream_lost(on_failure);
                return Err(e);
            }
        }
    }

    if shared.opts.control != ControlPolicy::Off {
        if let Some(every) = shared.opts.reconnect.control_reopen {
            let reopen = tokio::spawn(reopen_loop(shared.clone(), epoch, every));
            shared.life().reopen = Some(reopen.abort_handle());
        }
    }
    Ok(stream)
}

/// Start one control attempt. The task claims the link for itself as its
/// first act — atomically in the core, so two attempts cannot both proceed —
/// and stamps the attempt for the reopen gap.
fn spawn_control(shared: &Arc<Shared>, epoch: u64) -> JoinHandle<Result<(), SessionError>> {
    let task = tokio::spawn(links::run_control(shared.clone(), epoch));
    shared.life().control = Some(task.abort_handle());
    task
}

/// Await the stream's end, then either report the loss or run the connect
/// sequence again until it succeeds or the model is closed.
async fn supervise(shared: Arc<Shared>, mut stream: JoinHandle<()>) {
    loop {
        let _ = stream.await;
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
            let started = match links::open_stream(shared.ip, shared.opts.port).await {
                Ok((session, tail)) => start_life(&shared, epoch, session, tail, failure)
                    .await
                    .ok(),
                Err(_) => None,
            };
            match started {
                Some(task) => {
                    stream = task;
                    break;
                }
                None => {
                    attempt += 1;
                    delay = backoff.next(delay);
                    shared.core.set_connection(failure);
                }
            }
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
            spawn_control(&shared, epoch);
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
    // If another caller claims the link first, the attempt returns `Ok` at
    // once: it is being opened, which is what was asked.
    match spawn_control(shared, epoch).await {
        Ok(Ok(())) => Ok(()),
        Ok(Err(e)) => Err(ChannelError::Session(e)),
        Err(_) => Err(ChannelError::Disconnected),
    }
}
