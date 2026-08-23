//! The device's native CBOR control channel and its state-dump snapshot.
//!
//! The `{774CDB9E-…}` protocol GUID ([`session::PROTOCOL_CBOR_CONTROL`]) completes
//! the same [handshake](crate::session) and 8-byte preamble as the MIDI3 stream,
//! then speaks **CBOR** (RFC 8949) rather than MIDI3 frames — a continuous
//! sequence of bare items with no outer framing (see `docs/06`). This module is
//! the reader and writer for that channel:
//!
//! - [`Value`] is one decoded item; [`Decoder`] is a streaming reader that
//!   buffers partial items across TCP chunk boundaries and tolerates the
//!   inter-item filler the channel emits.
//! - [`encode`] / [`to_vec`] and [`param_write`] write the one item this library
//!   sends — the write that asks the device for its full state.
//! - [`extract_snapshot`] reads the device's current bank and rig slot out of the
//!   resulting dump, and [`StateSnapshot::fetch`] runs the whole exchange over a
//!   fresh session.
//!
//! The channel does not volunteer state: a passive session sees only live change
//! events. Writing [`param_write`]`(`[`generated::STATE_DUMP_TRIGGER_ADDRESS`]`,
//! 1)` asks for the whole parameter state, which arrives as a burst of
//! multi-parameter and string items carrying — among much else — the current
//! bank ([`generated::CURRENT_BANK_ADDRESS`]) and rig slot
//! ([`generated::CURRENT_RIG_SLOT_ADDRESS`]), both 0-based. The write is
//! non-mutating.

use std::net::Ipv4Addr;
use std::sync::Mutex;
use std::time::Duration;

use tokio::sync::broadcast;
use tokio::task::JoinHandle;

use crate::SessionError;
use crate::generated;
use crate::session::{PROTOCOL_CBOR_CONTROL, Session};

/// A decoded CBOR data item.
#[derive(Debug, Clone, PartialEq)]
pub enum Value {
    Uint(u64),
    /// Negative integer, held widened so `-1 - u64::MAX` still fits.
    Neg(i128),
    Bytes(Vec<u8>),
    Text(String),
    Array(Vec<Value>),
    Map(Vec<(Value, Value)>),
    Tag(u64, Box<Value>),
    Bool(bool),
    Null,
    Undefined,
    Simple(u8),
    Float(f64),
    /// The `0xFF` break stop-code; only valid inside an indefinite-length item.
    Break,
}

impl Value {
    /// The item as an integer, for the `[selector, addr, value]` shapes.
    pub fn as_i128(&self) -> Option<i128> {
        match self {
            Value::Uint(u) => Some(*u as i128),
            Value::Neg(n) => Some(*n),
            _ => None,
        }
    }

    /// The elements, if this is an array (unwrapping one layer of tag first).
    pub fn as_array(&self) -> Option<&[Value]> {
        match self {
            Value::Array(items) => Some(items),
            Value::Tag(_, inner) => inner.as_array(),
            _ => None,
        }
    }
}

/// Why a parse did not yield an item.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Error {
    /// Ran out of bytes mid-item — wait for more of the stream.
    Incomplete,
    /// Not valid CBOR (reserved additional-info, bad UTF-8, nesting too deep).
    Invalid,
}

/// Nesting limit — guards the recursive descent against hostile/desynced input.
const MAX_DEPTH: usize = 32;

/// Heads that open a text string (major type 3), definite or indefinite.
fn is_text_head(b: u8) -> bool {
    b >> 5 == 3
}

/// A streaming CBOR reader: feed it TCP chunks, take whole items back.
///
/// The channel pads between top-level items with runs of the filler byte
/// [`generated::CBOR_FILLER_BYTE`] (`0xC0`) — a `tag(0)` head with no content,
/// which is not well-formed CBOR on its own. Parsed naively it swallows the
/// following item, so the decoder skips it. A genuine `tag(0)` is an RFC 8949
/// date/time whose content must be a text string, so a `0xC0` is treated as
/// filler only when the next head is *not* a text head, leaving real datetimes
/// intact.
#[derive(Debug, Default)]
pub struct Decoder {
    buf: Vec<u8>,
    filler: usize,
}

impl Decoder {
    pub fn new() -> Self {
        Self::default()
    }

