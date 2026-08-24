//! The request lane: request/reply over the stream, paced.
//!
//! A `request_*` call enqueues the SysEx request, registers what it is waiting
//! for with the core (keyed by flat address, or by page/number/value for a
//! rendered string), and awaits the answer or the timeout. The core settles
//! the entry when a matching value folds — a reply, or an unsolicited push at
//! the same address, which is equally current.
//!
//! The pacing is the device's, measured: every request type answers within
//! 50 ms even with the whole connect burst in flight, so a request unanswered
//! for [`generated::REQUEST_TIMEOUT_MS`] is not coming and is dropped, never
//! retried. At most [`generated::MAX_IN_FLIGHT_REQUESTS`] are on the wire at
//! once; further ones wait their turn here rather than being refused.

use std::sync::Arc;
use std::time::Duration;

use tokio::sync::oneshot;
use tokio::sync::{OwnedSemaphorePermit, Semaphore};
use tokio::task::JoinSet;
use tokio::time::timeout;

use super::core::{PendingKey, Reply};
use super::supervisor::Shared;
use super::{DEVICE, PRODUCT, RequestError};
use crate::generated::{self, Kind, Route, STATE_ROUTES, Wire};
use crate::nrpn::{
    request_extended_param, request_extended_string, request_single, request_string,
};
use crate::routes::route;
use crate::state::ChannelState;

/// The extended-address boundary: below it an address is a `page * 128 +
/// number` pair the short request forms name; at or above it only the `$46` /
/// `$47` extended forms can ask.
const EXTENDED_ADDRESS_BASE: u32 = 128 * 128;

/// The lane's one piece of state: how many requests may be on the wire. An
/// `Arc` so a permit can be owned and carried into the task that waits out one
/// request's reply.
pub(crate) struct RequestLane {
    permits: Arc<Semaphore>,
}

impl RequestLane {
    pub(crate) fn new() -> Self {
        RequestLane {
            permits: Arc::new(Semaphore::new(generated::MAX_IN_FLIGHT_REQUESTS)),
        }
    }
}

/// A request that has been reserved a slot on the wire, registered, and sent:
/// what is left is to wait out the reply or the timeout, which
/// [`finish`] does. Splitting the two lets [`refresh`] send its rows in table
/// order — the send is the ordered part — and then await them together.
struct Pending {
    permit: OwnedSemaphorePermit,
    id: u64,
    address: u32,
    reply: oneshot::Receiver<Reply>,
}

/// Reserve a wire slot for one request, register it, and send it. Ordered: the
/// caller awaits this in turn, so the enqueue order is the call order. Refuses
/// what the stream cannot answer before taking a slot.
async fn start(shared: &Shared, key: PendingKey, bytes: Vec<u8>) -> Result<Pending, RequestError> {
    let address = key.address();
    // A row the routing table gives to the control link alone draws no reply
    // on the stream (the morph position: `$41`/`$7C` there are met with
    // silence), so asking would only wait out the timeout. This covers a
    // rendered string too: a `request_render` whose page/number is the morph is
    // as unreadable on the stream as a `request_param` for it.
    let readable = route(address).is_none_or(|row| row.wire != Wire::Control);
    if !readable {
        return Err(RequestError::Unreadable);
    }
    if shared.core.stream_state() != ChannelState::Open {
        return Err(RequestError::Disconnected);
    }
    let permit = shared
        .lane
        .permits
        .clone()
        .acquire_owned()
        .await
        .map_err(|_| RequestError::Disconnected)?;
    // Register before sending: the reply cannot beat the entry.
    let (id, reply) = shared.core.register(key);
    if shared.enqueue(bytes).await.is_err() {
        shared.core.unregister(id);
        return Err(RequestError::Disconnected);
    }
    Ok(Pending {
        permit,
        id,
        address,
        reply,
    })
}

/// Wait out one reserved request's reply or its timeout, freeing its wire slot
/// when it is done.
async fn finish(shared: &Shared, pending: Pending) -> Result<Reply, RequestError> {
    let _permit = pending.permit;
    match timeout(
        Duration::from_millis(generated::REQUEST_TIMEOUT_MS),
        pending.reply,
    )
    .await
    {
        Ok(Ok(reply)) => Ok(reply),
        // The sender was dropped: the stream ended and the core failed every
        // pending entry.
        Ok(Err(_)) => Err(RequestError::Disconnected),
        Err(_) => {
            shared.core.unregister(pending.id);
            shared.core.request_timed_out(pending.address);
            Err(RequestError::Timeout)
        }
    }
}

