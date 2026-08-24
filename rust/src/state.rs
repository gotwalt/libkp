//! The typed, immutable device state tree — the **store snapshot** the UI binds
//! to — and the decoders that feed it.
//!
//! Everything here is plain data: `Clone`, `Debug`, `PartialEq` (and `Copy`
//! where cheap), with no `HashMap`, no borrows beyond `&'static str` labels, and
//! no methods that read fields (callers touch the fields directly). That keeps
//! the tree FFI-friendly — a flat set of records and enums a foreign-function
//! layer can mirror as value types — and cheap to clone for each snapshot the
//! store emits.
//!
//! [`DeviceState`] is the root. A value reaches it on one of two wires — a
//! MIDI3 SysEx message via [`DeviceState::apply`], or a CBOR item via
//! [`DeviceState::apply_cbor`] / [`DeviceState::apply_cbor_text`] — and each of
//! those is a thin decoder that turns the bytes into an [`Update`] and hands it
//! to the one funnel, [`DeviceState::apply_update`] in [`crate::routes`], where
//! the generated routing table decides what the tree does with it.

use crate::generated;
use crate::model::{ApplyOutcome, DeviceEvent, RealtimeStatus};
use crate::nrpn::{
    self, FUNCTION_EXT_PARAM, FUNCTION_EXT_STRING_PARAM, FUNCTION_MULTI_PARAM,
    FUNCTION_RENDERED_STRING_REPLY, FUNCTION_SINGLE_PARAM, FUNCTION_STRING_PARAM, NrpnHeader, u14,
};
use crate::params;

/// Where the model stands with the device as a whole — the one word an app keys
/// its connection indicator on. The two links underneath it are reported
/// separately in [`Channels`]; this is their summary.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Connection {
    /// No session: the initial state, the device closed the stream and no
    /// reconnect policy was set, or the model was closed.
    Disconnected,
    /// The stream was lost and the model is waiting out its backoff before it
    /// dials again. `attempt` counts from 1 and starts over once a dial
    /// succeeds.
    Reconnecting {
        /// Which retry is pending, counting from 1.
        attempt: u32,
    },
    /// The stream is open and the control link is wherever its policy says it
    /// should be: open, or never asked for.
    Connected,
    /// The stream is open but the control link, which was asked for, is not —
    /// it could not be opened, or it was lost. Everything the stream carries
    /// still flows; only what the control link alone carries (the morph
    /// position) has gone stale.
    Degraded,
}

/// The life of one of the model's two links to the device.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ChannelState {
    /// Not asked for: the policy is off, or the first attempt has not started.
    Closed,
    /// Dialing, or in the handshake.
    Connecting,
    /// Streaming.
    Open,
    /// The open failed — the dial, the handshake, or (for the control link)
    /// the write of the dump trigger.
    Unavailable,
    /// Was open, then the socket ended.
    Lost,
}

/// The state of both links, as they really are. The stream is the session the
/// model cannot do without; the control link is the CBOR channel that carries
/// the state dump and the morph.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Channels {
    /// The MIDI3 stream.
    pub stream: ChannelState,
    /// The CBOR control channel.
    pub control: ChannelState,
}

impl Default for Channels {
    fn default() -> Self {
        Channels {
            stream: ChannelState::Closed,
            control: ChannelState::Closed,
        }
    }
}

/// Which wire carried an [`Update`].
///
/// The two channels are one event universe in two wire formats, but they are
/// not interchangeable: the routing table's `wire` column says which of them
/// may write each row, because the control channel carries its own copies of
/// the realtime page and the momentaries that the tree must not take.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Channel {
    /// The MIDI3 SysEx stream.
    Stream,
    /// The CBOR control channel.
    Control,
}

/// Whether an [`Update`] is a value the device pushed as it changed, or one
/// item of the CBOR state dump it sends on request.
///
/// A dump is a snapshot taken when it was asked for, so a value that moved
/// while the dump was still streaming is fresher than the dump's copy of it.
/// [`DeviceState::begin_dump`] / [`DeviceState::end_dump`] bracket that window.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Phase {
    /// A pushed value: current as of its arrival.
    Live,
    /// One item of the state dump: current as of the dump's start.
    Dump,
}

/// The payload of an [`Update`], decoded off the wire but not yet interpreted
/// — the routing table's `kind` column does that.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Decoded {
    /// A numeric value. `$01` carries 14 bits, `$06` 35, the CBOR channel up to
    /// 64; the row's `kind` says how many the field takes.
    Num(u64),
    /// A string value (a `$03` / `$07` string, or a CBOR text item).
    Text(String),
    /// The values of one `$02` multi-value message, starting at the update's
    /// address. Stream only: the CBOR channel's runs are folded item by item
    /// before they reach the tree.
    Block(Vec<u16>),
}

/// One value on its way into the tree, whichever wire carried it.
///
/// This is the currency of the funnel: [`DeviceState::apply`] and
/// [`DeviceState::apply_cbor`] produce these, [`DeviceState::apply_update`]
/// consumes them, and the conformance vectors drive the funnel with them
/// directly so that each rule is pinned independently of any decoder.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Update {
    /// The wire the value arrived on.
    pub source: Channel,
    /// Whether it was pushed live or is an item of the state dump.
    pub phase: Phase,
    /// The flat address: `page * 128 + number`, or a bare extended address.
    pub address: u32,
    /// The value, as decoded off the wire.
    pub decoded: Decoded,
}

/// The loaded rig's metadata and rig-wide settings.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Rig {
    /// Rig Name (string tag 1).
    pub name: Option<String>,
    /// Rig Author (string tag 2).
    pub author: Option<String>,
    /// Rig Comment (string tag 4).
    pub comment: Option<String>,
    /// Rig Creation Date (string tag 3).
    pub date: Option<String>,
    /// Rig Volume (NRPN `0x04/1`, 14-bit), once seen.
    pub volume: Option<u16>,
    /// Tempo in whole beats per minute (NRPN `0x04/0`, wire value ÷ 64), once
    /// seen.
    pub tempo_bpm: Option<u16>,
}