    /// Feed raw stream bytes; return every item completed by this input.
    ///
    /// A byte that cannot start a valid item is dropped to resync (the same
    /// strategy [`crate::midi3::Unframer`] uses), so a mid-stream join or a
    /// wrong-protocol guess degrades to noise rather than a permanent stall.
    pub fn push(&mut self, data: &[u8]) -> Vec<Value> {
        self.buf.extend_from_slice(data);
        let mut out = Vec::new();
        let mut off = 0usize;
        // `skip_filler` returning None means a trailing filler byte we cannot
        // classify until more bytes arrive — stop and keep it buffered.
        while let Some(next) = self.skip_filler(off) {
            off = next;
            match parse_item(&self.buf[off..], 0) {
                Ok((value, used)) => {
                    off += used;
                    out.push(value);
                }
                Err(Error::Incomplete) => break,
                Err(Error::Invalid) => off += 1, // resync
            }
        }
        self.buf.drain(..off);
        out
    }

    /// Bytes buffered but not yet forming a complete item.
    pub fn pending(&self) -> usize {
        self.buf.len()
    }

    /// How many filler bytes have been skipped over this stream's life.
    pub fn filler_bytes(&self) -> usize {
        self.filler
    }

    /// Step `off` past any inter-item filler. `None` means the buffer ends on a
    /// filler byte whose role cannot be decided until the next byte arrives.
    fn skip_filler(&mut self, mut off: usize) -> Option<usize> {
        while self.buf.get(off) == Some(&generated::CBOR_FILLER_BYTE) {
            match self.buf.get(off + 1) {
                Some(&b) if is_text_head(b) => return Some(off), // real datetime tag
                Some(_) => {
                    off += 1;
                    self.filler += 1;
                }
                None => return None,
            }
        }
        Some(off)
    }
}

/// Parse one item from the front of `b`. Returns the item and bytes consumed.
fn parse_item(b: &[u8], depth: usize) -> Result<(Value, usize), Error> {
    if depth > MAX_DEPTH {
        return Err(Error::Invalid);
    }
    let head = *b.first().ok_or(Error::Incomplete)?;
    let major = head >> 5;
    let ai = head & 0x1F;

    // Additional info 31 = indefinite length (or the break stop-code).
    if ai == 31 {
        return match major {
            2 => parse_indefinite_chunks(b, depth, true),
            3 => parse_indefinite_chunks(b, depth, false),
            4 => parse_indefinite_array(b, depth),
            5 => parse_indefinite_map(b, depth),
            7 => Ok((Value::Break, 1)),
            _ => Err(Error::Invalid),
        };
    }

    let (arg, head_len) = parse_argument(b, ai)?;
    let rest = &b[head_len..];

    match major {
        0 => Ok((Value::Uint(arg), head_len)),
        1 => Ok((Value::Neg(-1 - arg as i128), head_len)),
        2 | 3 => {
            let n = usize::try_from(arg).map_err(|_| Error::Invalid)?;
            let bytes = rest.get(..n).ok_or(Error::Incomplete)?;
            let value = if major == 2 {
                Value::Bytes(bytes.to_vec())
            } else {
                Value::Text(
                    std::str::from_utf8(bytes)
                        .map_err(|_| Error::Invalid)?
                        .to_string(),
                )
            };
            Ok((value, head_len + n))
        }
        4 => {
            let n = usize::try_from(arg).map_err(|_| Error::Invalid)?;
            let mut items = Vec::with_capacity(n.min(64));
            let mut off = head_len;
            for _ in 0..n {
                let (v, used) = parse_item(&b[off..], depth + 1)?;
                off += used;
                items.push(v);
            }
            Ok((Value::Array(items), off))
        }
        5 => {
            let n = usize::try_from(arg).map_err(|_| Error::Invalid)?;
            let mut pairs = Vec::with_capacity(n.min(64));
            let mut off = head_len;
            for _ in 0..n {
                let (k, used) = parse_item(&b[off..], depth + 1)?;
                off += used;
                let (v, used) = parse_item(&b[off..], depth + 1)?;
                off += used;
                pairs.push((k, v));
            }
            Ok((Value::Map(pairs), off))
        }
        6 => {
            let (inner, used) = parse_item(rest, depth + 1)?;
            Ok((Value::Tag(arg, Box::new(inner)), head_len + used))
        }
        7 => parse_simple(ai, arg, head_len),
        _ => Err(Error::Invalid),
    }
}

