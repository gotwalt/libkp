//! MIDI3 stream framing — the wrapper that carries MIDI over the TCP streaming
//! protocol ([`crate::session::PROTOCOL_MIDI3_STREAM`]).
//!
//! Established by observed experimentation. The stream is a sequence of 4-byte
//! frames: `[tag][b0][b1][b2]`.
//!
//! - `0x14` — continuation group: all 3 bytes are valid, more groups follow.
//! - `0x15` — final group, 1 valid byte  (b0), message ends.
//! - `0x16` — final group, 2 valid bytes (b0,b1), message ends.
//! - `0x17` — final group, 3 valid bytes (b0,b1,b2), message ends.
//!
//! Concatenating the valid bytes of the groups making up a message yields a raw
//! MIDI message (typically a Kemper SysEx `F0 00 20 33 … F7`).
//!
//! Example (a 19-byte SysEx):
//! ```text
//! 14 f0 00 20 | 14 33 02 00 | 14 06 00 00 | 14 00 06 20
//! 14 05 00 00 | 14 00 00 10 | 15 f7 00 00
//!   -> F0 00 20 33 02 00 06 00 00 00 06 20 05 00 00 00 00 10 F7
//! ```

use crate::generated;

/// Continuation frame tag: 3 valid bytes, message continues.
pub const TAG_CONT: u8 = generated::MIDI3_TAG_CONTINUATION;

/// A streaming de-framer. Feed it raw bytes; it yields complete MIDI messages.
#[derive(Debug, Default)]
pub struct Unframer {
    /// Leftover bytes that don't yet complete a 4-byte frame.
    partial: Vec<u8>,
    /// Bytes accumulated for the in-progress MIDI message.
    current: Vec<u8>,
}

impl Unframer {
    pub fn new() -> Self {
        Self::default()
    }

    /// Feed raw stream bytes; return any MIDI messages completed by this input.
    ///
    /// A frame whose tag is not `0x14..=0x17` is treated as a desync: the
    /// current partial message is discarded and that byte skipped to resync.
    pub fn push(&mut self, data: &[u8]) -> Vec<Vec<u8>> {
        self.partial.extend_from_slice(data);
        let mut out = Vec::new();

        while self.partial.len() >= 4 {
            let tag = self.partial[0];
            let payload = [self.partial[1], self.partial[2], self.partial[3]];

            let valid = match tag {
                generated::MIDI3_TAG_CONTINUATION => 3,
                generated::MIDI3_TAG_FINAL_1 => 1,
                generated::MIDI3_TAG_FINAL_2 => 2,
                generated::MIDI3_TAG_FINAL_3 => 3,
                _ => {
                    // Unknown tag — resync by dropping one byte.
                    self.partial.drain(0..1);
                    self.current.clear();
                    continue;
                }
            };

            self.partial.drain(0..4);
            self.current.extend_from_slice(&payload[..valid]);
            if tag != generated::MIDI3_TAG_CONTINUATION {
                out.push(std::mem::take(&mut self.current));
            }
        }
        out
    }

    /// Bytes buffered but not yet forming a complete message.
    pub fn pending(&self) -> usize {
        self.partial.len() + self.current.len()
    }
}

/// Frame a raw MIDI message into MIDI3 wire format (inverse of [`Unframer`]).
///
/// Splits into 3-byte groups: full non-final groups get `0x14`; the final group
/// gets `0x15`/`0x16`/`0x17` for 1/2/3 valid bytes (padded to 3).
pub fn frame(msg: &[u8]) -> Vec<u8> {
    let mut out = Vec::new();
    let n_groups = msg.len().div_ceil(3).max(1);
    for (i, chunk) in msg.chunks(3).enumerate() {
        let last = i + 1 == n_groups;
        let tag = if last {
            generated::MIDI3_TAG_CONTINUATION + chunk.len() as u8
        } else {
            generated::MIDI3_TAG_CONTINUATION
        };
        let mut group = [0u8; 3];
        group[..chunk.len()].copy_from_slice(chunk);
        out.push(tag);
        out.extend_from_slice(&group);
    }
    out
}

/// True if `msg` is a Kemper SysEx (`F0 00 20 33 … F7`). `00 20 33` is Kemper's
/// MIDI manufacturer ID.
pub fn is_kemper_sysex(msg: &[u8]) -> bool {
    msg.len() >= 5
        && msg[0] == 0xF0
        && msg[1..4] == generated::MANUFACTURER_ID
        && *msg.last().unwrap() == 0xF7
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unframes_a_multi_group_sysex() {
        let raw: &[u8] = &[
            0x14, 0xf0, 0x00, 0x20, 0x14, 0x33, 0x02, 0x00, 0x14, 0x06, 0x00, 0x00, 0x14, 0x00,
            0x06, 0x20, 0x14, 0x05, 0x00, 0x00, 0x14, 0x00, 0x00, 0x10, 0x15, 0xf7, 0x00, 0x00,
        ];
        let mut uf = Unframer::new();
        let msgs = uf.push(raw);
        assert_eq!(msgs.len(), 1);
        assert_eq!(
            msgs[0],
            vec![
                0xF0, 0x00, 0x20, 0x33, 0x02, 0x00, 0x06, 0x00, 0x00, 0x00, 0x06, 0x20, 0x05, 0x00,
                0x00, 0x00, 0x00, 0x10, 0xF7
            ]
        );
        assert!(is_kemper_sysex(&msgs[0]));
        assert_eq!(uf.pending(), 0);
    }

    #[test]
    fn frame_unframe_roundtrip() {
        for msg in [
            vec![
                0xF0u8, 0x00, 0x20, 0x33, 0x02, 0x7F, 0x7E, 0x00, 0x40, 0x02, 0x23, 0x0F, 0xF7,
            ],
            vec![0xF0, 0x00, 0x20, 0x33, 0xF7],       // len 5
            vec![0xF0, 0x00, 0x20, 0x33, 0x01, 0xF7], // len 6 (multiple of 3)
        ] {
            let framed = frame(&msg);
            assert_eq!(framed.len() % 4, 0);
            let mut uf = Unframer::new();
            assert_eq!(uf.push(&framed), vec![msg]);
        }
    }

    #[test]
    fn reassembles_across_chunk_boundaries() {
        let raw: &[u8] = &[
            0x14, 0xf0, 0x00, 0x20, 0x14, 0x33, 0x02, 0x00, 0x17, 0x01, 0x02, 0xf7,
        ];
        let mut uf = Unframer::new();
        assert!(uf.push(&raw[..5]).is_empty()); // split mid-stream
        let msgs = uf.push(&raw[5..]);
        assert_eq!(
            msgs,
            vec![vec![0xF0, 0x00, 0x20, 0x33, 0x02, 0x00, 0x01, 0x02, 0xF7]]
        );
    }
}
