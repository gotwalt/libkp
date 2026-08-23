//! TCP session with a Profiler and the line-based protocol handshake.
//!
//! Established by observed experimentation against hardware.
//!
//! Sequence:
//! 1. TCP connect to the device on [`crate::protocol::DISCOVERY_PORT`] (5727).
//! 2. The **device sends first**: a CRLF-separated list of supported protocol
//!    identifiers, terminated by a line containing just `"."` (read up to 256
//!    bytes).
//! 3. Client writes the chosen protocol name followed by `"\r\n"`.
//! 4. Device replies with a line beginning `+` (accept) or `-` (reject).
//! 5. For the streaming session the client then writes an 8-byte zero preamble
//!    and the framed stream begins.
//!
//! # Connection spacing
//!
//! The device tolerates concurrent sessions but not connection *churn*: a
//! socket opened too soon after another one opened or closed to the same
//! device is not greeted, and enough of them stop the device accepting TCP at
//! all until it is power-cycled (`docs/11`). Rather than ask every caller to
//! space its own connections, this module keeps a process-wide
//! `ConnectionLedger`: [`Session::connect_to`] records the instant of every
//! successful open, [`Session::close`] and dropping a [`Session`] record every
//! close, and the next open to the same `(address, port)` sleeps until
//! [`CONNECTION_COOLDOWN`] has passed since the later of the two. Opens to
//! different peers never wait on each other, so a fake device on an ephemeral
//! port is unaffected.

use std::collections::HashMap;
use std::net::{Ipv4Addr, SocketAddr, SocketAddrV4};
use std::sync::{Arc, LazyLock, Mutex};
use std::time::Duration;

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::time::{Instant, timeout};

use crate::error::SessionError;
use crate::generated;
use crate::protocol::DISCOVERY_PORT;

/// Line terminator the client appends to the chosen protocol name.
pub const HANDSHAKE_TERMINATOR: &[u8] = generated::HANDSHAKE_TERMINATOR.as_bytes();

/// The protocol identifier that streams live MIDI3 data (meters, params, tuner).
/// The only offered protocol observed to push data unprompted.
pub const PROTOCOL_MIDI3_STREAM: &str = generated::PROTOCOL_MIDI3_STREAM;

/// Request/response protocol identifier: accepts the handshake but pushes nothing.
pub const PROTOCOL_REQUEST_RESPONSE: &str = generated::PROTOCOL_REQUEST_RESPONSE;

/// The device's native CBOR control channel — the state-dump snapshot route
/// ([`crate::cbor`]). Completes the same handshake and preamble as the MIDI3
/// stream, then speaks CBOR rather than MIDI3 frames.
pub const PROTOCOL_CBOR_CONTROL: &str = generated::PROTOCOL_CBOR_CONTROL;

/// 8 zero bytes the client writes to open the stream.
pub const SESSION_PREAMBLE: [u8; generated::SESSION_PREAMBLE_LEN] =
    [0u8; generated::SESSION_PREAMBLE_LEN];

/// Minimum quiet gap between any two connection events — open or close — to
/// the same device. The device refuses to greet, or resets, a session opened
/// too soon after a prior socket opened or closed, and repeated offences wedge
/// it (`docs/06`, `docs/11`). Every [`Session`] opened through this module
/// waits it out automatically via the `ConnectionLedger`, so an orchestrator
/// that opens more than one session (a CBOR
/// [`StateSnapshot::fetch`](crate::cbor::StateSnapshot::fetch) then a MIDI3
/// [`DeviceModel`](crate::model::DeviceModel)) needs no spacing of its own; the
/// constant is exported so callers can budget for the wait.
pub const CONNECTION_COOLDOWN: Duration = Duration::from_millis(generated::CONNECTION_COOLDOWN_MS);

/// What the ledger remembers about one peer: when a socket to it last opened
/// and when one last closed. Either is `None` until it has happened.
#[derive(Debug, Default, Clone, Copy)]
struct PeerTouch {
    last_open: Option<Instant>,
    last_close: Option<Instant>,
}