/// Read the head byte's argument. Returns `(value, total head length)`.
fn parse_argument(b: &[u8], ai: u8) -> Result<(u64, usize), Error> {
    let width = match ai {
        0..=23 => return Ok((ai as u64, 1)),
        24 => 1,
        25 => 2,
        26 => 4,
        27 => 8,
        _ => return Err(Error::Invalid), // 28..=30 are reserved
    };
    let bytes = b.get(1..1 + width).ok_or(Error::Incomplete)?;
    let arg = bytes.iter().fold(0u64, |acc, &x| (acc << 8) | x as u64);
    Ok((arg, 1 + width))
}

/// Major type 7: booleans, null, undefined, simple values and floats.
fn parse_simple(ai: u8, arg: u64, head_len: usize) -> Result<(Value, usize), Error> {
    let value = match ai {
        20 => Value::Bool(false),
        21 => Value::Bool(true),
        22 => Value::Null,
        23 => Value::Undefined,
        0..=19 | 24 => Value::Simple(arg as u8),
        25 => Value::Float(f16_to_f64(arg as u16)),
        26 => Value::Float(f32::from_bits(arg as u32) as f64),
        27 => Value::Float(f64::from_bits(arg)),
        _ => return Err(Error::Invalid),
    };
    Ok((value, head_len))
}

/// Indefinite-length byte/text string: definite chunks until the break code.
fn parse_indefinite_chunks(
    b: &[u8],
    depth: usize,
    bytes_mode: bool,
) -> Result<(Value, usize), Error> {
    let mut off = 1;
    let mut acc: Vec<u8> = Vec::new();
    loop {
        if *b.get(off).ok_or(Error::Incomplete)? == 0xFF {
            off += 1;
            break;
        }
        match parse_item(&b[off..], depth + 1)? {
            (Value::Bytes(chunk), used) if bytes_mode => {
                acc.extend_from_slice(&chunk);
                off += used;
            }
            (Value::Text(chunk), used) if !bytes_mode => {
                acc.extend_from_slice(chunk.as_bytes());
                off += used;
            }
            _ => return Err(Error::Invalid), // chunks must match the outer type
        }
    }
    let value = if bytes_mode {
        Value::Bytes(acc)
    } else {
        Value::Text(String::from_utf8(acc).map_err(|_| Error::Invalid)?)
    };
    Ok((value, off))
}

fn parse_indefinite_array(b: &[u8], depth: usize) -> Result<(Value, usize), Error> {
    let mut off = 1;
    let mut items = Vec::new();
    loop {
        if *b.get(off).ok_or(Error::Incomplete)? == 0xFF {
            off += 1;
            break;
        }
        let (v, used) = parse_item(&b[off..], depth + 1)?;
        off += used;
        items.push(v);
    }
    Ok((Value::Array(items), off))
}

fn parse_indefinite_map(b: &[u8], depth: usize) -> Result<(Value, usize), Error> {
    let mut off = 1;
    let mut pairs = Vec::new();
    loop {
        if *b.get(off).ok_or(Error::Incomplete)? == 0xFF {
            off += 1;
            break;
        }
        let (k, used) = parse_item(&b[off..], depth + 1)?;
        off += used;
        let (v, used) = parse_item(&b[off..], depth + 1)?;
        off += used;
        pairs.push((k, v));
    }
    Ok((Value::Map(pairs), off))
}

/// IEEE-754 half precision → f64 (CBOR additional info 25).
fn f16_to_f64(bits: u16) -> f64 {
    let sign = if bits & 0x8000 != 0 { -1.0 } else { 1.0 };
    let exp = ((bits >> 10) & 0x1F) as i32;
    let frac = (bits & 0x03FF) as f64;
    let mag = match exp {
        0 => frac * 2f64.powi(-24), // subnormal
        31 if frac == 0.0 => f64::INFINITY,
        31 => f64::NAN,
        _ => (frac / 1024.0 + 1.0) * 2f64.powi(exp - 15),
    };
    sign * mag
}

// --------------------------------------------------------------------------
// Encoding
// --------------------------------------------------------------------------

