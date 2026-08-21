//! Kemper NRPN-over-SysEx message helpers.
//!
//! The message grammar is documented in the Kemper MIDI Parameter
//! Documentation and mirrored by PySwitch:
//! `F0 00 20 33 <product> <device> <function> <instance=00> <page> <number> <value…> F7`
//!
//! The **bidirectional beacon** (`function 0x7E`) asks the Profiler to start
//! streaming a selected parameter set and to send a status "sense" message
//! roughly every 500 ms. It must be re-sent within the time lease to stay alive.

use crate::generated;

/// Kemper MIDI manufacturer ID.
pub const MANUFACTURER_ID: [u8; 3] = generated::MANUFACTURER_ID;

/// Product type: original Profiler.
pub const PRODUCT_PROFILER: u8 = generated::PRODUCT_PROFILER;
/// Product type: Profiler Player (and Player-class units).
pub const PRODUCT_PLAYER: u8 = generated::PRODUCT_PLAYER;
/// Omni device id (all devices).
pub const DEVICE_OMNI: u8 = generated::DEVICE_OMNI;

// SysEx function codes.
/// Single Parameter Change (set), also the response to a single-param request.
pub const FUNCTION_SINGLE_PARAM: u8 = generated::FN_SINGLE_PARAM;
/// Multi Parameter Change: consecutive addresses; also the response to $42.
pub const FUNCTION_MULTI_PARAM: u8 = generated::FN_MULTI_PARAM;
/// String Parameter (set / response).
pub const FUNCTION_STRING_PARAM: u8 = generated::FN_STRING_PARAM;
/// Extended String Parameter Change (function $07): 5-byte encoded address.
pub const FUNCTION_EXT_STRING_PARAM: u8 = generated::FN_EXT_STRING_PARAM;
/// Request a single numeric parameter value (reply arrives as $01).
pub const FUNCTION_REQUEST_SINGLE: u8 = generated::FN_REQUEST_SINGLE;
/// Request all numeric parameters of a unit (reply arrives as $02).
pub const FUNCTION_REQUEST_MULTI: u8 = generated::FN_REQUEST_MULTI;
/// Request a string parameter (reply arrives as $03).
pub const FUNCTION_REQUEST_STRING: u8 = generated::FN_REQUEST_STRING;
/// Request a parameter value rendered to a string (reply arrives as $3C).
pub const FUNCTION_REQUEST_RENDERED_STRING: u8 = generated::FN_REQUEST_RENDERED_STRING;
/// Reply function for a rendered-string request ([`FUNCTION_REQUEST_RENDERED_STRING`]).
pub const FUNCTION_RENDERED_STRING_REPLY: u8 = generated::FN_RENDERED_STRING_REPLY;

/// Address page holding the string tags (rig/amp/cab names).
pub const PAGE_STRINGS: u8 = generated::PAGE_STRINGS;
/// String-tag number for the current Rig Name (page 0).
pub const STRING_RIG_NAME: u8 = generated::STRING_RIG_NAME;

/// Build the bidirectional beacon SysEx (raw MIDI, `F0…F7`).
///
/// - `init`: set on the first beacon of a session.
/// - `tuner`: request tuner data in the stream.
/// - `lease_secs`: keep-alive lease (encoded in 2-second steps); re-send within
///   half of it.
/// - `param_set`: selected parameter-set id (PySwitch uses `0x02`).
/// - `product`: product type byte to address.
pub fn beacon(init: bool, tuner: bool, lease_secs: u8, param_set: u8, product: u8) -> Vec<u8> {
    let flags = beacon_flags(init, tuner);
    let lease_encoded = lease_secs / 2;
    let mut msg = vec![0xF0];
    msg.extend_from_slice(&MANUFACTURER_ID);
    msg.extend_from_slice(&[
        product,
        DEVICE_OMNI,
        generated::BEACON_FUNCTION,
        0x00, // instance
        generated::BEACON_SUBCOMMAND,
        param_set,
        flags,
        lease_encoded,
    ]);
    msg.push(0xF7);
    msg
}

