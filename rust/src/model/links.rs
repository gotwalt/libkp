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

use std::net::Ipv4Addr;
use std::sync::Arc;
use std::time::Duration;

use tokio::sync::mpsc;
use tokio::task::JoinHandle;

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
/// handle completes when the socket ends.
pub(crate) fn spawn_stream(
    shared: Arc<Shared>,
    epoch: u64,
    session: Session,
    tail: Vec<u8>,
) -> (mpsc::Sender<Vec<u8>>, JoinHandle<()>) {
    let (tx, rx) = mpsc::channel(COMMAND_QUEUE);
    let task = tokio::spawn(run_stream(shared, epoch, session, tail, rx));
    (tx, task)
}

/// Own the stream socket: fold every read chunk, write every command.
async fn run_stream(
    shared: Arc<Shared>,
    epoch: u64,
    mut session: Session,
    tail: Vec<u8>,
    mut commands: mpsc::Receiver<Vec<u8>>,
) {
    let mut unframer = Unframer::new();
    // Decode anything that rode in on the handshake acceptance tail.
    shared.core.apply_messages(epoch, &unframer.push(&tail));
    loop {
        tokio::select! {
            read = session.read_once(READ_IDLE, READ_MAX) => match read {
                Ok(chunk) => shared.core.apply_messages(epoch, &unframer.push(&chunk)),
                // EOF or a read error: the socket is gone, and so is this life.
                Err(_) => return,
            },
            cmd = commands.recv() => match cmd {
                Some(bytes) => {
                    if session.write_all(&midi3::frame(&bytes)).await.is_err() {
                        return;
                    }
                }
                // The life was torn down under us: nothing more will be asked.
                None => return,
            },
        }
    }
}

// ---- the control link ------------------------------------------------------

/// One attempt at the control link, start to finish: open it (dial,
/// handshake, preamble, dump trigger), report it open, fold what it sends
/// until the socket ends, report it lost.
///
/// The dump phase starts with the trigger and ends when the run at
/// [`generated::DUMP_END_ADDRESS`] — the item that always closes a dump — has
/// been folded, or [`generated::DUMP_SETTLE_MS`] after the trigger if it never
/// comes. Items up to and including the marker fold as [`Phase::Dump`], so a
/// value pushed live meanwhile keeps its authority; everything after folds as
/// [`Phase::Live`].
///
/// `Err` is the open failing; the link ending after it opened is `Ok`, and so
/// is finding another attempt already under way (the claim fails), since what
/// was asked for is happening.
pub(crate) async fn run_control(shared: Arc<Shared>, epoch: u64) -> Result<(), SessionError> {
    let core = &shared.core;
    // Claiming the link is what raises `Connecting`; it happens here, in the
    // task, so that a subscriber who joined right after `connect` returned
    // sees the whole `Connecting → Open` sequence.
    if !core.claim_control(epoch) {
        return Ok(());
    }
    shared.note_control_attempt();
    let mut link = match ControlLink::open(shared.ip, shared.opts.port, READ_IDLE).await {
        Ok(link) => link,
        Err(e) => {
            core.set_channel(epoch, Channel::Control, ChannelState::Unavailable);
            return Err(e);
        }
    };
    core.begin_dump(epoch);
    core.set_channel(epoch, Channel::Control, ChannelState::Open);

    let settle = tokio::time::sleep(Duration::from_millis(generated::DUMP_SETTLE_MS));
    tokio::pin!(settle);
    let mut dumping = true;
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
                    } else if let Some(end) = cbor::dump_end_index(&items) {
                        // The marker closes the dump; whatever follows it in
                        // the same read is already live.
                        ended = true;
                        let mut updates = cbor::updates(&items[..=end], Phase::Dump);
                        updates.extend(cbor::updates(&items[end + 1..], Phase::Live));
                        updates
                    } else {
                        cbor::updates(&items, Phase::Dump)
                    };
                    core.apply_updates(epoch, &updates);
                    if ended {
                        dumping = false;
                        core.end_dump(epoch, true);
                    }
                }
                Err(_) => {
                    if dumping {
                        // The dump never finished: clear the bookkeeping
                        // without claiming a sync that did not happen.
                        core.end_dump(epoch, false);
                    }
                    core.set_channel(epoch, Channel::Control, ChannelState::Lost);
                    return Ok(());
                }
            },
        }
    }
}