/// Write a CBOR head: the 3-bit major type plus the shortest argument encoding
/// that fits, which is what the device itself emits.
fn write_head(major: u8, arg: u64, out: &mut Vec<u8>) {
    let m = major << 5;
    match arg {
        0..=23 => out.push(m | arg as u8),
        24..=0xFF => out.extend_from_slice(&[m | 24, arg as u8]),
        0x100..=0xFFFF => {
            out.push(m | 25);
            out.extend_from_slice(&(arg as u16).to_be_bytes());
        }
        0x1_0000..=0xFFFF_FFFF => {
            out.push(m | 26);
            out.extend_from_slice(&(arg as u32).to_be_bytes());
        }
        _ => {
            out.push(m | 27);
            out.extend_from_slice(&arg.to_be_bytes());
        }
    }
}

/// Encode a value as CBOR, appending to `out`.
///
/// [`Value::Break`] encodes as the bare `0xFF` stop code, which is only
/// well-formed inside an indefinite-length item; this module never produces one,
/// so encoding a stray `Break` yields bytes the device would reject.
pub fn encode(v: &Value, out: &mut Vec<u8>) {
    match v {
        Value::Uint(u) => write_head(0, *u, out),
        // Major type 1 encodes -1-n as the argument n.
        Value::Neg(n) => write_head(1, (-1 - *n) as u64, out),
        Value::Bytes(b) => {
            write_head(2, b.len() as u64, out);
            out.extend_from_slice(b);
        }
        Value::Text(t) => {
            write_head(3, t.len() as u64, out);
            out.extend_from_slice(t.as_bytes());
        }
        Value::Array(items) => {
            write_head(4, items.len() as u64, out);
            items.iter().for_each(|i| encode(i, out));
        }
        Value::Map(pairs) => {
            write_head(5, pairs.len() as u64, out);
            for (k, val) in pairs {
                encode(k, out);
                encode(val, out);
            }
        }
        Value::Tag(t, inner) => {
            write_head(6, *t, out);
            encode(inner, out);
        }
        Value::Bool(false) => out.push(0xF4),
        Value::Bool(true) => out.push(0xF5),
        Value::Null => out.push(0xF6),
        Value::Undefined => out.push(0xF7),
        Value::Simple(s) => write_head(7, *s as u64, out),
        Value::Float(f) => {
            out.push(0xFB);
            out.extend_from_slice(&f.to_bits().to_be_bytes());
        }
        Value::Break => out.push(0xFF),
    }
}

/// Encode a value as a fresh byte vector.
pub fn to_vec(v: &Value) -> Vec<u8> {
    let mut out = Vec::new();
    encode(v, &mut out);
    out
}

/// An integer as the right [`Value`] variant for its sign.
fn int(n: i64) -> Value {
    if n < 0 {
        Value::Neg(n as i128)
    } else {
        Value::Uint(n as u64)
    }
}

/// Build a single-parameter write, `tag(1)([1, addr, value])` — the shape the
/// channel uses to set a parameter (`docs/06`).
///
/// This is a **write**: the device applies it and rebroadcasts it to every other
/// open session. The one this library sends —
/// `param_write(`[`generated::STATE_DUMP_TRIGGER_ADDRESS`]`, 1)` — is
/// non-mutating and asks the device for its full state.
pub fn param_write(addr: u32, value: i64) -> Value {
    Value::Tag(
        generated::CBOR_ITEM_TAG,
        Box::new(Value::Array(vec![
            Value::Uint(generated::CBOR_SELECTOR_SINGLE as u64),
            Value::Uint(addr as u64),
            int(value),
        ])),
    )
}

/// The item that asks the device for its full parameter state.
pub fn state_dump_request() -> Value {
    param_write(
        generated::STATE_DUMP_TRIGGER_ADDRESS,
        generated::STATE_DUMP_TRIGGER_VALUE,
    )
}

// --------------------------------------------------------------------------
// Snapshot
// --------------------------------------------------------------------------

/// Is `addr` one whose string value is a device secret (WiFi credentials)?
/// The state dump volunteers these in the clear; a reader must never surface
/// them. See [`generated::SENSITIVE_ADDRESSES`].
pub fn is_sensitive(addr: u32) -> bool {
    generated::SENSITIVE_ADDRESSES.contains(&addr)
}