/// Beacon flag byte: bit0 init, bit1 sysex, bit2 echo, bit3 nofe, bit4 noctr, bit5 tunemode.
fn beacon_flags(init: bool, tuner: bool) -> u8 {
    let mut f = 0u8;
    if init {
        f |= generated::BEACON_FLAG_INIT;
    }
    f |= generated::BEACON_FLAG_SYSEX; // always on (the PySwitch default)
    if tuner {
        f |= generated::BEACON_FLAG_TUNEMODE;
    }
    f
}

/// Combine an MSB/LSB pair of 7-bit bytes into a 14-bit value (0–16383).
pub fn u14(msb: u8, lsb: u8) -> u16 {
    (((msb & 0x7F) as u16) << 7) | (lsb & 0x7F) as u16
}

/// Split a 14-bit value into (MSB, LSB) 7-bit bytes.
pub fn u14_split(value: u16) -> (u8, u8) {
    (((value >> 7) & 0x7F) as u8, (value & 0x7F) as u8)
}

/// Build a Kemper SysEx message with the standard header:
/// `F0 00 20 33 <product> <device> <function> <instance=0> <page> <number> <values…> F7`.
pub fn sysex(
    product: u8,
    device: u8,
    function: u8,
    page: u8,
    number: u8,
    values: &[u8],
) -> Vec<u8> {
    let mut msg = vec![0xF0];
    msg.extend_from_slice(&MANUFACTURER_ID);
    msg.extend_from_slice(&[product, device, function, 0x00, page, number]);
    msg.extend_from_slice(values);
    msg.push(0xF7);
    msg
}

/// Request a string parameter (function $43). The device replies with a $03
/// string message. Read-only — does not change device state.
pub fn request_string(product: u8, device: u8, page: u8, number: u8) -> Vec<u8> {
    sysex(product, device, FUNCTION_REQUEST_STRING, page, number, &[])
}

/// Request a single numeric parameter (function $41). Reply arrives as $01.
/// Read-only — a nonexistent address is silently ignored by the device.
pub fn request_single(product: u8, device: u8, page: u8, number: u8) -> Vec<u8> {
    sysex(product, device, FUNCTION_REQUEST_SINGLE, page, number, &[])
}

/// Set a single numeric parameter (function $01, Single Parameter Change).
/// **Mutating** — changes the value on the device. `value` is 14-bit; for a
/// switch parameter use 1 (on) / 0 (off).
pub fn set_single(product: u8, device: u8, page: u8, number: u8, value: u16) -> Vec<u8> {
    let (msb, lsb) = u14_split(value);
    sysex(
        product,
        device,
        FUNCTION_SINGLE_PARAM,
        page,
        number,
        &[msb, lsb],
    )
}

/// Request all numeric parameters of a unit (function $42, "Request Multi").
/// Reply arrives as a $02 Multi Parameter Change; decode its value block with
/// [`multi_values`]. Read-only. The request must address the *first* controller
/// number of the unit or the device ignores it.
pub fn request_multi(product: u8, device: u8, page: u8, number: u8) -> Vec<u8> {
    sysex(product, device, FUNCTION_REQUEST_MULTI, page, number, &[])
}

/// Request a parameter value rendered to a string (function $7C). The device
/// replies with a $3C message carrying the rendered ASCII (e.g. `<0.0>`).
/// Read-only, but costly in device CPU.
///
/// Layout: `<fn=7C> <flags=00> <page> <number> <valMSB> <valLSB>`. The flags
/// byte occupies the instance slot of the standard header.
pub fn request_rendered_string(
    product: u8,
    device: u8,
    page: u8,
    number: u8,
    value: u16,
) -> Vec<u8> {
    let (msb, lsb) = u14_split(value);
    sysex(
        product,
        device,
        FUNCTION_REQUEST_RENDERED_STRING,
        page,
        number,
        &[msb, lsb],
    )
}

