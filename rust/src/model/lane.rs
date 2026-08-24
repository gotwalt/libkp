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

use tokio::sync::Semaphore;
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

/// The lane's one piece of state: how many requests may be on the wire.
pub(crate) struct RequestLane {
    permits: Semaphore,
}

impl RequestLane {
    pub(crate) fn new() -> Self {
        RequestLane {
            permits: Semaphore::new(generated::MAX_IN_FLIGHT_REQUESTS),
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
    let address = key.address();
    // A row the routing table gives to the control link alone draws no reply
    // on the stream (the morph position: `$41` there is met with silence), so
    // asking would only wait out the timeout. A rendered string is not a row.
    let readable = matches!(key, PendingKey::Render { .. })
        || route(address).is_none_or(|row| row.wire != Wire::Control);
    if !readable {
        return Err(RequestError::Unreadable);
    }
    if shared.core.stream_state() != ChannelState::Open {
        return Err(RequestError::Disconnected);
    }
    let _permit = shared
        .lane
        .permits
        .acquire()
        .await
        .map_err(|_| RequestError::Disconnected)?;
    // Register before sending: the reply cannot beat the entry.
    let (id, reply) = shared.core.register(key);
    if shared.enqueue(bytes).await.is_err() {
        shared.core.unregister(id);
        return Err(RequestError::Disconnected);
    }
    match timeout(Duration::from_millis(generated::REQUEST_TIMEOUT_MS), reply).await {
        Ok(Ok(reply)) => Ok(reply),
        // The sender was dropped: the stream ended and the core failed every
        // pending entry.
        Ok(Err(_)) => Err(RequestError::Disconnected),
        Err(_) => {
            shared.core.unregister(id);
            shared.core.request_timed_out(address);
            Err(RequestError::Timeout)
        }
    }
}

/// Ask for every `request = true` row of the routing table that `rows`
/// selects, all at once through the lane, and wait for the lot. `Ok` when
/// every one was answered; otherwise the worst thing that happened to any of
/// them — a lost stream outranks a timeout — while the others still landed.
pub(crate) async fn refresh(
    shared: &Arc<Shared>,
    rows: impl Fn(&Route) -> bool,
) -> Result<(), RequestError> {
    let mut burst = JoinSet::new();
    for row in STATE_ROUTES.iter().filter(|r| r.request && rows(r)) {
        let (key, bytes) = request_for(row);
        let shared = shared.clone();
        burst.spawn(async move { request(&shared, key, bytes).await.map(|_| ()) });
    }
    let mut worst: Option<RequestError> = None;
    while let Some(joined) = burst.join_next().await {
        if let Err(e) = joined.unwrap_or(Err(RequestError::Disconnected)) {
            worst = Some(match worst {
                Some(w) if severity(w) >= severity(e) => w,
                _ => e,
            });
        }
    }
    worst.map_or(Ok(()), Err)
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