/// The device's current position and the values carried alongside it, read out
/// of a state dump. Both indices are 0-based; any field is `None` if the dump
/// did not carry it.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct StateSnapshot {
    /// Current bank, 0-based ([`generated::CURRENT_BANK_ADDRESS`]).
    pub current_bank: Option<u16>,
    /// Current rig slot within the bank, 0-based
    /// ([`generated::CURRENT_RIG_SLOT_ADDRESS`]).
    pub current_rig_slot: Option<u16>,
    /// The morph position (0 = base, 16383 = fully morphed), at
    /// [`generated::MORPH_ADDRESS`].
    ///
    /// This dump is the only way a client that is not holding a CBOR session
    /// open can learn it: the position is never sent on the MIDI3 stream, and
    /// answers neither a `$41` nor a `$46` request. It is a live value, so it is
    /// true as of the read and stale the moment anyone morphs.
    pub morph: Option<u16>,
    /// String parameters the dump carried, by address, with any sensitive value
    /// redacted. In document order; useful for the current rig name (address 1)
    /// and the bank name.
    pub strings: Vec<(u32, String)>,
}

impl StateSnapshot {
    /// True once every value this snapshot reads is known — the point at which
    /// the reader may stop before the dump has finished streaming.
    ///
    /// The morph counts: it arrives later in the dump than the two indices, so
    /// stopping at those would truncate the read just short of it and leave
    /// [`morph`](Self::morph) `None` on a device that reported it perfectly
    /// well. Every dump observed carries all three, at base as readily as
    /// morphed.
    pub fn is_complete(&self) -> bool {
        self.current_bank.is_some() && self.current_rig_slot.is_some() && self.morph.is_some()
    }

    /// The string parameter at `addr`, if the dump carried one.
    pub fn string(&self, addr: u32) -> Option<&str> {
        self.strings
            .iter()
            .find(|(a, _)| *a == addr)
            .map(|(_, s)| s.as_str())
    }
}

/// Record the value at one address into the snapshot's indices.
fn note_index(snap: &mut StateSnapshot, addr: i128, value: i128) {
    let v = u16::try_from(value).ok();
    if addr == generated::CURRENT_BANK_ADDRESS as i128 {
        snap.current_bank = v;
    } else if addr == generated::CURRENT_RIG_SLOT_ADDRESS as i128 {
        snap.current_rig_slot = v;
    } else if addr == generated::MORPH_ADDRESS as i128 {
        snap.morph = v;
    }
}

/// Read the current bank, rig slot and string parameters out of decoded dump
/// items.
///
/// Scans the two index-bearing shapes: a single `[1, addr, value]`, and a
/// consecutive-run `[2, base, v0, v1, …]` where the whole run is walked because
/// the target address can fall anywhere inside it (at session open the position
/// arrives inside one run). A leading negative source-flag word, if present, is
/// skipped exactly as [`param_write`] never emits one.
pub fn extract_snapshot(items: &[Value]) -> StateSnapshot {
    let mut snap = StateSnapshot::default();
    walk(items, &mut |element| match element {
        Element::Numeric(addr, value) => note_index(&mut snap, addr, value),
        Element::Text(addr, text) => {
            let a = addr as u32;
            let text = if is_sensitive(a) {
                generated::REDACTED_PLACEHOLDER.to_string()
            } else {
                text.to_string()
            };
            snap.strings.push((a, text));
        }
    });
    snap
}

/// One element of a decoded dump or push, as [`walk`] hands it out.
enum Element<'a> {
    /// A numeric parameter: flat address, value.
    Numeric(i128, i128),
    /// A string parameter: flat address, text.
    Text(i128, &'a str),
}

/// Every numeric address/value pair the items carry, in document order.
///
/// The dump and a session's live pushes are the same shapes, so this is what a
/// [`CborSession`] folds into a state tree as values move.
pub fn numeric_values(items: &[Value]) -> Vec<(u32, i64)> {
    let mut out = Vec::new();
    walk(items, &mut |element| {
        if let Element::Numeric(addr, value) = element {
            // A negative or oversized address is malformed, not an address to
            // wrap into a plausible-looking one.
            if let (Ok(a), Ok(v)) = (u32::try_from(addr), i64::try_from(value)) {
                out.push((a, v));
            }
        }
    });
    out
}

