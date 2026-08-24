//! The two links: the tasks that own the sockets.
//!
//! - The **stream link** owns the MIDI3 [`Session`]: one task that reads the
//!   socket into an [`Unframer`] and hands each read chunk's messages to the
//!   core, and drains the bounded command queue to the wire. It ends when the
//!   socket does; the supervisor is what notices.
//! - The **control link** owns a [`ControlLink`]: one task that reads items
//!   and hands each chunk to the core tagged with the dump phase, ending the
//!   phase at the dump's end marker or its settle time. It writes nothing —
//!   the trigger went out with the open, and there is no queue.
//!
//! Both hand every position the core folds from their chunk to the
//! Navigator, which is how an aim is confirmed whichever wire says so first.

use std::net::Ipv4Addr;
use std::sync::Arc;
use std::time::Duration;

use tokio::sync::{mpsc, oneshot};
use tokio::task::JoinHandle;

use super::nav;
use super::supervisor::Shared;
use crate::cbor::{self, ControlLink};
use crate::error::SessionError;
use crate::generated;
use crate::midi3::{self, Unframer};
use crate::session::{PROTOCOL_MIDI3_STREAM, Session};
use crate::state::{Channel, ChannelState, Phase};

/// Read idle gap driving the ingest loops; short so they react per packet.
const READ_IDLE: Duration = Duration::from_millis(30);
/// Max bytes per stream read.
const READ_MAX: usize = 64 * 1024;
/// How many commands may wait for the writer before a caller blocks.
const COMMAND_QUEUE: usize = 64;

// ---- the stream --------------------------------------------------------

/// Dial `ip:port`, select the streaming protocol, write the preamble. The
/// session and whatever stream bytes rode in on the acceptance line.
pub(crate) async fn open_stream(
    ip: Ipv4Addr,
    port: u16,
) -> Result<(Session, Vec<u8>), SessionError> {
    let mut session = Session::connect_to(ip, port).await?;
    let outcome = session
        .handshake(&[PROTOCOL_MIDI3_STREAM], READ_IDLE)
        .await?;
    session.write_session_preamble().await?;
    Ok((session, outcome.response_tail().to_vec()))
}

/// Start the stream task for one life. The sender is the command queue; the
/// handle completes when the socket ends. When the socket ends *on its own*
/// (not by an abort), the task signals the supervisor through
/// [`Shared::stream_ended`] — an aborted task is cancelled before that line and
/// stays silent, so a teardown does not look like a loss.
pub(crate) fn spawn_stream(
    shared: Arc<Shared>,
    epoch: u64,
    session: Session,
    tail: Vec<u8>,
) -> (mpsc::Sender<Vec<Vec<u8>>>, JoinHandle<()>) {
    let (tx, rx) = mpsc::channel(COMMAND_QUEUE);
    let loss = shared.stream_ended();
    let task = tokio::spawn(async move {
        run_stream(shared, epoch, session, tail, rx).await;
        let _ = loss.send(epoch).await;
    });
    (tx, task)
}

/// Own the stream socket: fold every read chunk, write every command.
async fn run_stream(
    shared: Arc<Shared>,
    epoch: u64,
    mut session: Session,
    tail: Vec<u8>,
    mut commands: mpsc::Receiver<Vec<Vec<u8>>>,
) {
    let mut unframer = Unframer::new();
    // Decode anything that rode in on the handshake acceptance tail.
    fold_messages(&shared, epoch, &unframer.push(&tail));
    loop {
        tokio::select! {
            read = session.read_once(READ_IDLE, READ_MAX) => match read {
                Ok(chunk) => fold_messages(&shared, epoch, &unframer.push(&chunk)),
                // EOF or a read error: the socket is gone, and so is this life.
                Err(_) => return,
            },
            cmd = commands.recv() => match cmd {
                Some(batch) => {
                    // One item is one or more messages framed into a single
                    // write, so the Navigator's pair cannot be split apart.
                    let mut framed = Vec::new();
                    for bytes in &batch {
                        framed.extend_from_slice(&midi3::frame(bytes));
                    }
                    if session.write_all(&framed).await.is_err() {
                        return;
                    }
                }
                // The life was torn down under us: nothing more will be asked.
                None => return,
            },
        }
    }
}

/// Finish one folded chunk and publish it once: forward its positions to the
/// Navigator, end the dump if this chunk closed it, then publish the events
/// and the single snapshot. Holding the publish until here is what folds a
/// settled aim and a finished dump into the chunk's own snapshot instead of
/// each raising one of its own.
fn finish_chunk(shared: &Arc<Shared>, epoch: u64, mut chunk: super::core::Chunk, dump_ended: bool) {
    for index in std::mem::take(&mut chunk.positions) {
        let (events, slow) = nav::fold_position(shared, epoch, index);
        chunk.events.extend(events);
        chunk.slow |= slow;
    }
    if dump_ended {
        // The SyncCompleted rides in the chunk's event list, before its one
        // snapshot, so an app that awaits it then reads the next snapshot is
        // not left one behind.
        chunk.events.extend(shared.core.end_dump_report(epoch));
    }
    shared.core.publish_chunk(epoch, &chunk.events, chunk.slow);
}