/// One request: refuse what the stream cannot answer, wait for a slot on the
/// wire, register, send, and await the value or the timeout.
pub(crate) async fn request(
    shared: &Shared,
    key: PendingKey,
    bytes: Vec<u8>,
) -> Result<Reply, RequestError> {
    let pending = start(shared, key, bytes).await?;
    finish(shared, pending).await
}

/// Ask for every `request = true` row of the routing table that `rows`
/// selects, through the lane, and wait for the lot. The rows are *sent* in
/// [`STATE_ROUTES`] order — the table is address-sorted, so the wire order is
/// the table order in every language — by reserving and enqueuing each in turn
/// before the replies are awaited together; the semaphore still caps how many
/// are on the wire at once. `Ok` when every one was answered; otherwise the
/// worst thing that happened to any of them — a lost stream outranks a timeout
/// — while the others still landed.
pub(crate) async fn refresh(
    shared: &Arc<Shared>,
    rows: impl Fn(&Route) -> bool,
) -> Result<(), RequestError> {
    let mut burst = JoinSet::new();
    let mut worst: Option<RequestError> = None;
    for row in STATE_ROUTES.iter().filter(|r| r.request && rows(r)) {
        let (key, bytes) = request_for(row);
        // Awaited in turn: the enqueue order is the table order. Acquiring the
        // slot here blocks the loop once the wire is full, which is the pacing.
        match start(shared, key, bytes).await {
            Ok(pending) => {
                let shared = shared.clone();
                burst.spawn(async move { finish(&shared, pending).await.map(|_| ()) });
            }
            Err(e) => worst = worse(worst, e),
        }
    }
    while let Some(joined) = burst.join_next().await {
        if let Err(e) = joined.unwrap_or(Err(RequestError::Disconnected)) {
            worst = worse(worst, e);
        }
    }
    worst.map_or(Ok(()), Err)
}

/// The worse of a running worst-error and a new one, by [`severity`].
fn worse(current: Option<RequestError>, e: RequestError) -> Option<RequestError> {
    Some(match current {
        Some(w) if severity(w) >= severity(e) => w,
        _ => e,
    })
}

/// The order in which a burst's failures are reported.
fn severity(e: RequestError) -> u8 {
    match e {
        RequestError::Unreadable => 0,
        RequestError::Timeout => 1,
        RequestError::Disconnected => 2,
    }
}

/// The request that reads one row, and what its reply looks like: a string
/// row is asked with `$43` (or `$47` beyond the page range), a numeric one
/// with `$41` (or `$46`).
fn request_for(row: &Route) -> (PendingKey, Vec<u8>) {
    let address = row.address;
    let page = (address / 128) as u8;
    let number = (address % 128) as u8;
    let text = row.kind == Kind::Text;
    let short = address < EXTENDED_ADDRESS_BASE;
    match (text, short) {
        (true, true) => (
            PendingKey::Text(address),
            request_string(PRODUCT, DEVICE, page, number),
        ),
        (true, false) => (
            PendingKey::Text(address),
            request_extended_string(PRODUCT, DEVICE, address),
        ),
        (false, true) => (
            PendingKey::Num(address),
            request_single(PRODUCT, DEVICE, page, number),
        ),
        (false, false) => (
            PendingKey::Num(address),
            request_extended_param(PRODUCT, DEVICE, address),
        ),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::nrpn::{
        FUNCTION_REQUEST_EXT_PARAM, FUNCTION_REQUEST_EXT_STRING, FUNCTION_REQUEST_SINGLE,
        FUNCTION_REQUEST_STRING,
    };

    /// The table's `request = true` rows are the 46-request connect burst:
    /// six string tags, a Type and On/Off per effect slot, seven numerics,
    /// fifteen bank-preview strings, and the two halves of the position.
    #[test]
    fn the_burst_is_forty_six_requests() {
        let rows: Vec<&Route> = STATE_ROUTES.iter().filter(|r| r.request).collect();
        assert_eq!(rows.len(), 46);
        let mut functions = [0usize; 4];
        for row in rows {
            let (_, bytes) = request_for(row);
            let slot = match bytes[6] {
                FUNCTION_REQUEST_STRING => 0,
                FUNCTION_REQUEST_SINGLE => 1,
                FUNCTION_REQUEST_EXT_STRING => 2,
                FUNCTION_REQUEST_EXT_PARAM => 3,
                other => panic!("unexpected request function {other:#x}"),
            };
            functions[slot] += 1;
        }
        assert_eq!(functions, [6, 16 + 7, 15, 2]);
    }
}