/// Walk decoded items, handing each numeric and string element to a callback.
///
/// Scans the value-bearing shapes: a single `[1, addr, value]`, a consecutive-run
/// `[2, base, v0, v1, …]` where the whole run is walked because the target
/// address can fall anywhere inside it, and a string `[4, addr, text]`. A leading
/// negative source-flag word, if present, is skipped.
fn walk(items: &[Value], visit: &mut dyn FnMut(Element)) {
    for item in items {
        let Some(fields) = item.as_array() else {
            continue;
        };
        // Skip a leading negative source-flags word.
        let rest = match fields.first().and_then(Value::as_i128) {
            Some(n) if n < 0 => &fields[1..],
            _ => fields,
        };
        let Some(selector) = rest.first().and_then(Value::as_i128) else {
            continue;
        };
        let addr = rest.get(1).and_then(Value::as_i128);
        if selector == generated::CBOR_SELECTOR_SINGLE as i128 {
            if let (Some(a), Some(v)) = (addr, rest.get(2).and_then(Value::as_i128)) {
                visit(Element::Numeric(a, v));
            }
        } else if selector == generated::CBOR_SELECTOR_MULTI as i128 {
            if let Some(base) = addr {
                for (i, v) in rest[2..].iter().enumerate() {
                    if let Some(v) = v.as_i128() {
                        visit(Element::Numeric(base + i as i128, v));
                    }
                }
            }
        } else if selector == generated::CBOR_SELECTOR_STRING as i128 {
            if let (Some(a), Some(Value::Text(t))) = (addr, rest.get(2)) {
                if !t.is_empty() {
                    visit(Element::Text(a, t));
                }
            }
        }
    }
}

impl StateSnapshot {
    /// Default time to keep reading the dump before giving up on the indices.
    pub const DEFAULT_TIMEOUT: Duration = Duration::from_secs(3);

    /// Open a fresh CBOR session to `ip`, trigger the state dump, and read back
    /// the current bank, rig slot and morph position.
    ///
    /// This opens its **own** short-lived connection, independent of any
    /// [`DeviceModel`](crate::model), and closes it (by drop) on return. The
    /// device crashes under connection churn, so run this sparingly. For a *live*
    /// view, and to run alongside a streaming session rather than before it, use
    /// [`CborSession`]. Returns as soon as every value it reads is known (see
    /// [`is_complete`](Self::is_complete)) or the default timeout elapses.
    pub async fn fetch(ip: Ipv4Addr) -> Result<StateSnapshot, SessionError> {
        Self::fetch_with(ip, Self::DEFAULT_TIMEOUT).await
    }

    /// [`fetch`](Self::fetch) with an explicit read timeout.
    pub async fn fetch_with(
        ip: Ipv4Addr,
        timeout: Duration,
    ) -> Result<StateSnapshot, SessionError> {
        let idle = Duration::from_millis(30);
        let mut session = Session::connect(ip).await?;
        let outcome = session.handshake(&[PROTOCOL_CBOR_CONTROL], idle).await?;
        session.write_session_preamble().await?;

        let mut decoder = Decoder::new();
        let mut items = decoder.push(outcome.response_tail());

        // Ask for the full state, then read until both indices land or the
        // deadline passes.
        session.write_all(&to_vec(&state_dump_request())).await?;

        let deadline = tokio::time::Instant::now() + timeout;
        loop {
            if extract_snapshot(&items).is_complete() {
                break;
            }
            let remaining = deadline.saturating_duration_since(tokio::time::Instant::now());
            if remaining.is_zero() {
                break;
            }
            match session.read_once(idle.min(remaining), 64 * 1024).await {
                Ok(chunk) if chunk.is_empty() => {}
                Ok(chunk) => items.extend(decoder.push(&chunk)),
                Err(SessionError::Closed) => break,
                Err(e) => return Err(e),
            }
        }
        Ok(extract_snapshot(&items))
    }
}

// ---------------------------------------------------------------------------
// Live session
// ---------------------------------------------------------------------------

/// One value the device pushed on the CBOR channel: a flat address and its
/// value, as the channel encodes it (35-bit range, so wider than `$01`).
pub type CborUpdate = (u32, i64);