impl PeerTouch {
    /// The instant the cooldown counts from: the later of the two events.
    fn latest(&self) -> Option<Instant> {
        self.last_open.max(self.last_close)
    }
}

/// One peer's entry in the ledger.
struct PeerEntry {
    /// Serialises the wait-then-dial sequence, so two tasks racing to open the
    /// same peer cannot both observe a stale ledger and dial inside each
    /// other's cooldown. A tokio mutex because it is held across the sleep.
    turn: tokio::sync::Mutex<()>,
    touch: Mutex<PeerTouch>,
}

/// Process-wide record of the last open and the last close to every peer,
/// keyed by `(address, port)`. There is exactly one, `LEDGER`; it is the
/// mechanism behind [`CONNECTION_COOLDOWN`] and is never consulted directly by
/// callers.
///
/// The ledger is keyed by port as well as address on purpose: the hazard is
/// the device at `:5727`, and a per-address key would make every test against
/// a fake on an ephemeral port pay the cooldown for the fake before it.
struct ConnectionLedger {
    peers: Mutex<HashMap<SocketAddrV4, Arc<PeerEntry>>>,
}

impl ConnectionLedger {
    fn new() -> Self {
        ConnectionLedger {
            peers: Mutex::new(HashMap::new()),
        }
    }

    /// The entry for `peer`, created on first sight. Entries are never removed:
    /// the set of devices a process talks to is tiny, and a forgotten entry
    /// would forget the very close it must count from.
    fn entry(&self, peer: SocketAddrV4) -> Arc<PeerEntry> {
        let mut peers = self.peers.lock().unwrap_or_else(|e| e.into_inner());
        peers
            .entry(peer)
            .or_insert_with(|| {
                Arc::new(PeerEntry {
                    turn: tokio::sync::Mutex::new(()),
                    touch: Mutex::new(PeerTouch::default()),
                })
            })
            .clone()
    }

    /// The instant an open to `peer` may go ahead: [`CONNECTION_COOLDOWN`] after
    /// its latest recorded event, or `now` if there is none.
    fn ready_at(&self, peer: SocketAddrV4, cooldown: Duration) -> Instant {
        let entry = self.entry(peer);
        let touch = *entry.touch.lock().unwrap_or_else(|e| e.into_inner());
        touch
            .latest()
            .map(|t| t + cooldown)
            .unwrap_or_else(Instant::now)
    }

    fn note_open(&self, peer: SocketAddrV4) {
        let entry = self.entry(peer);
        entry
            .touch
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .last_open = Some(Instant::now());
    }

    fn note_close(&self, peer: SocketAddrV4) {
        let entry = self.entry(peer);
        entry
            .touch
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .last_close = Some(Instant::now());
    }
}

/// The one ledger every [`Session`] in the process reports to.
static LEDGER: LazyLock<ConnectionLedger> = LazyLock::new(ConnectionLedger::new);

/// An open TCP session with a Profiler.
///
/// Opening one waits out the [`CONNECTION_COOLDOWN`] since the last open or
/// close to the same peer; closing one — by [`close`](Self::close) or by
/// dropping it — starts the next cooldown.
pub struct Session {
    stream: TcpStream,
    peer: SocketAddrV4,
}

/// Result of the protocol-selection handshake.
#[derive(Debug, Clone)]
pub struct HandshakeOutcome {
    /// Raw greeting bytes the device sent on connect.
    pub greeting: Vec<u8>,
    /// Protocol names parsed from the greeting.
    pub offered: Vec<String>,
    /// The protocol name we selected and sent.
    pub selected: String,
    /// Raw device response to our selection (first byte `+`/`-`).
    pub response: Vec<u8>,
}

impl HandshakeOutcome {
    /// Stream bytes that arrived piggybacked after the `+<name>\r\n` ack line.
    /// The device often sends the first burst of session data in the same
    /// packet as the acceptance; feed this to the unframer before reading more.
    pub fn response_tail(&self) -> &[u8] {
        match self
            .response
            .windows(HANDSHAKE_TERMINATOR.len())
            .position(|w| w == HANDSHAKE_TERMINATOR)
        {
            Some(pos) => &self.response[pos + HANDSHAKE_TERMINATOR.len()..],
            None => &[],
        }
    }
}