/// Parse a $3C Rendered String reply ([`FUNCTION_RENDERED_STRING_REPLY`]) — the
/// response to [`request_rendered_string`]. The reply mirrors the $7C request
/// header, then carries the value pair and the rendered ASCII:
/// `<fn=3C> <flags> <page> <number> <valMSB> <valLSB> <ascii…> 00`.
///
/// Returns `(page, number, value, text)` where `value` is the 14-bit value the
/// string renders and `text` is the ASCII up to the first NUL (e.g. `<0.0>`,
/// `120 BPM`). Returns `None` if it isn't a $3C reply or lacks the value pair.
pub fn parse_rendered_string(msg: &[u8]) -> Option<(u8, u8, u16, String)> {
    let (hdr, vals) = NrpnHeader::parse(msg)?;
    if hdr.function != FUNCTION_RENDERED_STRING_REPLY || vals.len() < 2 {
        return None;
    }
    let value = u14(vals[0], vals[1]);
    let text: String = vals[2..]
        .iter()
        .take_while(|&&b| b != 0)
        .map(|&b| b as char)
        .collect();
    Some((hdr.page, hdr.number, value, text))
}

/// Decode a $02 Multi Parameter Change value block: the values apply to
/// **consecutive addresses** starting at `number`, each a 14-bit MSB/LSB pair.
/// Returns `(number + i, value)` for each pair; a trailing odd byte is ignored.
/// Used to parse the rig-load dump and $42 replies.
pub fn multi_values(number: u8, vals: &[u8]) -> Vec<(u8, u16)> {
    vals.chunks_exact(2)
        .enumerate()
        .map(|(i, p)| (number.wrapping_add(i as u8), u14(p[0], p[1])))
        .collect()
}

/// Decode the 5-byte-per-field "extended" encoding: big-endian, 7 data bits per
/// byte. Works on any slice length; a 5-byte input yields a 35-bit value. This
/// is the shared decode for function $06 (ext-param) and $07 (ext-string)
/// addresses/values.
pub fn ext_decode(bytes: &[u8]) -> u64 {
    bytes
        .iter()
        .fold(0u64, |acc, &x| (acc << 7) | (x & 0x7F) as u64)
}

/// Inverse of [`ext_decode`]: encode `value` big-endian into `n` bytes, 7 data
/// bits each.
pub fn ext_encode(value: u64, n: usize) -> Vec<u8> {
    let mut out = vec![0u8; n];
    for (i, byte) in out.iter_mut().enumerate() {
        let shift = 7 * (n - 1 - i);
        *byte = ((value >> shift) & 0x7F) as u8;
    }
    out
}

/// Parse a function-$07 Extended String Parameter message:
/// `F0 00 20 33 <prod> <dev> 07 <inst> <5-byte address> <ascii…> 00 F7`.
/// Returns `(address, text)` where the address decodes via the 5×7 extended
/// scheme ([`ext_decode`]). Text is the ASCII up to the first NUL (or the
/// trailing `F7`).
///
/// For a normal page the address equals `page * 128 + number` (the low two
/// 7-bit bytes hold `number` then `page`), so an extended string on page 0
/// carries the same string-tag numbers as function $03.
pub fn parse_extended_string(msg: &[u8]) -> Option<(u32, String)> {
    // F0 + mfr(3) + prod + dev + fn + inst + 5-byte addr + NUL + F7 = 14 min.
    if msg.len() < 14
        || msg[0] != 0xF0
        || msg[1..4] != MANUFACTURER_ID
        || msg[6] != FUNCTION_EXT_STRING_PARAM
        || *msg.last()? != 0xF7
    {
        return None;
    }
    let address = ext_decode(&msg[8..13]) as u32;
    let text: String = msg[13..msg.len() - 1]
        .iter()
        .take_while(|&&b| b != 0)
        .map(|&b| b as char)
        .collect();
    Some((address, text))
}