/// A **live** session on the native CBOR channel: it opens, asks for the state
/// dump, and then streams every value the device pushes until it is dropped.
///
/// This is the counterpart to [`StateSnapshot::fetch`], which reads one dump and
/// hangs up. Hold this open instead when you want to *watch* the values the
/// MIDI3 stream never carries — the morph position above all, which is pushed
/// here at about 40 Hz while a morph ramps and is unreachable by any request on
/// the streaming session.
///
/// **It may run alongside a [`DeviceModel`](crate::model::DeviceModel).** The
/// device's fragility is about connection *churn*, not concurrency: two
/// read-only sessions coexist happily, and the pair is how a client gets both
/// the meter lane and the morph. Space the two connections by
/// [`generated::CONNECTION_COOLDOWN_MS`] when opening them, and do not reopen
/// either in a tight loop.
///
/// Read-only. The one thing it writes is the state-dump trigger, which is a flag
/// the device already carries — see `docs/06`.
pub struct CborSession {
    updates: broadcast::Sender<CborUpdate>,
    /// The receiver opened before ingest started, handed to the first
    /// [`subscribe`](Self::subscribe) so the state dump survives the gap between
    /// connecting and subscribing.
    initial: Mutex<Option<broadcast::Receiver<CborUpdate>>>,
    task: JoinHandle<()>,
}

impl CborSession {
    /// How long a read waits before looping. Short, so a drop takes effect
    /// promptly.
    const READ_IDLE: Duration = Duration::from_millis(250);

    /// Connect to `ip:5727`, open the CBOR protocol, ask for the state dump, and
    /// start streaming.
    ///
    /// Returns once the session is established; values arrive on
    /// [`subscribe`](Self::subscribe). Subscribe *before* awaiting them, or the
    /// dump's own burst is missed.
    pub async fn connect(ip: Ipv4Addr) -> Result<Self, SessionError> {
        let mut session = Session::connect(ip).await?;
        let outcome = session
            .handshake(&[PROTOCOL_CBOR_CONTROL], Duration::from_millis(30))
            .await?;
        session.write_session_preamble().await?;
        let tail = outcome.response_tail().to_vec();

        // Writing one item asks for the whole state; the reply is the burst the
        // stream opens with.
        session.write_all(&to_vec(&state_dump_request())).await?;

        // Subscribe before spawning: `broadcast::Sender::send` discards when
        // nothing is listening, and the opening burst — the only place several
        // values, the morph among them, appear until something moves them —
        // lands immediately. This receiver keeps it, and `subscribe` hands it to
        // the first caller.
        let (updates, initial) = broadcast::channel(4096);
        let task = tokio::spawn(ingest(session, updates.clone(), tail));
        Ok(CborSession {
            updates,
            initial: Mutex::new(Some(initial)),
            task,
        })
    }

    /// Subscribe to every value the device pushes, in arrival order.
    ///
    /// The first caller also receives whatever arrived before it subscribed, so
    /// the state dump is not lost to the gap after [`connect`](Self::connect).
    pub fn subscribe(&self) -> broadcast::Receiver<CborUpdate> {
        if let Some(initial) = self.initial.lock().ok().and_then(|mut g| g.take()) {
            return initial;
        }
        self.updates.subscribe()
    }
}

impl Drop for CborSession {
    fn drop(&mut self) {
        self.task.abort();
    }
}