/// The amplifier block.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Amp {
    /// Amp Name (string tag 10).
    pub name: Option<String>,
    /// On/Off state (NRPN `0x0A/2`), once seen.
    pub on: Option<bool>,
    /// Gain (NRPN `0x0A/4`, 14-bit), once seen.
    pub gain: Option<u16>,
}

/// The cabinet block.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Cabinet {
    /// Cabinet Name (string tag 32).
    pub name: Option<String>,
    /// On/Off state (NRPN `0x0C/2`), once seen.
    pub on: Option<bool>,
}

/// One effect slot's identity and state within the loaded rig.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Effect {
    /// Slot short name in signal-chain order (`"A"`..`"REV"`), from
    /// [`crate::params::effect_slots`].
    pub slot: &'static str,
    /// The slot's address page.
    pub page: u8,
    /// Effect Type value (effect number 0), if known; resolve with
    /// [`Effect::type_name`].
    pub kind: Option<u16>,
    /// On/Off state (effect number 3), if known.
    pub on: Option<bool>,
    /// Dry/wet Mix (effect number 4, 14-bit), if known.
    pub mix: Option<u16>,
}

impl Effect {
    /// The effect Type's human name via [`crate::params::effect_type_name`], or
    /// `None` if the type is unknown or unmapped.
    pub fn type_name(&self) -> Option<&'static str> {
        params::effect_type_name(self.kind?)
    }

    /// The effect Type's category via [`crate::params::effect_category_name`],
    /// or `None` if the type is unknown or in no block.
    pub fn category_name(&self) -> Option<&'static str> {
        params::effect_category_name(self.kind?)
    }

    /// True if the slot holds no effect (Type == 0, "empty").
    pub fn is_empty(&self) -> bool {
        self.kind == Some(0)
    }
}

/// The tuner readout.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Tuner {
    /// Detected note index (NRPN `0x7D/0x54`, low 7 bits), once seen.
    pub note: Option<u8>,
    /// Pitch deviance (NRPN `0x7C/0x0F`; 8192 = perfectly in tune), once seen.
    pub deviance: Option<u16>,
}

impl Tuner {
    /// Whether the detected pitch is within the in-tune window (8192 ± 350), or
    /// `None` if no deviance has been seen yet.
    pub fn in_tune(&self) -> Option<bool> {
        let dev = self.deviance?;
        Some(
            (i32::from(dev) - i32::from(generated::TUNER_IN_TUNE_CENTER)).abs()
                <= i32::from(generated::TUNER_IN_TUNE_WINDOW),
        )
    }
}

/// The global output volumes.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Output {
    /// Main Output Volume (NRPN `0x7F/0`, 14-bit), once seen.
    pub main_volume: Option<u16>,
    /// Headphone Output Volume (NRPN `0x7F/1`, 14-bit), once seen. Driven 1:1 by
    /// the physical Master Volume knob.
    pub headphone_volume: Option<u16>,
    /// Monitor Output Volume (NRPN `0x7F/2`, 14-bit), once seen. Driven 1:1 by
    /// the physical Master Volume knob.
    pub monitor_volume: Option<u16>,
}

impl Output {
    /// The physical Master Volume knob's value. The knob is a potentiometer that
    /// drives Headphone (`0x7F/1`) and Monitor (`0x7F/2`) 1:1 under the default
    /// output routing, so report Headphone, falling back to Monitor. This is a
    /// **read-only** readout: the pot has no soft-takeover, so a written value is
    /// authoritative only until the knob next moves.
    pub fn master_volume(&self) -> Option<u16> {
        self.headphone_volume.or(self.monitor_volume)
    }
}

/// One bank slot's preview names — the identity of one of the current bank's
/// five rigs, as shown on the device's rig browser.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct BankSlot {
    /// Rig name in this slot (Bank Preview number 0..4), once seen.
    pub rig_name: Option<String>,
    /// The rig's amp name (Bank Preview number 5..9), once seen.
    pub amp_name: Option<String>,
    /// The rig's cabinet name (Bank Preview number 10..14), once seen.
    pub cabinet_name: Option<String>,
}

/// The loaded bank's five-slot name preview (page `0x96`). The device pushes the
/// whole block on a bank change, and it is readable on demand via
/// [`DeviceModel::refresh_bank`](crate::model::DeviceModel::refresh_bank). Slot
/// index 0 is rig slot 1.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Bank {
    /// The five preview slots, in slot order (index 0 = slot 1).
    pub slots: [BankSlot; crate::generated::BANK_SLOTS],
}

/// The bookkeeping behind dump authority: while a state dump is streaming, a
/// live push to an address is fresher than the dump's copy of it, so the dump's
/// item for any address a live update touched is dropped.
///
/// This is not part of the snapshot's *value*: two trees that differ only here
/// compare equal, so a dump in progress never reads as a state change and no
/// vector's `expect` can see it. The touched set is a `Vec` rather than a
/// `HashSet` because it holds only the handful of addresses that move during
/// the few seconds a dump takes, and the tree stays plain data.
#[derive(Debug, Clone, Default)]
pub(crate) struct DumpGuard {
    /// Between [`DeviceState::begin_dump`] and [`DeviceState::end_dump`].
    pub(crate) active: bool,
    /// Addresses a live update reached while the dump was active.
    pub(crate) touched: Vec<u32>,
}

impl PartialEq for DumpGuard {
    fn eq(&self, _other: &Self) -> bool {
        true
    }
}

impl Eq for DumpGuard {}