/// One read chunk of the stream into the core, its positions on to the
/// Navigator, and one snapshot for the lot.
fn fold_messages(shared: &Arc<Shared>, epoch: u64, msgs: &[Vec<u8>]) {
    let chunk = shared.core.fold_message_chunk(epoch, msgs);
    finish_chunk(shared, epoch, chunk, false);
}

/// One chunk of control-link updates into the core, its positions on to the
/// Navigator, and one snapshot for the lot. `dump_ended` says this chunk
/// carried the run that closed the dump.
pub(crate) fn fold_updates(shared: &Arc<Shared>, epoch: u64, updates: &[crate::state::Update]) {
    let chunk = shared.core.fold_update_chunk(epoch, updates);
    finish_chunk(shared, epoch, chunk, false);
}

/// [`fold_updates`] for the control link, told whether this chunk ended the
/// dump so the [`DeviceEvent::SyncCompleted`](crate::model::DeviceEvent::SyncCompleted)
/// rides in its snapshot.
fn fold_control(
    shared: &Arc<Shared>,
    epoch: u64,
    updates: &[crate::state::Update],
    dump_ended: bool,
) {
    let chunk = shared.core.fold_update_chunk(epoch, updates);
    finish_chunk(shared, epoch, chunk, dump_ended);
}

// ---- the control link ------------------------------------------------------

/// One attempt at the control link, start to finish: open it (dial,
/// handshake, preamble, dump trigger), report it open, fold what it sends
/// until the socket ends, report it lost.
///
/// The dump phase starts with the trigger and ends when the
/// [`generated::DUMP_END_RUNS`]-th run based at [`generated::DUMP_END_ADDRESS`]
/// has been folded, or [`generated::DUMP_SETTLE_MS`] after the trigger if it
/// never comes. A real dump has two sections — a system section closed by one
/// such run, then the rig section closed by a second — so a single run does
/// not end the phase; the count is kept across reads. Items up to and
/// including the closing run fold as [`Phase::Dump`], so a value pushed live
/// meanwhile keeps its authority; everything after folds as [`Phase::Live`].
///
/// `opened` resolves the moment the link is open — `Ok(())` on open (or when a
/// sibling attempt already holds the claim), `Err` when the open failed — so a
/// caller that must know only whether the link came up need not await the whole
/// life. The link ending after it opened is reported through `channels`, not
/// here.
pub(crate) async fn run_control(
    shared: Arc<Shared>,
    epoch: u64,
    opened: oneshot::Sender<Result<(), SessionError>>,
) {
    let core = &shared.core;
    // Claiming the link is what raises `Connecting`; it happens here, in the
    // task, so that a subscriber who joined right after `connect` returned
    // sees the whole `Connecting → Open` sequence.
    if !core.claim_control(epoch) {
        // Another attempt holds the claim: what was asked for is happening.
        let _ = opened.send(Ok(()));
        return;
    }
    shared.note_control_attempt();
    let mut link = match ControlLink::open(shared.ip, shared.opts.port, READ_IDLE).await {
        Ok(link) => link,
        Err(e) => {
            core.set_channel(epoch, Channel::Control, ChannelState::Unavailable);
            let _ = opened.send(Err(e));
            return;
        }
    };
    core.begin_dump(epoch);
    core.set_channel(epoch, Channel::Control, ChannelState::Open);
    let _ = opened.send(Ok(()));

    let settle = tokio::time::sleep(Duration::from_millis(generated::DUMP_SETTLE_MS));
    tokio::pin!(settle);
    let mut dumping = true;
    // Runs based at DUMP_END_ADDRESS seen since the trigger. The phase ends at
    // the DUMP_END_RUNS-th, so a lone run (the system section's) does not.
    let mut end_runs = 0usize;
    loop {
        tokio::select! {
            _ = &mut settle, if dumping => {
                dumping = false;
                core.end_dump(epoch, true);
            }
            read = link.read(READ_IDLE) => match read {
                Ok(items) => {
                    if items.is_empty() {
                        continue;
                    }
                    let mut ended = false;
                    let updates = if !dumping {
                        cbor::updates(&items, Phase::Live)
                    } else {
                        // Find the run that carries the phase's end: the one
                        // that brings the cumulative count to DUMP_END_RUNS.
                        let mut split = None;
                        for index in cbor::dump_end_indices(&items) {
                            end_runs += 1;
                            if end_runs >= generated::DUMP_END_RUNS {
                                split = Some(index);
                                break;
                            }
                        }
                        match split {
                            Some(end) => {
                                // The closing run ends the dump; whatever
                                // follows it in the same read is already live.
                                ended = true;
                                let mut updates = cbor::updates(&items[..=end], Phase::Dump);
                                updates.extend(cbor::updates(&items[end + 1..], Phase::Live));
                                updates
                            }
                            None => cbor::updates(&items, Phase::Dump),
                        }
                    };
                    fold_control(&shared, epoch, &updates, ended);
                    if ended {
                        dumping = false;
                    }
                }
                Err(_) => {
                    if dumping {
                        // The dump never finished: clear the bookkeeping
                        // without claiming a sync that did not happen.
                        core.end_dump(epoch, false);
                    }
                    core.set_channel(epoch, Channel::Control, ChannelState::Lost);
                    return;
                }
            },
        }
    }
}