/// Own the socket: read, decode, and fan out every numeric value.
async fn ingest(mut session: Session, updates: broadcast::Sender<CborUpdate>, tail: Vec<u8>) {
    let mut decoder = Decoder::new();
    for pair in numeric_values(&decoder.push(&tail)) {
        let _ = updates.send(pair);
    }
    loop {
        match session.read_once(CborSession::READ_IDLE, 64 * 1024).await {
            Ok(chunk) if chunk.is_empty() => {}
            Ok(chunk) => {
                for pair in numeric_values(&decoder.push(&chunk)) {
                    let _ = updates.send(pair);
                }
            }
            Err(_) => return,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn encodes_with_minimal_length_heads() {
        // uint16 address, small value.
        assert_eq!(
            to_vec(&param_write(15953, 0)),
            vec![0xc1, 0x83, 0x01, 0x19, 0x3e, 0x51, 0x00]
        );
        // uint32 address (the extended range).
        assert_eq!(
            to_vec(&param_write(102405, 19)),
            vec![0xc1, 0x83, 0x01, 0x1a, 0x00, 0x01, 0x90, 0x05, 0x13]
        );
    }

    #[test]
    fn encodes_the_state_dump_request() {
        // tag(1)([1, 102528, 1]) — the write that asks for the full state.
        assert_eq!(
            to_vec(&state_dump_request()),
            vec![0xc1, 0x83, 0x01, 0x1a, 0x00, 0x01, 0x90, 0x80, 0x01]
        );
    }

    #[test]
    fn round_trips_through_the_decoder() {
        for item in [
            param_write(102528, 1),
            param_write(0, 0),
            param_write(16383, -1),
        ] {
            let mut d = Decoder::new();
            let out = d.push(&to_vec(&item));
            assert_eq!(out, vec![item]);
            assert_eq!(d.pending(), 0);
        }
    }

    #[test]
    fn decoder_skips_inter_item_filler() {
        let mut bytes = vec![generated::CBOR_FILLER_BYTE, generated::CBOR_FILLER_BYTE];
        bytes.extend(to_vec(&param_write(1412, 8629)));
        let mut d = Decoder::new();
        let out = d.push(&bytes);
        assert_eq!(out, vec![param_write(1412, 8629)]);
        assert_eq!(d.filler_bytes(), 2);
    }

    /// A multi-parameter run `[2, 100700, v0, v1, v2]` carries the position:
    /// 100700, then bank at 100701 and slot at 100702.
    #[test]
    fn extracts_position_from_a_multi_run() {
        let run = Value::Tag(
            1,
            Box::new(Value::Array(vec![
                Value::Uint(2),
                Value::Uint(100_700),
                Value::Uint(0),
                Value::Uint(1),
                Value::Uint(2),
            ])),
        );
        let snap = extract_snapshot(std::slice::from_ref(&run));
        assert_eq!(snap.current_bank, Some(1));
        assert_eq!(snap.current_rig_slot, Some(2));
        // The morph has not landed, so the reader must keep going.
        assert!(!snap.is_complete());
        let snap = extract_snapshot(&[run.clone(), param_write(119, 8192)]);
        assert_eq!(snap.morph, Some(8192));
        assert!(snap.is_complete());
    }

    #[test]
    fn extracts_position_from_single_items() {
        let items = vec![param_write(100_701, 3), param_write(100_702, 4)];
        let snap = extract_snapshot(&items);
        assert_eq!(snap.current_bank, Some(3));
        assert_eq!(snap.current_rig_slot, Some(4));
    }

    #[test]
    fn collects_strings_and_redacts_secrets() {
        let name = Value::Tag(
            1,
            Box::new(Value::Array(vec![
                Value::Uint(4),
                Value::Uint(1),
                Value::Text("Maz 18 Pushed".into()),
            ])),
        );
        let secret = Value::Tag(
            1,
            Box::new(Value::Array(vec![
                Value::Uint(4),
                Value::Uint(generated::SENSITIVE_ADDRESSES[0] as u64),
                Value::Text("hunter2".into()),
            ])),
        );
        let snap = extract_snapshot(&[name, secret]);
        assert_eq!(snap.string(1), Some("Maz 18 Pushed"));
        assert_eq!(
            snap.string(generated::SENSITIVE_ADDRESSES[0]),
            Some(generated::REDACTED_PLACEHOLDER)
        );
    }

    /// A negative or oversized address is malformed; wrapping it into a
    /// plausible-looking `u32` would invent a parameter that was never sent.
    #[test]
    fn numeric_values_skips_unrepresentable_addresses() {
        let good = param_write(generated::MORPH_ADDRESS, 8192);
        assert_eq!(
            numeric_values(&[good]),
            vec![(generated::MORPH_ADDRESS, 8192)]
        );
        let wide = Value::Tag(
            1,
            Box::new(Value::Array(vec![
                Value::Uint(generated::CBOR_SELECTOR_SINGLE as u64),
                Value::Uint(u64::from(u32::MAX) + 1),
                Value::Uint(1),
            ])),
        );
        assert!(numeric_values(&[wide]).is_empty());
    }
}