impl Session {
    /// Connect to `ip:5727` (default 5 s timeout).
    pub async fn connect(ip: Ipv4Addr) -> Result<Self, SessionError> {
        Self::connect_to(ip, DISCOVERY_PORT).await
    }

    /// Connect to `ip:5727` with an explicit connect timeout.
    pub async fn connect_timeout(ip: Ipv4Addr, dur: Duration) -> Result<Self, SessionError> {
        Self::connect_to_timeout(ip, DISCOVERY_PORT, dur).await
    }

    /// Connect to `ip:port` (default 5 s timeout). The port exists for fakes and
    /// tests; a real device only ever listens on 5727, which
    /// [`connect`](Self::connect) fills in.
    pub async fn connect_to(ip: Ipv4Addr, port: u16) -> Result<Self, SessionError> {
        Self::connect_to_timeout(
            ip,
            port,
            Duration::from_secs(generated::CONNECT_TIMEOUT_SECS),
        )
        .await
    }

    /// Connect to `ip:port` with an explicit connect timeout. This is the one
    /// constructor; the others fill in a port or a timeout and call it.
    ///
    /// Before dialing, waits until [`CONNECTION_COOLDOWN`] has passed since the
    /// last open or close to the same peer as recorded by the process-wide
    /// ledger, and records the attempt as it dials — a refused dial counts too,
    /// so a retry loop against a device that has stopped listening is paced
    /// like any other reopen. The wait is not counted against `dur`, which only
    /// bounds the TCP connect itself.
    pub async fn connect_to_timeout(
        ip: Ipv4Addr,
        port: u16,
        dur: Duration,
    ) -> Result<Self, SessionError> {
        let peer = SocketAddrV4::new(ip, port);
        let entry = LEDGER.entry(peer);
        // Hold the peer's turn across the wait *and* the dial: a sibling that
        // wins the lock afterwards sees this open in the ledger and waits its
        // own full cooldown from it.
        let _turn = entry.turn.lock().await;
        tokio::time::sleep_until(LEDGER.ready_at(peer, CONNECTION_COOLDOWN)).await;
        // Stamp before the dial, not after: the device sees the SYN whether or
        // not it answers, and a refused attempt must pace the next one.
        LEDGER.note_open(peer);

        let addr = SocketAddr::V4(peer);
        let stream = match timeout(dur, TcpStream::connect(addr)).await {
            Ok(Ok(s)) => s,
            Ok(Err(source)) => return Err(SessionError::Connect { addr, source }),
            Err(_) => {
                return Err(SessionError::Timeout {
                    phase: "connect",
                    ms: dur.as_millis() as u64,
                });
            }
        };
        // The device is latency-sensitive for live control; disable Nagle.
        let _ = stream.set_nodelay(true);
        Ok(Self { stream, peer })
    }

    /// The connected device address.
    pub fn peer(&self) -> SocketAddr {
        SocketAddr::V4(self.peer)
    }

    /// Close the session: shut the socket down, then record the close in the
    /// ledger so the next open to this peer waits out [`CONNECTION_COOLDOWN`].
    ///
    /// A shutdown that fails — typically because the device already hung up —
    /// is deliberately not surfaced: the socket is gone either way, and from the
    /// ledger's point of view a close is a close. Dropping a `Session` without
    /// calling this records the close too; `close` only adds the orderly FIN.
    pub async fn close(mut self) {
        let _ = self.stream.shutdown().await;
        // `self` drops here, and `Drop` stamps the ledger *after* the shutdown.
    }