/// The immutable device-state snapshot — the store's value type.
///
/// A fresh, cheap-to-clone bag of plain data. The UI reads fields directly
/// (`state.rig.name`, `state.effects[0].on`, …); no field has an accessor. The
/// async [`DeviceModel`](crate::model::DeviceModel) hands out clones of this via
/// [`state`](crate::model::DeviceModel::state) and its coalesced
/// [`subscribe`](crate::model::DeviceModel::subscribe) stream.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DeviceState {
    /// Where the model stands with the device: the summary of
    /// [`channels`](Self::channels).
    pub connection: Connection,
    /// The state of the stream and the control link, as they really are.
    pub channels: Channels,
    /// The loaded rig's metadata and settings.
    pub rig: Rig,
    /// The amplifier block.
    pub amp: Amp,
    /// The cabinet block.
    pub cabinet: Cabinet,
    /// The eight effect slots in signal-chain order (A..REV).
    pub effects: [Effect; 8],
    /// The tuner readout.
    pub tuner: Tuner,
    /// The global output volumes.
    pub output: Output,
    /// The loaded bank's five-slot name preview (page `0x96`).
    pub bank: Bank,
    /// Current bank, 0-based, once known. Kept live by the `$06` Extended
    /// Parameter the device sends at [`generated::CURRENT_BANK_ADDRESS`]
    /// whenever the bank changes — from the front panel as readily as from a
    /// controller — and by the reply to
    /// [`DeviceModel::refresh_position`](crate::model::DeviceModel::refresh_position).
    /// The CBOR channel carries the same address, so a state dump or a live
    /// CBOR push lands here too via [`apply_cbor`](Self::apply_cbor).
    pub current_bank: Option<u16>,
    /// Current rig slot within the bank, 0-based, once known. Same sources as
    /// [`current_bank`](Self::current_bank), at
    /// [`generated::CURRENT_RIG_SLOT_ADDRESS`]; slot 0 is rig slot 1.
    pub current_rig_slot: Option<u16>,
    /// Latest morph position (0 = base, 16383 = fully morphed), once seen (NRPN
    /// `0x00/0x77`).
    ///
    /// Filled only over the CBOR control link — its state dump, then its live
    /// pushes — because the position never appears on the MIDI3 stream. A model
    /// whose control link is off, unavailable or lost never learns it, so this
    /// stays `None` (or goes stale) there; `channels.control` says which.
    pub morph: Option<u16>,
    /// The most recent realtime status / meter frame (the FAST lane).
    pub status: RealtimeStatus,
    /// Dump-authority bookkeeping; never part of the snapshot's value.
    pub(crate) dump: DumpGuard,
}

impl DeviceState {
    /// The flat, 0-based rig index — `current_bank * BANK_SLOTS +
    /// current_rig_slot` — once both halves are known.
    ///
    /// This is the device's own numbering, and the only address that can name a
    /// rig outside the current bank.
    pub fn current_rig_index(&self) -> Option<u16> {
        Some(self.current_bank? * generated::BANK_SLOTS as u16 + self.current_rig_slot?)
    }

    /// A fresh, empty state: [`Connection::Disconnected`], no rig data, all eight
    /// effect slots seeded from [`crate::params::effect_slots`], zeroed meters.
    pub fn new() -> Self {
        let effects = std::array::from_fn(|i| {
            let (slot, page) = generated::EFFECT_SLOTS[i];
            Effect {
                slot,
                page,
                kind: None,
                on: None,
                mix: None,
            }
        });
        DeviceState {
            connection: Connection::Disconnected,
            channels: Channels::default(),
            rig: Rig::default(),
            amp: Amp::default(),
            cabinet: Cabinet::default(),
            effects,
            tuner: Tuner::default(),
            output: Output::default(),
            bank: Bank::default(),
            current_bank: None,
            current_rig_slot: None,
            morph: None,
            status: RealtimeStatus::default(),
            dump: DumpGuard::default(),
        }
    }

    /// The effect slot named `slot` (case-insensitive: `"a"`, `"REV"`, `"dly"`…),
    /// or `None` if the name is not one of A/B/C/D/X/MOD/DLY/REV.
    pub fn effect(&self, slot: &str) -> Option<&Effect> {
        self.effects
            .iter()
            .find(|e| e.slot.eq_ignore_ascii_case(slot))
    }
}

impl Default for DeviceState {
    fn default() -> Self {
        Self::new()
    }
}

// ---------------------------------------------------------------------------
// The decoders: bytes and items in, `Update`s out, everything through the funnel.
// ---------------------------------------------------------------------------

/// The pure, network-free decoders for [`DeviceState`] — the testable core.
///
/// Each entry point turns what its wire carries into one or more [`Update`]s
/// and hands them to [`apply_update`](Self::apply_update). None of them knows
/// which addresses the tree tracks; that is the routing table's job, and
/// keeping the decoders ignorant of it is what makes the two wires agree.
impl DeviceState {
    /// Parse and apply ONE decoded MIDI message, returning the [`ApplyOutcome`]
    /// (granular events + slow-changed flag). This is pure: no IO, no clock, no
    /// allocation beyond the event vector. Non-Kemper or ignored messages return
    /// an empty outcome.
    ///
    /// Decoding, by function:
    /// - `$01` single-param → a [`Decoded::Num`] at `page * 128 + number`.
    /// - `$02` multi-param → a [`Decoded::Block`] at the same address; the
    ///   funnel takes the meter frame as one unit and folds any other block
    ///   element by element (the rig-load dump).
    /// - `$03` string → a [`Decoded::Text`] at `page * 128 + number`.
    /// - `$06` extended param / `$07` extended string → the same, at the
    ///   5×7-bit address they carry (the device's position, and the strings the
    ///   rig-load dump sends in the extended form).
    /// - `$3C` rendered-string reply → a transient `RenderedString` event with a
    ///   value's exact display text. FAST, and never routed: it is the answer to
    ///   [`DeviceModel::request_render`](crate::model::DeviceModel::request_render),
    ///   not a parameter.
    /// - Anything else → ignored.
    pub fn apply(&mut self, msg: &[u8]) -> ApplyOutcome {
        match decode_stream(msg) {
            StreamMessage::Update(update) => self.apply_update(&update),
            StreamMessage::Rendered {
                page,
                number,
                value,
                text,
            } => ApplyOutcome::fast(DeviceEvent::RenderedString {
                page,
                number,
                value,
                text,
            }),
            StreamMessage::Ignored => ApplyOutcome::empty(),
        }
    }

