//! Device discovery ("DSCV") wire encoding.
//!
//! Discovery happens over UDP on [`DISCOVERY_PORT`]. The client broadcasts a
//! fixed poll packet; Profilers on the LAN reply on the same port. The packet
//! shapes below were established by observed experimentation against hardware.
//!
//! ## Wire format — the tag stream
//!
//! A payload is an optional 4-byte ASCII header followed by a series of
//! length-prefixed fields. Each field is `[len: u8][content: len-1 bytes]`,
//! where **`len` is inclusive of the length byte itself** (so an empty field is
//! `0x00`, and content length is `len - 1`). A `0x00` byte terminates the stream.
//!
//! The 34-byte poll request is:
//! ```text
//! "DSCV"  0x16 "MAC#00:00:00:00:00:00"  0x07 "POLL:)"  0x00
//! ```

use crate::error::ParseError;
use crate::generated;

/// UDP port used for both the discovery broadcast and the TCP session (0x165f).
pub const DISCOVERY_PORT: u16 = generated::PORT;

/// 4-byte header that opens a discovery poll request.
pub const DSCV_HEADER: &[u8; 4] = b"DSCV";

/// Build the discovery poll request packet.
///
/// `mac` is the client's own MAC address string. The device appears to track it
/// but replies regardless, so the all-zero placeholder
/// ([`generated::POLL_PLACEHOLDER_MAC`]) works fine for probing.
pub fn build_poll_request(mac: &str) -> Vec<u8> {
    let mut out = Vec::with_capacity(40);
    out.extend_from_slice(generated::DISCOVERY_HEADER.as_bytes());
    push_field(
        &mut out,
        format!("{}{mac}", generated::POLL_MAC_PREFIX).as_bytes(),
    );
    push_field(&mut out, generated::POLL_PAYLOAD.as_bytes());
    out.push(0x00); // stream terminator
    out
}

/// Append one length-prefixed field. The `len` byte is inclusive of itself.
fn push_field(out: &mut Vec<u8>, content: &[u8]) {
    debug_assert!(content.len() < u8::MAX as usize);
    out.push(content.len() as u8 + 1);
    out.extend_from_slice(content);
}

/// A decoded tag-stream payload.
#[derive(Debug, Clone)]
pub struct TagStream {
    /// Leading 4-byte ASCII header, if the payload looked like it had one.
    pub header: Option<[u8; 4]>,
    /// Length-prefixed fields, content only (length byte stripped).
    pub fields: Vec<Vec<u8>>,
}

impl TagStream {
    /// Best-effort parse of a received payload.
    ///
    /// If the first four bytes are printable ASCII and the fifth byte is a
    /// plausible field length, they are taken as a header; otherwise fields are
    /// read from offset 0. This is a heuristic — the raw bytes are always the
    /// source of truth.
    pub fn parse(buf: &[u8]) -> Result<Self, ParseError> {
        if buf.is_empty() {
            return Err(ParseError::TooShort { need: 1, got: 0 });
        }

        let (header, mut off) = detect_header(buf);
        let mut fields = Vec::new();

        while off < buf.len() {
            let len = buf[off] as usize;
            if len == 0 {
                break; // terminator / empty field
            }
            let content_len = len - 1;
            let start = off + 1;
            let end = start + content_len;
            if end > buf.len() {
                return Err(ParseError::FieldOverrun {
                    offset: off,
                    len,
                    remaining: buf.len() - off - 1,
                });
            }
            fields.push(buf[start..end].to_vec());
            off = end;
        }

        Ok(TagStream { header, fields })
    }

    /// Split each field into its 4-char ASCII key and value bytes.
    ///
    /// Discovery-reply fields are `[4-char key][value]` (e.g. `NAME`, `SER#`).
    /// Fields whose first four bytes are not printable are skipped.
    pub fn key_values(&self) -> Vec<(String, Vec<u8>)> {
        self.fields
            .iter()
            .filter_map(|f| {
                if f.len() >= 4 && f[..4].iter().all(|b| b.is_ascii_graphic()) {
                    Some((
                        String::from_utf8_lossy(&f[..4]).into_owned(),
                        f[4..].to_vec(),
                    ))
                } else {
                    None
                }
            })
            .collect()
    }
}

/// Decide whether `buf` opens with a 4-byte ASCII header.
fn detect_header(buf: &[u8]) -> (Option<[u8; 4]>, usize) {
    if buf.len() >= 5 && buf[..4].iter().all(|b| b.is_ascii_graphic()) && {
        // fifth byte should be a plausible inclusive field length
        let len = buf[4] as usize;
        len == 0 || 4 + len <= buf.len()
    } {
        let mut h = [0u8; 4];
        h.copy_from_slice(&buf[..4]);
        (Some(h), 4)
    } else {
        (None, 0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn poll_request_is_34_bytes() {
        let expected: &[u8] = &[
            0x44, 0x53, 0x43, 0x56, // DSCV
            0x16, 0x4d, 0x41, 0x43, 0x23, 0x30, 0x30, 0x3a, 0x30, 0x30, 0x3a, 0x30, 0x30, 0x3a,
            0x30, 0x30, 0x3a, 0x30, 0x30, 0x3a, 0x30, 0x30, // len + "MAC#00:00:00:00:00:00"
            0x07, 0x50, 0x4f, 0x4c, 0x4c, 0x3a, 0x29, // len + "POLL:)"
            0x00,
        ];
        let built = build_poll_request(generated::POLL_PLACEHOLDER_MAC);
        assert_eq!(built, expected);
        assert_eq!(built.len(), 34);
    }

    #[test]
    fn roundtrip_parse_request() {
        let pkt = build_poll_request("00:00:00:00:00:00");
        let ts = TagStream::parse(&pkt).unwrap();
        assert_eq!(ts.header, Some(*DSCV_HEADER));
        assert_eq!(ts.fields.len(), 2);
        assert_eq!(ts.fields[0], b"MAC#00:00:00:00:00:00");
        assert_eq!(ts.fields[1], b"POLL:)");
    }
}
