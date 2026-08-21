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

use std::net::{Ipv4Addr, SocketAddr, SocketAddrV4};
use std::time::Duration;

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::time::timeout;

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

/// 8 zero bytes the client writes to open the stream.
pub const SESSION_PREAMBLE: [u8; generated::SESSION_PREAMBLE_LEN] =
    [0u8; generated::SESSION_PREAMBLE_LEN];

/// An open TCP session with a Profiler.
pub struct Session {
    stream: TcpStream,
    peer: SocketAddr,
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
        Self::connect_timeout(ip, Duration::from_secs(generated::CONNECT_TIMEOUT_SECS)).await
    }

    /// Connect to `ip:5727` with an explicit connect timeout.
    pub async fn connect_timeout(ip: Ipv4Addr, dur: Duration) -> Result<Self, SessionError> {
        let peer = SocketAddr::V4(SocketAddrV4::new(ip, DISCOVERY_PORT));
        let stream = match timeout(dur, TcpStream::connect(peer)).await {
            Ok(Ok(s)) => s,
            Ok(Err(source)) => return Err(SessionError::Connect { addr: peer, source }),
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
        self.peer
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
}