/// A 3-byte MIDI Control Change on `channel` (0–15).
pub fn control_change(channel: u8, controller: u8, value: u8) -> [u8; 3] {
    [
        generated::CONTROL_CHANGE_STATUS | (channel & 0x0F),
        controller & 0x7F,
        value & 0x7F,
    ]
}

/// A parsed Kemper NRPN/SysEx message header.
#[derive(Debug, Clone, Copy)]
pub struct NrpnHeader {
    pub product: u8,
    pub device: u8,
    pub function: u8,
    pub instance: u8,
    pub page: u8,
    pub number: u8,
}

impl NrpnHeader {
    /// Parse the fixed header of a Kemper SysEx (returns None if it isn't one).
    pub fn parse(msg: &[u8]) -> Option<(Self, &[u8])> {
        if msg.len() < 11 || msg[0] != 0xF0 || msg[1..4] != MANUFACTURER_ID {
            return None;
        }
        let hdr = NrpnHeader {
            product: msg[4],
            device: msg[5],
            function: msg[6],
            instance: msg[7],
            page: msg[8],
            number: msg[9],
        };
        // value bytes are everything between the header and the trailing 0xF7
        let end = msg.len().saturating_sub(1);
        Some((hdr, &msg[10..end]))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn init_beacon_bytes() {
        // init=1, sysex=1, tuner=1 -> flags 0x23; lease 30s -> 15 (0x0F); set 0x02.
        let b = beacon(true, true, 30, 0x02, PRODUCT_PLAYER);
        assert_eq!(
            b,
            vec![0xF0, 0x00, 0x20, 0x33, 0x02, 0x7F, 0x7E, 0x00, 0x40, 0x02, 0x23, 0x0F, 0xF7]
        );
    }

    #[test]
    fn builds_rig_name_request() {
        let msg = request_string(0x00, 0x7F, PAGE_STRINGS, STRING_RIG_NAME);
        assert_eq!(
            msg,
            vec![0xF0, 0x00, 0x20, 0x33, 0x00, 0x7F, 0x43, 0x00, 0x00, 0x01, 0xF7]
        );
        let (h, vals) = NrpnHeader::parse(&msg).unwrap();
        assert_eq!((h.function, h.page, h.number), (0x43, 0x00, 0x01));
        assert!(vals.is_empty());
    }

    #[test]
    fn builds_effect_state_set() {
        // Turn REV (page 0x3D) on: $01 to number 3, value 00 01.
        let on = set_single(0x00, 0x7F, 0x3D, 0x03, 1);
        assert_eq!(
            on,
            vec![0xF0, 0x00, 0x20, 0x33, 0x00, 0x7F, 0x01, 0x00, 0x3D, 0x03, 0x00, 0x01, 0xF7]
        );
        let off = set_single(0x00, 0x7F, 0x3D, 0x03, 0);
        assert_eq!(off[10..12], [0x00, 0x00]);
    }

    #[test]
    fn u14_roundtrip() {
        for v in [0u16, 1, 6925, 8192, 16383] {
            let (m, l) = u14_split(v);
            assert_eq!(u14(m, l), v);
        }
    }

    #[test]
    fn control_change_bytes() {
        assert_eq!(control_change(0, 50, 1), [0xB0, 50, 1]);
        assert_eq!(control_change(15, 47, 0), [0xBF, 47, 0]);
    }

    #[test]
    fn builds_request_multi() {
        let msg = request_multi(0x02, 0x7F, 0x34, 0x00);
        assert_eq!(
            msg,
            vec![0xF0, 0x00, 0x20, 0x33, 0x02, 0x7F, 0x42, 0x00, 0x34, 0x00, 0xF7]
        );
    }

    #[test]
    fn builds_request_rendered_string() {
        // fn 7C, flags 00, page 0x3C, num 53 (Ducking), value 8192 (0x40 0x00).
        let msg = request_rendered_string(0x02, 0x7F, 0x3C, 53, 8192);
        assert_eq!(
            msg,
            vec![0xF0, 0x00, 0x20, 0x33, 0x02, 0x7F, 0x7C, 0x00, 0x3C, 53, 0x40, 0x00, 0xF7]
        );
    }

    #[test]
    fn parses_rendered_string_reply() {
        let (msb, lsb) = u14_split(8192);
        let msg = sysex(
            PRODUCT_PLAYER,
            DEVICE_OMNI,
            FUNCTION_RENDERED_STRING_REPLY,
            0x3C,
            53,
            &[msb, lsb, b'<', b'0', b'.', b'0', b'>', 0],
        );
        assert_eq!(
            parse_rendered_string(&msg),
            Some((0x3C, 53, 8192, "<0.0>".to_string()))
        );

        // A too-short reply (no value pair) is rejected.
        let short = sysex(
            PRODUCT_PLAYER,
            DEVICE_OMNI,
            FUNCTION_RENDERED_STRING_REPLY,
            0x3C,
            53,
            &[0x40],
        );
        assert_eq!(parse_rendered_string(&short), None);

        // A wrong function is rejected.
        let bad = set_single(0x00, 0x7F, 0x3C, 53, 8192);
        assert_eq!(parse_rendered_string(&bad), None);
    }

    #[test]
    fn multi_values_decodes_consecutive_block() {
        let (m0, l0) = u14_split(8192);
        let (m2, l2) = u14_split(16383);
        let vals = [m0, l0, 0x00, 0x00, m2, l2];
        assert_eq!(multi_values(4, &vals), vec![(4, 8192), (5, 0), (6, 16383)]);
        // trailing odd byte is ignored
        assert_eq!(multi_values(0, &[0x01, 0x02, 0x7F]), vec![(0, u14(1, 2))]);
    }

    #[test]
    fn ext_decode_roundtrips() {
        for v in [0u64, 1, 6925, 16383, 0x1_2345_6789] {
            let enc = ext_encode(v, 5);
            assert_eq!(ext_decode(&enc), v);
            // matches the u14 scheme for small values in the low two bytes
            if v <= 16383 {
                let (m, l) = u14_split(v as u16);
                assert_eq!(ext_decode(&[m, l]), v);
            }
        }
    }

    #[test]
    fn parse_extended_string_recovers_address_and_text() {
        // Rig Name on page 0, number 1 -> address = 0*128 + 1 = 1.
        let mut msg = vec![0xF0, 0x00, 0x20, 0x33, 0x02, 0x00, 0x07, 0x00];
        msg.extend_from_slice(&ext_encode(1, 5));
        msg.extend_from_slice(b"AC30");
        msg.push(0x00);
        msg.push(0xF7);
        assert_eq!(parse_extended_string(&msg), Some((1, "AC30".to_string())));

        // A normal-page address encodes page*128 + number (page 10, number 0).
        let mut m2 = vec![0xF0, 0x00, 0x20, 0x33, 0x02, 0x00, 0x07, 0x00];
        m2.extend_from_slice(&ext_encode(10 * 128, 5));
        m2.extend_from_slice(b"JCM800");
        m2.push(0x00);
        m2.push(0xF7);
        let (a, t) = parse_extended_string(&m2).unwrap();
        assert_eq!((a / 128, a % 128, t.as_str()), (10, 0, "JCM800"));

        // wrong function is rejected
        let bad = set_single(0x00, 0x7F, 0x00, 0x01, 1);
        assert_eq!(parse_extended_string(&bad), None);
    }

    #[test]
    fn parses_status_header() {
        let msg = [
            0xF0u8, 0x00, 0x20, 0x33, 0x00, 0x00, 0x02, 0x00, 0x7C, 0x4E, 0x00, 0x00, 0xF7,
        ];
        let (h, vals) = NrpnHeader::parse(&msg).unwrap();
        assert_eq!((h.function, h.page, h.number), (0x02, 0x7C, 0x4E));
        assert_eq!(vals, &[0x00, 0x00]);
    }
}