    /// Fold one **CBOR** numeric address/value into the tree.
    ///
    /// The CBOR channel and the MIDI3 stream are two wire formats over one event
    /// universe, so a value arriving either way lands in the same field and
    /// raises the same event. This is the live-push decoder for a control-link
    /// item; a state dump's items go through [`apply_update`](Self::apply_update)
    /// tagged [`Phase::Dump`] so the dump cannot overwrite a fresher live value.
    ///
    /// The channel's integers are signed; no tracked row holds a negative value,
    /// so a negative is dropped here rather than reinterpreted.
    pub fn apply_cbor(&mut self, address: u32, value: i64) -> ApplyOutcome {
        let Ok(value) = u64::try_from(value) else {
            return ApplyOutcome::empty();
        };
        self.apply_update(&Update {
            source: Channel::Control,
            phase: Phase::Live,
            address,
            decoded: Decoded::Num(value),
        })
    }

    /// Fold one **CBOR** string address/text into the tree — the rig strings and
    /// the bank preview, which the control channel carries as text items at the
    /// same flat addresses the `$03` / `$07` strings use.
    pub fn apply_cbor_text(&mut self, address: u32, text: &str) -> ApplyOutcome {
        self.apply_update(&Update {
            source: Channel::Control,
            phase: Phase::Live,
            address,
            decoded: Decoded::Text(text.to_string()),
        })
    }

    /// Start a state dump: from here until [`end_dump`](Self::end_dump), a live
    /// update marks its address, and a [`Phase::Dump`] item for a marked address
    /// is dropped, because the dump's copy predates the push.
    pub fn begin_dump(&mut self) {
        self.dump.active = true;
        self.dump.touched.clear();
    }

    /// End a state dump and forget which addresses moved during it. Afterwards
    /// a [`Phase::Dump`] item folds exactly like a live one.
    pub fn end_dump(&mut self) {
        self.dump.active = false;
        self.dump.touched.clear();
    }
}

/// What one unframed MIDI3 message decodes to, before the tree sees it.
///
/// [`DeviceState::apply`] is the decode followed by the fold; the model's core
/// runs the two halves itself so that it can see the address a message
/// carries and settle a pending request on it, which the folded
/// [`ApplyOutcome`] alone cannot tell it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum StreamMessage {
    /// A value for the funnel: source [`Channel::Stream`], phase [`Phase::Live`].
    Update(Update),
    /// A `$3C` rendered-string reply: a transient event, never routed.
    Rendered {
        page: u8,
        number: u8,
        value: u16,
        text: String,
    },
    /// Non-Kemper, malformed, or a function the tree does not decode.
    Ignored,
}

/// Decode one unframed MIDI3 message. See [`DeviceState::apply`] for the
/// per-function rules; this is the half of it that touches no state.
pub(crate) fn decode_stream(msg: &[u8]) -> StreamMessage {
    let Some((h, vals)) = NrpnHeader::parse(msg) else {
        return StreamMessage::Ignored;
    };
    let address = u32::from(h.page) * 128 + u32::from(h.number);
    let live = |address, decoded| {
        StreamMessage::Update(Update {
            source: Channel::Stream,
            phase: Phase::Live,
            address,
            decoded,
        })
    };
    match h.function {
        // The extended forms carry 5×7-bit fields that do not fit the fixed
        // header layout, so they are parsed off the raw message.
        FUNCTION_EXT_PARAM => match nrpn::parse_extended_param(msg) {
            Some((address, value)) => live(address, Decoded::Num(value)),
            None => StreamMessage::Ignored,
        },
        FUNCTION_EXT_STRING_PARAM => match nrpn::parse_extended_string(msg) {
            Some((address, text)) => live(address, Decoded::Text(text)),
            None => StreamMessage::Ignored,
        },
        FUNCTION_STRING_PARAM => live(address, Decoded::Text(ascii_until_nul(vals))),
        FUNCTION_SINGLE_PARAM => {
            if vals.len() < 2 {
                return StreamMessage::Ignored;
            }
            live(address, Decoded::Num(u64::from(u14(vals[0], vals[1]))))
        }
        // Consecutive 14-bit values from `number` on; a trailing odd byte is
        // ignored, as `nrpn::multi_values` does.
        FUNCTION_MULTI_PARAM => live(
            address,
            Decoded::Block(
                vals.chunks_exact(2)
                    .map(|pair| u14(pair[0], pair[1]))
                    .collect(),
            ),
        ),
        FUNCTION_RENDERED_STRING_REPLY => match nrpn::parse_rendered_string(msg) {
            Some((page, number, value, text)) => StreamMessage::Rendered {
                page,
                number,
                value,
                text,
            },
            None => StreamMessage::Ignored,
        },
        _ => StreamMessage::Ignored,
    }
}