    /// Read whatever the device sends until an `idle` gap with no data (or
    /// `max` bytes, or EOF). Used both to capture the greeting and to drain the
    /// live stream. Returns the bytes collected (possibly empty).
    pub async fn read_available(
        &mut self,
        idle: Duration,
        max: usize,
    ) -> Result<Vec<u8>, SessionError> {
        let mut buf = Vec::new();
        let mut chunk = [0u8; 4096];
        loop {
            match timeout(idle, self.stream.read(&mut chunk)).await {
                // EOF: report a closed connection unless we already collected
                // data this call (then hand that back; the next call errors).
                Ok(Ok(0)) if buf.is_empty() => return Err(SessionError::Closed),
                Ok(Ok(0)) => break,
                Ok(Ok(n)) => {
                    buf.extend_from_slice(&chunk[..n]);
                    if buf.len() >= max {
                        break;
                    }
                }
                Ok(Err(source)) => {
                    return Err(SessionError::Io {
                        phase: "read",
                        source,
                    });
                }
                Err(_) => break, // idle timeout — treat as "nothing more for now"
            }
        }
        Ok(buf)
    }

    /// Do a **single** read with a timeout, returning the bytes read.
    ///
    /// Unlike [`read_available`](Self::read_available), this returns as soon as
    /// any data arrives (or the timeout elapses, yielding an empty vec). Use it
    /// to drive a render loop that must react to every packet even while the
    /// device is streaming continuously.
    pub async fn read_once(&mut self, wait: Duration, max: usize) -> Result<Vec<u8>, SessionError> {
        let mut chunk = vec![0u8; max];
        match timeout(wait, self.stream.read(&mut chunk)).await {
            Ok(Ok(0)) => Err(SessionError::Closed), // EOF, unlike a quiet tick
            Ok(Ok(n)) => {
                chunk.truncate(n);
                Ok(chunk)
            }
            Ok(Err(source)) => Err(SessionError::Io {
                phase: "read",
                source,
            }),
            Err(_) => Ok(Vec::new()),
        }
    }

    /// Write all `data` to the device.
    pub async fn write_all(&mut self, data: &[u8]) -> Result<(), SessionError> {
        self.stream
            .write_all(data)
            .await
            .map_err(|source| SessionError::Io {
                phase: "write",
                source,
            })
    }

    /// Send `name` + `"\r\n"` and read the device's response line.
    ///
    /// Returns the raw response. `Err(ProtocolRejected)` if it begins with `-`.
    pub async fn select_protocol(
        &mut self,
        name: &str,
        resp_idle: Duration,
    ) -> Result<Vec<u8>, SessionError> {
        let mut msg = name.as_bytes().to_vec();
        msg.extend_from_slice(HANDSHAKE_TERMINATOR);
        self.write_all(&msg).await?;
        let resp = self.read_available(resp_idle, 256).await?;
        match resp.first() {
            Some(b) if *b == generated::HANDSHAKE_REJECT_PREFIX.as_bytes()[0] => {
                Err(SessionError::ProtocolRejected {
                    name: name.to_string(),
                    detail: Some(String::from_utf8_lossy(&resp).trim().to_string()),
                })
            }
            _ => Ok(resp), // '+' accept, or unknown -> hand back for inspection
        }
    }

    /// Full handshake: read the greeting, pick the first `preferred` protocol
    /// that the device offers (falling back to its first offered), select it.
    pub async fn handshake(
        &mut self,
        preferred: &[&str],
        idle: Duration,
    ) -> Result<HandshakeOutcome, SessionError> {
        let greeting = self.read_available(idle, 256).await?;
        let offered = parse_protocol_list(&greeting);

        let selected = preferred
            .iter()
            .find(|p| offered.iter().any(|o| o == *p))
            .map(|p| p.to_string())
            .or_else(|| offered.first().cloned())
            .ok_or(SessionError::Timeout {
                phase: "greeting",
                ms: idle.as_millis() as u64,
            })?;

        let response = self.select_protocol(&selected, idle).await?;
        Ok(HandshakeOutcome {
            greeting,
            offered,
            selected,
            response,
        })
    }

    /// Write the 8-byte zero preamble that opens the stream.
    pub async fn write_session_preamble(&mut self) -> Result<(), SessionError> {
        self.write_all(&SESSION_PREAMBLE).await
    }
}

