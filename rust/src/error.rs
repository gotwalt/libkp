//! Error types for discovery, sessions, and payload parsing.

use std::net::SocketAddr;

/// Errors that can arise while discovering Kemper Profiler devices.
#[derive(Debug, thiserror::Error)]
pub enum DiscoverError {
    #[error("failed to create UDP socket: {0}")]
    SocketCreate(#[source] std::io::Error),

    #[error("failed to configure socket option {option}: {source}")]
    SocketOption {
        option: &'static str,
        #[source]
        source: std::io::Error,
    },

    #[error("failed to bind discovery socket to {addr}: {source}")]
    Bind {
        addr: SocketAddr,
        #[source]
        source: std::io::Error,
    },

    #[error("failed to enumerate local network interfaces: {0}")]
    Interfaces(#[source] std::io::Error),

    #[error("failed to send poll to {addr}: {source}")]
    Send {
        addr: SocketAddr,
        #[source]
        source: std::io::Error,
    },

    #[error("failed while receiving replies: {0}")]
    Recv(#[source] std::io::Error),
}

/// Errors from the TCP session / handshake.
#[derive(Debug, thiserror::Error)]
pub enum SessionError {
    #[error("failed to connect to {addr}: {source}")]
    Connect {
        addr: SocketAddr,
        #[source]
        source: std::io::Error,
    },

    #[error("i/o error during {phase}: {source}")]
    Io {
        phase: &'static str,
        #[source]
        source: std::io::Error,
    },

    #[error("timed out waiting for {phase} after {ms} ms")]
    Timeout { phase: &'static str, ms: u64 },

    #[error("connection closed by device")]
    Closed,

    #[error("device rejected protocol \"{name}\"{}", .detail.as_ref().map(|d| format!(": {d}")).unwrap_or_default())]
    ProtocolRejected {
        name: String,
        detail: Option<String>,
    },
}

/// A malformed or truncated tag-stream payload.
#[derive(Debug, thiserror::Error)]
pub enum ParseError {
    #[error("payload too short: need at least {need} bytes, got {got}")]
    TooShort { need: usize, got: usize },

    #[error("field at offset {offset} claims length {len} but only {remaining} bytes remain")]
    FieldOverrun {
        offset: usize,
        len: usize,
        remaining: usize,
    },
}