/// The ASCII payload of a `$03` string up to its NUL terminator.
fn ascii_until_nul(vals: &[u8]) -> String {
    vals.iter()
        .take_while(|&&b| b != 0)
        .map(|&b| b as char)
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::generated::{
        AMP_ON_NUMBER, AMP_PAGE, BEAT_PULSE_NUMBER, EFFECT_PARAM_MIX, EFFECT_PARAM_STATE,
        EFFECT_PARAM_TYPE, GAIN_NUMBER, HEADPHONE_VOLUME_NUMBER, MAIN_VOLUME_NUMBER,
        METER_BLOCK_NUMBER, METER_COUNT, MONITOR_VOLUME_NUMBER, MORPH_BUTTON_NUMBER, MORPH_NUMBER,
        PAGE_BANK_PREVIEW, PAGE_MORPH, PAGE_REALTIME, PAGE_RIG_SETTINGS, PAGE_STRINGS,
        PAGE_TUNER_NOTE, RIG_VOLUME_NUMBER, SYSTEM_PAGE, TEMPO_NUMBER, TUNER_DEVIANCE_NUMBER,
        TUNER_NOTE_NUMBER,
    };
    use crate::nrpn::{set_single, sysex, u14_split};

    #[test]
    fn new_seeds_eight_slots_in_order() {
        let s = DeviceState::new();
        assert_eq!(s.connection, Connection::Disconnected);
        assert_eq!(s.effects.len(), 8);
        assert_eq!(s.effects[0].slot, "A");
        assert_eq!(s.effects[0].page, 0x32);
        assert_eq!(s.effects[7].slot, "REV");
        assert_eq!(s.effects[7].page, 0x3D);
        assert!(
            s.effects
                .iter()
                .all(|e| e.kind.is_none() && e.on.is_none() && e.mix.is_none())
        );
        assert_eq!(s, DeviceState::default());
    }

    #[test]
    fn effect_lookup_is_case_insensitive() {
        let s = DeviceState::new();
        assert_eq!(s.effect("rev").map(|e| e.slot), Some("REV"));
        assert_eq!(s.effect("A").map(|e| e.page), Some(0x32));
        assert!(s.effect("nope").is_none());
    }

    #[test]
    fn effect_type_name_and_empty() {
        let mut e = DeviceState::new().effects[7];
        assert!(e.type_name().is_none());
        e.kind = Some(0);
        assert!(e.is_empty());
        e.kind = Some(179);
        assert!(!e.is_empty());
        assert_eq!(e.type_name(), Some("Easy Reverb"));
        assert_eq!(e.category_name(), Some("Reverb"));
    }

    #[test]
    fn tuner_in_tune_window() {
        let mut t = Tuner::default();
        assert_eq!(t.in_tune(), None);
        t.deviance = Some(8192);
        assert_eq!(t.in_tune(), Some(true));
        t.deviance = Some(8192 + 350);
        assert_eq!(t.in_tune(), Some(true));
        t.deviance = Some(8192 + 351);
        assert_eq!(t.in_tune(), Some(false));
        t.deviance = Some(0);
        assert_eq!(t.in_tune(), Some(false));
    }

    /// A dump in progress is bookkeeping, not state: it must never make two
    /// otherwise-identical snapshots compare unequal, or the store would
    /// republish on nothing.
    #[test]
    fn dump_bookkeeping_is_invisible_to_equality() {
        let mut a = DeviceState::new();
        let b = DeviceState::new();
        a.begin_dump();
        a.apply_cbor(generated::CURRENT_BANK_ADDRESS, 70_000); // dropped, marks nothing
        assert_eq!(a, b);
        a.end_dump();
        assert_eq!(a, b);
    }

    // ---- the stream decoder ------------------------------------------------

    /// Build an 11×14-bit value block with the given values (missing = 0).
    fn meter_block(values: &[u16; METER_COUNT]) -> Vec<u8> {
        let mut out = Vec::with_capacity(22);
        for &v in values {
            let (m, l) = u14_split(v);
            out.push(m);
            out.push(l);
        }
        out
    }

    #[test]
    fn rig_name_string_updates_and_signals_rig_change() {
        let mut st = DeviceState::new();
        // $03 string reply, page 0, number 1 (Rig Name).
        let msg = sysex(
            0x00,
            0x7F,
            FUNCTION_STRING_PARAM,
            PAGE_STRINGS,
            1,
            b"AC30\x00",
        );
        let out = st.apply(&msg);
        assert_eq!(st.rig.name.as_deref(), Some("AC30"));
        assert!(out.slow_changed);
        assert_eq!(
            out.events,
            vec![
                DeviceEvent::StringTag { number: 1 },
                DeviceEvent::RigChanged
            ]
        );

        // A non-name tag (Author) does not signal a rig change but is still slow.
        let msg = sysex(0x00, 0x7F, FUNCTION_STRING_PARAM, PAGE_STRINGS, 2, b"Aaron");
        let out = st.apply(&msg);
        assert_eq!(st.rig.author.as_deref(), Some("Aaron"));
        assert!(out.slow_changed);
        assert_eq!(out.events, vec![DeviceEvent::StringTag { number: 2 }]);

        // Amp Name (tag 10) and Cabinet Name (tag 32) route to their blocks.
        st.apply(&sysex(
            0x00,
            0x7F,
            FUNCTION_STRING_PARAM,
            PAGE_STRINGS,
            generated::STRING_AMP_NAME,
            b"JCM",
        ));
        st.apply(&sysex(
            0x00,
            0x7F,
            FUNCTION_STRING_PARAM,
            PAGE_STRINGS,
            32,
            b"412",
        ));
        assert_eq!(st.amp.name.as_deref(), Some("JCM"));
        assert_eq!(st.cabinet.name.as_deref(), Some("412"));

        // An untracked string tag has no row, and a text with no row is silent:
        // the snapshot is unchanged and there is no generic string event.
        let out = st.apply(&sysex(
            0x00,
            0x7F,
            FUNCTION_STRING_PARAM,
            PAGE_STRINGS,
            99,
            b"x",
        ));
        assert_eq!(out, ApplyOutcome::empty());
    }

    #[test]
    fn effect_type_state_and_mix_fold_into_slot() {
        let mut st = DeviceState::new();
        // REV (page 0x3D) Type 179 = Easy Reverb; slot index 7.
        let out = st.apply(&set_single(0x00, 0x7F, 0x3D, EFFECT_PARAM_TYPE, 179));
        assert_eq!(out.events, vec![DeviceEvent::EffectChanged { slot: 7 }]);
        assert!(out.slow_changed);
        let rev = &st.effects[7];
        assert_eq!(rev.kind, Some(179));
        assert_eq!(rev.type_name(), Some("Easy Reverb"));

        // On/Off = number 3.
        let out = st.apply(&set_single(0x00, 0x7F, 0x3D, EFFECT_PARAM_STATE, 1));
        assert_eq!(out.events, vec![DeviceEvent::EffectChanged { slot: 7 }]);
        assert_eq!(st.effects[7].on, Some(true));

        // Mix = number 4.
        let out = st.apply(&set_single(0x00, 0x7F, 0x3D, EFFECT_PARAM_MIX, 8192));
        assert_eq!(out.events, vec![DeviceEvent::EffectChanged { slot: 7 }]);
        assert!(out.slow_changed);
        assert_eq!(st.effects[7].mix, Some(8192));
        assert_eq!(st.effect("rev").and_then(|e| e.mix), Some(8192));
    }

    #[test]
    fn meter_block_fills_status_and_is_fast() {
        let mut st = DeviceState::new();
        let mut vals = [0u16; METER_COUNT];
        vals[3] = 4096; // strobe phase
        vals[4] = 9000; // stack level
        vals[6] = 12000; // rig out level
        vals[9] = 3000; // loudness
        let msg = sysex(
            0x00,
            0x00,
            FUNCTION_MULTI_PARAM,
            PAGE_REALTIME,
            METER_BLOCK_NUMBER,
            &meter_block(&vals),
        );
        let out = st.apply(&msg);
        assert_eq!(out.events, vec![DeviceEvent::Status(st.status)]);
        assert!(!out.slow_changed); // FAST lane: no snapshot needed.
        assert_eq!(st.status.strobe_phase(), 4096);
        assert_eq!(st.status.stack_level(), 9000);
        assert_eq!(st.status.rig_out_level(), 12000);
        assert_eq!(st.status.loudness(), 3000);
        assert_eq!(st.status.raw, vals);
    }

    #[test]
    fn beat_pulse_is_fast_and_touches_nothing() {
        let mut st = DeviceState::new();
        let before = st.clone();
        let msg = set_single(0x00, 0x00, PAGE_REALTIME, BEAT_PULSE_NUMBER, 16383);
        let out = st.apply(&msg);
        assert_eq!(out.events, vec![DeviceEvent::BeatPulse { on: true }]);
        assert!(!out.slow_changed);
        assert_eq!(st, before);

        let msg = set_single(0x00, 0x00, PAGE_REALTIME, BEAT_PULSE_NUMBER, 0);
        let out = st.apply(&msg);
        assert_eq!(out.events, vec![DeviceEvent::BeatPulse { on: false }]);
        assert!(!out.slow_changed);
    }

    #[test]
    fn tempo_and_rig_volume_set_fields() {
        let mut st = DeviceState::new();
        // 120 BPM encodes as 120 * 64 = 7680.
        let out = st.apply(&set_single(
            0x00,
            0x00,
            PAGE_RIG_SETTINGS,
            TEMPO_NUMBER,
            7680,
        ));
        assert_eq!(out.events, vec![DeviceEvent::TempoBpm(120)]);
        assert!(out.slow_changed);
        assert_eq!(st.rig.tempo_bpm, Some(120));

        let out = st.apply(&set_single(
            0x00,
            0x00,
            PAGE_RIG_SETTINGS,
            RIG_VOLUME_NUMBER,
            4096,
        ));
        assert!(out.slow_changed);
        assert_eq!(st.rig.volume, Some(4096));
    }

    #[test]
    fn amp_on_and_gain_set_fields() {
        let mut st = DeviceState::new();
        let out = st.apply(&set_single(0x00, 0x00, AMP_PAGE, AMP_ON_NUMBER, 1));
        assert!(out.slow_changed);
        assert_eq!(st.amp.on, Some(true));

        let out = st.apply(&set_single(0x00, 0x00, AMP_PAGE, GAIN_NUMBER, 5000));
        assert!(out.slow_changed);
        assert_eq!(st.amp.gain, Some(5000));
        assert_eq!(
            out.events,
            vec![DeviceEvent::ParamChanged {
                page: 0x0A,
                number: 4,
                value: 5000
            }]
        );
    }

    #[test]
    fn output_volumes_set_fields() {
        let mut st = DeviceState::new();
        let out = st.apply(&set_single(
            0x00,
            0x00,
            SYSTEM_PAGE,
            MAIN_VOLUME_NUMBER,
            9000,
        ));
        assert!(out.slow_changed);
        assert_eq!(st.output.main_volume, Some(9000));

        let out = st.apply(&set_single(
            0x00,
            0x00,
            SYSTEM_PAGE,
            MONITOR_VOLUME_NUMBER,
            3000,
        ));
        assert!(out.slow_changed);
        assert_eq!(st.output.monitor_volume, Some(3000));
    }

    #[test]
    fn untracked_generic_param_is_not_slow() {
        let mut st = DeviceState::new();
        // Input Section Noise Gate Intensity (page 0x09/3): not modelled.
        let out = st.apply(&set_single(0x00, 0x00, 0x09, 3, 5000));
        assert!(!out.slow_changed);
        assert_eq!(
            out.events,
            vec![DeviceEvent::ParamChanged {
                page: 0x09,
                number: 3,
                value: 5000
            }]
        );
    }

    #[test]
    fn rig_load_dump_routes_multiple_values() {
        let mut st = DeviceState::new();
        // A $02 dump for effect slot A (page 0x32) starting at number 0:
        // num0 = Type (33 = Green Scream), num1/num2 arbitrary, num3 = On.
        let vals = [
            u14_split(33).0,
            u14_split(33).1, // number 0: Type
            0x10,
            0x00, // number 1: some param
            0x20,
            0x00, // number 2: some param
            0x00,
            0x01, // number 3: On/Off = 1
        ];
        let msg = sysex(0x00, 0x00, FUNCTION_MULTI_PARAM, 0x32, 0, &vals);
        let out = st.apply(&msg);
        assert!(out.slow_changed);
        assert_eq!(
            out.events,
            vec![
                DeviceEvent::EffectChanged { slot: 0 },
                DeviceEvent::ParamChanged {
                    page: 0x32,
                    number: 1,
                    value: u14(0x10, 0x00)
                },
                DeviceEvent::ParamChanged {
                    page: 0x32,
                    number: 2,
                    value: u14(0x20, 0x00)
                },
                DeviceEvent::EffectChanged { slot: 0 },
            ]
        );
        assert_eq!(st.effects[0].kind, Some(33));
        assert_eq!(st.effects[0].type_name(), Some("Green Scream"));
        assert_eq!(st.effects[0].on, Some(true));
    }

    #[test]
    fn non_kemper_message_is_ignored() {
        let mut st = DeviceState::new();
        assert_eq!(st.apply(&[0xB0, 0x20, 0x01]), ApplyOutcome::empty()); // a plain CC
        assert_eq!(st.apply(&[]), ApplyOutcome::empty());
    }

    /// Build a `$07` Extended String Parameter message mirroring the layout
    /// [`nrpn::parse_extended_string`] expects: a 5×7-bit big-endian address of
    /// `page * 128 + number`, then NUL-terminated ASCII.
    fn ext_string(page: u8, number: u8, text: &[u8]) -> Vec<u8> {
        let addr = (page as u64) * 128 + number as u64;
        let mut msg = vec![
            0xF0,
            0x00,
            0x20,
            0x33,
            0x00,
            0x00,
            FUNCTION_EXT_STRING_PARAM,
            0x00,
        ];
        msg.extend_from_slice(&nrpn::ext_encode(addr, 5));
        msg.extend_from_slice(text);
        msg.push(0x00);
        msg.push(0xF7);
        msg
    }

    #[test]
    fn ext_string_recovers_amp_name() {
        let mut st = DeviceState::new();
        // Amp Name is string tag 0x10 on the string page (docs/05).
        let msg = ext_string(PAGE_STRINGS, generated::STRING_AMP_NAME, b"JCM800");
        let out = st.apply(&msg);
        assert_eq!(st.amp.name.as_deref(), Some("JCM800"));
        assert!(out.slow_changed);
        assert_eq!(
            out.events,
            vec![DeviceEvent::StringTag {
                number: generated::STRING_AMP_NAME
            }]
        );
    }

    #[test]
    fn ext_string_rig_name_signals_rig_change() {
        let mut st = DeviceState::new();
        let msg = ext_string(PAGE_STRINGS, nrpn::STRING_RIG_NAME, b"AC30");
        let out = st.apply(&msg);
        assert_eq!(st.rig.name.as_deref(), Some("AC30"));
        assert_eq!(
            out.events,
            vec![
                DeviceEvent::StringTag { number: 1 },
                DeviceEvent::RigChanged
            ]
        );
    }

    #[test]
    fn ext_string_off_string_page_is_ignored() {
        let mut st = DeviceState::new();
        // A $07 addressed to a page with no text row is not routed.
        let msg = ext_string(0x0A, 0, b"nope");
        assert_eq!(st.apply(&msg), ApplyOutcome::empty());
    }

    #[test]
    fn ext_string_populates_bank_preview() {
        let mut st = DeviceState::new();
        // Bank Preview page 0x96: number 0 = slot 1 rig name, number 5 = slot 1
        // amp name, number 12 = slot 3 cabinet name.
        let out = st.apply(&ext_string(PAGE_BANK_PREVIEW, 0, b"AC30"));
        assert!(out.slow_changed);
        assert_eq!(out.events, vec![DeviceEvent::BankPreview { number: 0 }]);
        st.apply(&ext_string(PAGE_BANK_PREVIEW, 5, b"Vox AC30TB"));
        st.apply(&ext_string(PAGE_BANK_PREVIEW, 12, b"2x12"));
        assert_eq!(st.bank.slots[0].rig_name.as_deref(), Some("AC30"));
        assert_eq!(st.bank.slots[0].amp_name.as_deref(), Some("Vox AC30TB"));
        assert_eq!(st.bank.slots[2].cabinet_name.as_deref(), Some("2x12"));
        // An out-of-range bank number has no row, so it is ignored.
        assert_eq!(
            st.apply(&ext_string(PAGE_BANK_PREVIEW, 15, b"x")),
            ApplyOutcome::empty()
        );
    }

    #[test]
    fn headphone_volume_feeds_master() {
        let mut st = DeviceState::new();
        assert_eq!(st.output.master_volume(), None);
        // Monitor alone answers for master until headphone is seen.
        st.apply(&set_single(
            0x00,
            0x00,
            SYSTEM_PAGE,
            MONITOR_VOLUME_NUMBER,
            5000,
        ));
        assert_eq!(st.output.master_volume(), Some(5000));
        let out = st.apply(&set_single(
            0x00,
            0x00,
            SYSTEM_PAGE,
            HEADPHONE_VOLUME_NUMBER,
            9000,
        ));
        assert!(out.slow_changed);
        assert_eq!(st.output.headphone_volume, Some(9000));
        // Headphone wins once present.
        assert_eq!(st.output.master_volume(), Some(9000));
    }

    #[test]
    fn morph_sets_position() {
        let mut st = DeviceState::new();
        // Page 0 / number 0x77, half-morphed. The row is the control channel's,
        // but the stream is never refused: if the position ever showed up here
        // it would be as real as anywhere.
        let out = st.apply(&set_single(0x00, 0x00, PAGE_MORPH, MORPH_NUMBER, 8192));
        assert_eq!(out.events, vec![DeviceEvent::MorphChanged(8192)]);
        assert!(out.slow_changed);
        assert_eq!(st.morph, Some(8192));
    }

    #[test]
    fn morph_button_is_momentary() {
        let mut st = DeviceState::new();
        let press = st.apply(&set_single(0x00, 0x00, PAGE_MORPH, MORPH_BUTTON_NUMBER, 1));
        assert_eq!(press.events, vec![DeviceEvent::MorphButton(true)]);
        assert!(!press.slow_changed, "the button stores nothing");
        let release = st.apply(&set_single(0x00, 0x00, PAGE_MORPH, MORPH_BUTTON_NUMBER, 0));
        assert_eq!(release.events, vec![DeviceEvent::MorphButton(false)]);
        // The button says a morph happened; it never says where the fader sits.
        assert_eq!(st.morph, None);
    }

    #[test]
    fn morph_is_not_at_0x0b() {
        // 0x0B came from a third-party mapping and is wrong: the device answers
        // a request there with a constant 0 whether the rig is morphed or at
        // base, and never pushes it. Nothing may land in `morph` from it, or the
        // same silent mistake returns — a value that simply never moves.
        let mut st = DeviceState::new();
        let out = st.apply(&set_single(0x00, 0x00, PAGE_MORPH, 0x0B, 16383));
        assert_eq!(st.morph, None);
        assert!(!out.slow_changed);
        assert_eq!(
            out.events,
            vec![DeviceEvent::ParamChanged {
                page: 0x00,
                number: 0x0B,
                value: 16383
            }]
        );
    }

    /// The two channels are one event universe: a value arriving over CBOR lands
    /// in the same field, and raises the same event, as one arriving over MIDI3,
    /// because both go through the one funnel.
    #[test]
    fn apply_cbor_routes_the_same_as_the_stream() {
        let mut st = DeviceState::new();
        let morph = st.apply_cbor(generated::MORPH_ADDRESS, 8192);
        assert_eq!(morph.events, vec![DeviceEvent::MorphChanged(8192)]);
        assert!(morph.slow_changed);
        assert_eq!(st.morph, Some(8192));

        let bank = st.apply_cbor(generated::CURRENT_BANK_ADDRESS, 3);
        assert_eq!(
            bank.events,
            vec![DeviceEvent::CurrentPosition {
                bank: Some(3),
                slot: None
            }]
        );
        let slot = st.apply_cbor(generated::CURRENT_RIG_SLOT_ADDRESS, 4);
        assert_eq!(
            slot.events,
            vec![DeviceEvent::CurrentPosition {
                bank: Some(3),
                slot: Some(4)
            }]
        );
        assert_eq!(st.current_rig_index(), Some(19));

        // An unchanged value is not a change, and an unknown address is ignored.
        assert_eq!(
            st.apply_cbor(generated::MORPH_ADDRESS, 8192),
            ApplyOutcome::empty()
        );
        assert_eq!(st.apply_cbor(102_405, 31), ApplyOutcome::empty());
        // A value too wide for the row is dropped, not truncated — and so is a
        // negative one, which no tracked row can hold.
        assert_eq!(
            st.apply_cbor(generated::MORPH_ADDRESS, 70_000),
            ApplyOutcome::empty()
        );
        assert_eq!(
            st.apply_cbor(generated::MORPH_ADDRESS, -1),
            ApplyOutcome::empty()
        );
        assert_eq!(st.morph, Some(8192));

        // Text takes the same road: the rig name over CBOR is the rig name.
        let name = st.apply_cbor_text(u32::from(nrpn::STRING_RIG_NAME), "AC30");
        assert_eq!(
            name.events,
            vec![
                DeviceEvent::StringTag { number: 1 },
                DeviceEvent::RigChanged
            ]
        );
        assert_eq!(st.rig.name.as_deref(), Some("AC30"));
    }

    #[test]
    fn tuner_deviance_is_fast() {
        let mut st = DeviceState::new();
        // Page 0x7C / number 0x0F; 8192 = in tune. FAST lane.
        let out = st.apply(&set_single(
            0x00,
            0x00,
            PAGE_REALTIME,
            TUNER_DEVIANCE_NUMBER,
            8192,
        ));
        assert_eq!(out.events, vec![DeviceEvent::TunerDeviance(8192)]);
        assert!(!out.slow_changed);
        assert_eq!(st.tuner.deviance, Some(8192));
        assert_eq!(st.tuner.in_tune(), Some(true));
    }

    #[test]
    fn tuner_note_is_slow() {
        let mut st = DeviceState::new();
        // Page 0x7D / number 0x54; low 7 bits are the note index.
        let out = st.apply(&set_single(
            0x00,
            0x00,
            PAGE_TUNER_NOTE,
            TUNER_NOTE_NUMBER,
            45,
        ));
        assert_eq!(out.events, vec![DeviceEvent::TunerNote(45)]);
        assert!(out.slow_changed);
        assert_eq!(st.tuner.note, Some(45));
    }

    #[test]
    fn rendered_string_reply_is_fast_event() {
        let mut st = DeviceState::new();
        // A $3C reply for page 0x3C number 53, value 8192, rendered "<0.0>".
        let (msb, lsb) = u14_split(8192);
        let msg = sysex(
            0x02,
            0x7F,
            FUNCTION_RENDERED_STRING_REPLY,
            0x3C,
            53,
            &[msb, lsb, b'<', b'0', b'.', b'0', b'>', 0],
        );
        let out = st.apply(&msg);
        assert!(!out.slow_changed); // transient reply: no snapshot.
        assert_eq!(
            out.events,
            vec![DeviceEvent::RenderedString {
                page: 0x3C,
                number: 53,
                value: 8192,
                text: "<0.0>".to_string(),
            }]
        );
    }
}