impl Drop for Session {
    /// Every session's end is a close as far as the device is concerned —
    /// whether by [`close`](Session::close), an ingest task finishing, or an
    /// error unwinding a constructor — so the ledger is stamped here rather
    /// than only on the explicit path. `Drop` is synchronous, so this only
    /// records the instant; the socket itself closes with the `TcpStream`.
    fn drop(&mut self) {
        LEDGER.note_close(self.peer);
    }
}

/// Parse the greeting's offered protocol list.
///
/// The device sends one protocol identifier per CRLF-terminated line, ending
/// with a line containing just `"."`:
/// ```text
/// {77DB6B28-...}\r\n{369F50E7-...}\r\n ... .\r\n
/// ```
pub fn parse_protocol_list(bytes: &[u8]) -> Vec<String> {
    let text = String::from_utf8_lossy(bytes);
    let mut out = Vec::new();
    for line in text.split(['\r', '\n']) {
        let t = line.trim();
        if t.is_empty() {
            continue;
        }
        if t == generated::HANDSHAKE_LIST_END {
            break; // end-of-list marker
        }
        out.push(t.to_string());
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_crlf_guid_list_terminated_by_dot() {
        let greeting = b"{AAA}\r\n{BBB}\r\n.\r\n";
        assert_eq!(parse_protocol_list(greeting), vec!["{AAA}", "{BBB}"]);
        assert_eq!(parse_protocol_list(b""), Vec::<String>::new());
        assert_eq!(parse_protocol_list(b".\r\n"), Vec::<String>::new());
    }

    #[test]
    fn response_tail_is_what_follows_the_ack_line() {
        let outcome = HandshakeOutcome {
            greeting: Vec::new(),
            offered: Vec::new(),
            selected: String::new(),
            response: b"+{AAA}\r\n\x14\xf0\x00\x20".to_vec(),
        };
        assert_eq!(outcome.response_tail(), &[0x14, 0xf0, 0x00, 0x20]);

        let no_line = HandshakeOutcome {
            greeting: Vec::new(),
            offered: Vec::new(),
            selected: String::new(),
            response: b"+{AAA}".to_vec(),
        };
        assert!(no_line.response_tail().is_empty());
    }

    // ---- ledger arithmetic, on a private ledger with stamps set by hand ----

    fn peer(port: u16) -> SocketAddrV4 {
        SocketAddrV4::new(Ipv4Addr::LOCALHOST, port)
    }

    fn stamp(ledger: &ConnectionLedger, peer: SocketAddrV4, touch: PeerTouch) {
        *ledger.entry(peer).touch.lock().unwrap() = touch;
    }

    #[test]
    fn unseen_peer_is_ready_now() {
        let ledger = ConnectionLedger::new();
        let before = Instant::now();
        let ready = ledger.ready_at(peer(1), CONNECTION_COOLDOWN);
        assert!(before <= ready && ready <= Instant::now());
    }

    #[test]
    fn cooldown_counts_from_the_later_of_open_and_close() {
        let ledger = ConnectionLedger::new();
        let t0 = Instant::now();
        let later = t0 + Duration::from_millis(300);

        stamp(
            &ledger,
            peer(1),
            PeerTouch {
                last_open: Some(t0),
                last_close: None,
            },
        );
        assert_eq!(
            ledger.ready_at(peer(1), CONNECTION_COOLDOWN),
            t0 + CONNECTION_COOLDOWN
        );

        // A close after the open moves the deadline out ...
        stamp(
            &ledger,
            peer(1),
            PeerTouch {
                last_open: Some(t0),
                last_close: Some(later),
            },
        );
        assert_eq!(
            ledger.ready_at(peer(1), CONNECTION_COOLDOWN),
            later + CONNECTION_COOLDOWN
        );

        // ... and an open after the close does too; the earlier close never
        // pulls it back.
        stamp(
            &ledger,
            peer(1),
            PeerTouch {
                last_open: Some(later),
                last_close: Some(t0),
            },
        );
        assert_eq!(
            ledger.ready_at(peer(1), CONNECTION_COOLDOWN),
            later + CONNECTION_COOLDOWN
        );
    }

    #[test]
    fn note_open_and_note_close_stamp_now() {
        let ledger = ConnectionLedger::new();
        let before = Instant::now();
        ledger.note_open(peer(1));
        ledger.note_close(peer(1));
        let touch = *ledger.entry(peer(1)).touch.lock().unwrap();
        let after = Instant::now();
        for t in [touch.last_open, touch.last_close] {
            let t = t.expect("stamped");
            assert!(before <= t && t <= after);
        }
    }

    #[test]
    fn peers_are_keyed_by_port_as_well_as_address() {
        let ledger = ConnectionLedger::new();
        ledger.note_close(peer(1));
        let before = Instant::now();
        let ready = ledger.ready_at(peer(2), CONNECTION_COOLDOWN);
        assert!(before <= ready && ready <= Instant::now());
    }

    // ---- the ledger wired into Session, against real sockets ---------------

    /// A listener that accepts every connection and parks it, which is all
    /// `connect_to` needs: the handshake is a separate call.
    async fn fake_listener() -> u16 {
        let listener = tokio::net::TcpListener::bind((Ipv4Addr::LOCALHOST, 0))
            .await
            .unwrap();
        let port = listener.local_addr().unwrap().port();
        tokio::spawn(async move {
            let mut held = Vec::new();
            while let Ok((stream, _)) = listener.accept().await {
                held.push(stream);
            }
        });
        port
    }

    #[tokio::test]
    async fn reopening_a_closed_peer_waits_out_the_cooldown() {
        let port = fake_listener().await;
        let session = Session::connect_to(Ipv4Addr::LOCALHOST, port)
            .await
            .unwrap();
        assert_eq!(session.peer(), SocketAddr::V4(peer(port)));

        // Taken before the close: the ledger stamps inside `close`, and the
        // next open must wait a full cooldown from that stamp, so it waits at
        // least as long from any instant before it.
        let before_close = Instant::now();
        session.close().await;
        let again = Session::connect_to(Ipv4Addr::LOCALHOST, port)
            .await
            .unwrap();
        assert!(before_close.elapsed() >= CONNECTION_COOLDOWN);
        drop(again);
    }

    #[tokio::test]
    async fn dropping_a_session_records_the_close_too() {
        let port = fake_listener().await;
        let session = Session::connect_to(Ipv4Addr::LOCALHOST, port)
            .await
            .unwrap();

        let before_drop = Instant::now();
        drop(session);
        let again = Session::connect_to(Ipv4Addr::LOCALHOST, port)
            .await
            .unwrap();
        assert!(before_drop.elapsed() >= CONNECTION_COOLDOWN);
        drop(again);
    }

    #[tokio::test]
    async fn opens_to_different_peers_do_not_wait_on_each_other() {
        let a = fake_listener().await;
        let b = fake_listener().await;
        let first = Session::connect_to(Ipv4Addr::LOCALHOST, a).await.unwrap();

        let started = Instant::now();
        let second = Session::connect_to(Ipv4Addr::LOCALHOST, b).await.unwrap();
        assert!(started.elapsed() < CONNECTION_COOLDOWN / 2);
        drop((first, second));
    }

    #[tokio::test]
    async fn a_failed_dial_still_paces_the_retry() {
        // The device saw the attempt whether or not it answered, so a retry
        // straight after a refused dial waits out the cooldown like any reopen.
        let listener = tokio::net::TcpListener::bind((Ipv4Addr::LOCALHOST, 0))
            .await
            .unwrap();
        let port = listener.local_addr().unwrap().port();
        drop(listener);

        let err = Session::connect_to_timeout(Ipv4Addr::LOCALHOST, port, Duration::from_secs(1))
            .await
            .err()
            .expect("nothing listens there");
        assert!(matches!(err, SessionError::Connect { .. }));
        assert!(LEDGER.ready_at(peer(port), CONNECTION_COOLDOWN) > Instant::now());
    }
}
