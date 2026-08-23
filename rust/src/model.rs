//! `DeviceModel` — a **React-style store** over a Profiler's live stream: a
//! typed immutable [`DeviceState`] snapshot bag the UI binds to, plus a command
//! channel for writes.
//!
//! Two layers:
//!
//! - [`DeviceState`] (in [`crate::state`]) is the **pure, network-free core**: a
//!   plain-data tree — rig, amp, cabinet, the eight effect slots, tuner, output,
//!   morph, and the latest [`RealtimeStatus`] meter frame. It decodes one
//!   already-unframed MIDI message at a time via [`DeviceState::apply`],
//!   returning an [`ApplyOutcome`] (the granular [`DeviceEvent`]s produced, plus
//!   whether any *slow* field changed). It does no IO, so unit tests drive it
//!   with synthesized messages.
//!
//! - [`DeviceModel`] is the **async handle** (cheap to `Clone`; an `Arc` inside).
//!   [`DeviceModel::connect`] opens a [`Session`], spawns an ingest task that owns
//!   the socket and an [`Unframer`], and kicks off a read-only initial sync. The
//!   task applies each decoded message to a shared [`DeviceState`].
//!
//! # This is a store
//!
//! Three access points, mirroring a React store:
//!
//! - [`state`](DeviceModel::state) — `getState`: the current snapshot.
//! - [`subscribe`](DeviceModel::subscribe) — the **store**: a fresh snapshot is
//!   emitted only when *slow* state changed, coalesced to at most once per
//!   ingested stream chunk.
//! - [`events`](DeviceModel::events) — the granular delta stream (every
//!   [`DeviceEvent`], including the fast ones), for callers that want deltas.
//! - [`status`](DeviceModel::status) — the **fast lane**: poll this per
//!   animation frame for the meter/tuner frame (equals `state().status`).
//!
//! State is classified FAST vs SLOW. **FAST** = the meter [`RealtimeStatus`]
//! block, the beat pulse, and tuner deviance — high-rate, poll them via
//! [`status`](DeviceModel::status). **SLOW** = everything else (rig / amp / cab /
//! effect / output / tempo / morph / tuner-note / connection): these drive the
//! coalesced [`subscribe`](DeviceModel::subscribe) snapshot.
//!
//! # Parameters vs Actions
//!
//! [`DeviceModel`]'s command methods split into two groups:
//!
//! - **Parameters** (`set_*`) — settable values the device stores. They go out
//!   as 14-bit NRPN `$01` Single Parameter Changes (via
//!   [`crate::nrpn::set_single`]); the device applies the write silently and
//!   does *not* echo it back on the stream, so follow a set with
//!   [`request_param`](DeviceModel::request_param) when [`DeviceModel::state`]
//!   should confirm the new value — the `$41` reply flows through normal
//!   ingest. The setters are:
//!   [`set_gain`](DeviceModel::set_gain),
//!   [`set_rig_volume`](DeviceModel::set_rig_volume),
//!   [`set_main_volume`](DeviceModel::set_main_volume),
//!   [`set_monitor_volume`](DeviceModel::set_monitor_volume),
//!   [`set_effect_enabled`](DeviceModel::set_effect_enabled),
//!   [`set_effect_mix`](DeviceModel::set_effect_mix),
//!   [`set_tempo_bpm`](DeviceModel::set_tempo_bpm), and the escape hatch
//!   [`set_param`](DeviceModel::set_param).
//! - **Actions** (verbs) — momentary presses and live expression that carry no
//!   stored value. They go out as 7-bit Control Change messages via the
//!   [`crate::control`] vocabulary and are *not* reflected in state:
//!   [`select_rig`](DeviceModel::select_rig),
//!   [`rig_up`](DeviceModel::rig_up), [`rig_down`](DeviceModel::rig_down),
//!   [`bank`](DeviceModel::bank), [`tap_tempo`](DeviceModel::tap_tempo),
//!   [`tuner_mode`](DeviceModel::tuner_mode), the buttons and pedals, and the
//!   escape hatch [`send_control`](DeviceModel::send_control).
//!
//! Power users reach the full raw vocabulary through
//! [`send_control`](DeviceModel::send_control) and the [`crate::control`]
//! module; any address at all through [`set_param`](DeviceModel::set_param).

use std::net::Ipv4Addr;
use std::sync::{Arc, RwLock};
use std::time::Duration;

use tokio::sync::{broadcast, mpsc};

use crate::control::{self, Control};
use crate::error::SessionError;
use crate::generated;
use crate::midi3::{self, Unframer};
use crate::nrpn::{
    self, FUNCTION_EXT_PARAM, FUNCTION_EXT_STRING_PARAM, FUNCTION_MULTI_PARAM,
    FUNCTION_RENDERED_STRING_REPLY, FUNCTION_SINGLE_PARAM, FUNCTION_STRING_PARAM, NrpnHeader,
    PAGE_STRINGS, multi_values, parse_extended_param, request_extended_param,
    request_extended_string, request_rendered_string, request_single, request_string, set_single,
    u14,
};
use crate::params::{self, EFFECT_PARAM_MIX, EFFECT_PARAM_STATE, EFFECT_PARAM_TYPE};
use crate::session::{PROTOCOL_MIDI3_STREAM, Session};
use crate::state::{Connection, DeviceState, Effect};

/// Volatile realtime page (meter block, beat pulse, tuner deviance).
const PAGE_REALTIME: u8 = generated::PAGE_REALTIME;
/// First NRPN number of the 11-value meter block on [`PAGE_REALTIME`].
const METER_NUMBER: u8 = generated::METER_BLOCK_NUMBER;
/// Beat-pulse number on [`PAGE_REALTIME`] (`$01`, toggles 0/16383).
const BEAT_PULSE_NUMBER: u8 = generated::BEAT_PULSE_NUMBER;
/// Rig Settings page (holds Tempo bpm at number 0, Rig Volume at number 1).
const PAGE_RIG_SETTINGS: u8 = generated::PAGE_RIG_SETTINGS;
/// Tempo bpm number on [`PAGE_RIG_SETTINGS`]; the value is `bpm * 64`.
const TEMPO_NUMBER: u8 = generated::TEMPO_NUMBER;
/// Rig Volume number on [`PAGE_RIG_SETTINGS`] (14-bit).
const RIG_VOLUME_NUMBER: u8 = generated::RIG_VOLUME_NUMBER;
/// Fixed-point scale of the Tempo bpm parameter: the wire value is `bpm * 64`.
const TEMPO_BPM_SCALE: u16 = generated::TEMPO_BPM_SCALE;
/// Amplifier page (holds On/Off at number 2, Gain at number 4).
const AMP_PAGE: u8 = generated::AMP_PAGE;
/// On/Off number on [`AMP_PAGE`] (`$01`, 0 = off, 1 = on).
const AMP_ON_NUMBER: u8 = generated::AMP_ON_NUMBER;
/// Gain number on [`AMP_PAGE`] (14-bit).
const GAIN_NUMBER: u8 = generated::GAIN_NUMBER;
/// System/Global page (holds Main/Monitor output volumes).
const SYSTEM_PAGE: u8 = generated::SYSTEM_PAGE;
/// Main Output Volume number on [`SYSTEM_PAGE`] (14-bit).
const MAIN_VOL_NUMBER: u8 = generated::MAIN_VOLUME_NUMBER;
/// Headphone Output Volume number on [`SYSTEM_PAGE`] (14-bit); one half of the
/// physical Master Volume knob.
const HEADPHONE_VOL_NUMBER: u8 = generated::HEADPHONE_VOLUME_NUMBER;
/// Monitor Output Volume number on [`SYSTEM_PAGE`] (14-bit); the other half.
const MONITOR_VOL_NUMBER: u8 = generated::MONITOR_VOLUME_NUMBER;
/// Bank Preview page (`0x96`): the loaded bank's five-slot name preview.
const PAGE_BANK_PREVIEW: u8 = generated::PAGE_BANK_PREVIEW;
/// The maximum 14-bit NRPN value (0–16383).
const NRPN_MAX: u16 = generated::FULL_SCALE;
/// Morph page: page 0 is dual-use, morph lives at [`MORPH_NUMBER`] and
/// [`MORPH_BUTTON_NUMBER`].
const PAGE_MORPH: u8 = generated::PAGE_MORPH;
/// Morph position on [`PAGE_MORPH`] (`$01`, 0 = base .. 16383 = fully morphed).
const MORPH_NUMBER: u8 = generated::MORPH_NUMBER;
/// Morph button on [`PAGE_MORPH`] (`$01`, 1 on press, 0 on release).
const MORPH_BUTTON_NUMBER: u8 = generated::MORPH_BUTTON_NUMBER;
/// Tuner-deviance number on [`PAGE_REALTIME`] (`$01`; 8192 = in tune).
const TUNER_DEVIANCE_NUMBER: u8 = generated::TUNER_DEVIANCE_NUMBER;
/// Tuner-note page (holds the detected note at [`TUNER_NOTE_NUMBER`]).
const PAGE_TUNER_NOTE: u8 = generated::PAGE_TUNER_NOTE;
/// Tuner-note number on [`PAGE_TUNER_NOTE`] (`$01`, 0x54 = 84).
const TUNER_NOTE_NUMBER: u8 = generated::TUNER_NOTE_NUMBER;
/// How many 14-bit values the realtime status block carries.
const METER_COUNT: usize = generated::METER_COUNT;

/// Product byte addressed in outbound SysEx (0x00 = Profiler).
const PRODUCT: u8 = nrpn::PRODUCT_PROFILER;
/// Device byte addressed in outbound SysEx (0x7F = omni).
const DEVICE: u8 = nrpn::DEVICE_OMNI;
/// MIDI channel used for Control Change commands (0 = channel 1).
const CC_CHANNEL: u8 = 0;
/// Read idle gap driving the ingest loop; short so it reacts per packet.
const READ_IDLE: Duration = Duration::from_millis(30);
/// Max bytes per stream read.
const READ_MAX: usize = 64 * 1024;

/// A decoded realtime status / meter-block frame: the eleven 14-bit values the
/// stream pushes at NRPN `0x7C/78..88` (function `$02`, page `0x7C`, number
/// `0x4E`). The field identities were established by observed experimentation
/// (knob-sweep and pitch-bend correlation against the labelled parameter
/// stream); see the generated `METER_FIELDS` table.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct RealtimeStatus {
    /// The eleven raw 14-bit values (v0..v10) in NRPN order (0x7C/78..88).
    pub raw: [u16; METER_COUNT],
}

impl RealtimeStatus {
    /// Decode an 11×14-bit value block (MSB/LSB pairs) into a status frame.
    /// Missing trailing values default to 0; extra bytes are ignored.
    fn from_values(vals: &[u8]) -> Self {
        let mut raw = [0u16; METER_COUNT];
        for (i, pair) in vals.chunks_exact(2).take(raw.len()).enumerate() {
            raw[i] = u14(pair[0], pair[1]);
        }
        RealtimeStatus { raw }
    }

    /// Tuner strobe phase (v3): a wrapping 0–16383 phase whose rotation rate
    /// tracks pitch deviance (stationary = in tune).
    pub fn strobe_phase(&self) -> u16 {
        self.raw[generated::STROBE_PHASE_INDEX]
    }

    /// The three tuner strobe display-segment drivers (v0/v1/v2).
    pub fn strobe_segments(&self) -> [u16; 3] {
        generated::STROBE_SEGMENT_INDICES.map(|i| self.raw[i])
    }

    /// Stack tap level (v4): pre-rig-volume amplitude.
    pub fn stack_level(&self) -> u16 {
        self.raw[4]
    }

    /// Rig output level (v6): post-rig-volume amplitude.
    pub fn rig_out_level(&self) -> u16 {
        self.raw[6]
    }

    /// Loudness (v9): slow RMS of the output.
    pub fn loudness(&self) -> u16 {
        self.raw[9]
    }
}

/// A typed change emitted by [`DeviceState::apply`] and broadcast by a
/// [`DeviceModel`].
///
/// `#[non_exhaustive]`: new variants may be added as more of the stream is
/// decoded, so downstream `match`es must include a wildcard arm.
///
/// Not `Copy`: the [`RenderedString`](DeviceEvent::RenderedString) variant owns
/// a `String`. Clone it if you need an owned copy.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub enum DeviceEvent {
    /// The loaded rig changed (its Rig Name was (re)applied).
    RigChanged,
    /// A page-0 string tag was applied (`number` per the string-tag table).
    StringTag {
        /// The string-tag number.
        number: u8,
    },
    /// A bank-preview name was applied (page `0x96`): one of the current bank's
    /// rig / amp / cabinet names. Read [`DeviceState::bank`] for the new value.
    BankPreview {
        /// The Bank Preview number (0..14): 0–4 rig, 5–9 amp, 10–14 cabinet.
        number: u8,
    },
    /// An effect slot's Type, On/Off state or Mix changed.
    EffectChanged {
        /// Index into [`crate::params::effect_slots`] (0 = A … 7 = REV).
        slot: usize,
    },
    /// A numeric parameter changed that the model does not model specially.
    ParamChanged {
        /// Address page.
        page: u8,
        /// Address number.
        number: u8,
        /// 14-bit value.
        value: u16,
    },
    /// A new realtime status / meter frame arrived.
    Status(RealtimeStatus),
    /// The tempo/beat pulse toggled (`on` = value != 0).
    BeatPulse {
        /// Pulse edge state.
        on: bool,
    },
    /// The tempo changed, in whole beats per minute.
    TempoBpm(u16),
    /// The morph position changed (`$01` page 0 / number 0x77): 0 = base,
    /// 16383 = fully morphed.
    ///
    /// Only a CBOR session ever sees this: the position is never sent on the
    /// MIDI3 stream, even while a morph is ramping. See [`Self::MorphButton`].
    MorphChanged(u16),
    /// The morph button was pressed (`true`) or released (`false`) — momentary,
    /// so nothing about it is stored in the snapshot.
    ///
    /// This is what a MIDI3 client sees of a morph: *that* one happened, and
    /// what it did to the audio parameters, but never where the fader sits.
    MorphButton(bool),
    /// The tuner pitch deviance changed (`$01` page 0x7C / number 0x0F; 8192 =
    /// in tune).
    TunerDeviance(u16),
    /// The tuner's detected note changed (`$01` page 0x7D / number 0x54; the
    /// low 7 bits are the note index).
    TunerNote(u8),
    /// A rendered-string reply arrived ($3C), carrying a value's exact display
    /// text (e.g. `5.2`, `120 BPM`, `<0.0>`) for the requested address. This is
    /// the transient response to [`DeviceModel::request_render`]; it is not
    /// stored in the snapshot tree.
    RenderedString {
        /// Address page.
        page: u8,
        /// Address number.
        number: u8,
        /// The 14-bit value the string renders.
        value: u16,
        /// The rendered display text.
        text: String,
    },
    /// The device's current position was learned from the CBOR state-dump
    /// snapshot. Read [`DeviceState::current_bank`] and
    /// [`DeviceState::current_rig_slot`] for the new values (both 0-based).
    CurrentPosition {
        /// Current bank, 0-based, if the dump carried it.
        bank: Option<u16>,
        /// Current rig slot within the bank, 0-based, if the dump carried it.
        slot: Option<u16>,
    },
    /// The model connected to a device.
    Connected,
    /// The device closed the connection.
    Disconnected,
}

/// The result of applying one message to a [`DeviceState`]: the granular events
/// it produced, plus whether any **slow** (snapshot) field changed.
///
/// The store uses `slow_changed` to decide whether to emit a fresh snapshot:
/// `true` when a rig / amp / cab / effect / output / tempo / morph / tuner-note /
/// connection field moved, `false` for FAST-only traffic (the meter
/// [`RealtimeStatus`] block, the beat pulse, tuner deviance) and for untracked
/// generic params that leave the snapshot unchanged. The granular
/// [`DeviceEvent`]s are still emitted regardless, for [`DeviceModel::events`].
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ApplyOutcome {
    /// The typed deltas this message produced (in order).
    pub events: Vec<DeviceEvent>,
    /// Whether a slow (snapshot-visible) field changed.
    pub slow_changed: bool,
}

impl ApplyOutcome {
    /// An empty outcome: nothing happened, no slow change.
    fn empty() -> Self {
        ApplyOutcome::default()
    }

    /// One event that changed no slow field (FAST lane or untracked generic).
    fn fast(event: DeviceEvent) -> Self {
        ApplyOutcome {
            events: vec![event],
            slow_changed: false,
        }
    }

    /// Events that changed a slow (snapshot-visible) field.
    fn slow(events: Vec<DeviceEvent>) -> Self {
        ApplyOutcome {
            events,
            slow_changed: true,
        }
    }
}

/// The pure, network-free decode logic for [`DeviceState`] — the testable core.
///
/// Feed already-unframed MIDI messages to [`DeviceState::apply`]; the data shape
/// itself lives in [`crate::state`].
impl DeviceState {
    /// Parse and apply ONE decoded MIDI message, returning the [`ApplyOutcome`]
    /// (granular events + slow-changed flag). This is pure: no IO, no clock, no
    /// allocation beyond the event vector. Non-Kemper or ignored messages return
    /// an empty outcome.
    ///
    /// Routing:
    /// - `$03` string on page 0 → rig/amp/cab name tags + `StringTag` (plus
    ///   `RigChanged` for tag 1, the Rig Name). SLOW.
    /// - `$01` single-param → beat pulse / effect / amp / rig / output / morph /
    ///   tuner / generic `ParamChanged`; FAST for beat pulse and tuner deviance,
    ///   SLOW for the tracked fields.
    /// - `$02` multi-param → meter block (→ `Status`, FAST) or the rig-load dump
    ///   (each consecutive address routed like a single-param).
    /// - `$07` ext-string on the string page → the amp/cab/author tags the
    ///   rig-load dump sends as extended strings (see [`Self::apply_ext_string`]).
    /// - `$06` extended param → the device's current bank / rig slot (see
    ///   [`Self::apply_ext_param`]).
    /// - `$3C` rendered-string reply → a transient `RenderedString` event with a
    ///   value's exact display text (see [`Self::apply_rendered_string`]). FAST.
    /// - Anything else → ignored.
    pub fn apply(&mut self, msg: &[u8]) -> ApplyOutcome {
        // The extended ($06) and extended-string ($07) addresses are 5×7-bit
        // encoded and do not fit the fixed `NrpnHeader` layout, so route both
        // off the raw message first.
        if let Some((h, _)) = NrpnHeader::parse(msg) {
            if h.function == FUNCTION_EXT_PARAM {
                return self.apply_ext_param(msg);
            }
            if h.function == FUNCTION_EXT_STRING_PARAM {
                return self.apply_ext_string(msg);
            }
        }
        let Some((h, vals)) = NrpnHeader::parse(msg) else {
            return ApplyOutcome::empty();
        };
        match h.function {
            FUNCTION_STRING_PARAM if h.page == PAGE_STRINGS => self.apply_string(h.number, vals),
            FUNCTION_SINGLE_PARAM => {
                if vals.len() < 2 {
                    return ApplyOutcome::empty();
                }
                self.route_param(h.page, h.number, u14(vals[0], vals[1]))
            }
            FUNCTION_MULTI_PARAM => self.apply_multi(h.page, h.number, vals),
            FUNCTION_RENDERED_STRING_REPLY => self.apply_rendered_string(msg),
            _ => ApplyOutcome::empty(),
        }
    }

    /// Fold a `$06` Extended Parameter into the tree.
    ///
    /// This is the device's position report. It carries a 5×7-bit address and a
    /// 5×7-bit value; the two addresses that matter here are the current bank
    /// and the current rig slot, both 0-based. The device pushes whichever of
    /// the two changed on every rig change — including changes made at the front
    /// panel — and answers [`DeviceModel::refresh_position`] with both.
    ///
    /// Any other extended address is ignored, and a value too large for the
    /// 16-bit field is dropped rather than truncated into a bogus position.
    fn apply_ext_param(&mut self, msg: &[u8]) -> ApplyOutcome {
        let Some((address, value)) = parse_extended_param(msg) else {
            return ApplyOutcome::empty();
        };
        let Ok(value) = u16::try_from(value) else {
            return ApplyOutcome::empty();
        };
        self.apply_address(address, value)
    }

    /// Fold one **CBOR** address/value into the tree.
    ///
    /// The CBOR channel and the MIDI3 stream are two wire formats over one event
    /// universe, so a value arriving either way lands in the same field and
    /// raises the same event. This is the entry point for a
    /// [`CborSession`](crate::cbor::CborSession)'s live pushes and for a state
    /// dump's items.
    ///
    /// A value outside the 16-bit range is dropped rather than truncated into a
    /// bogus reading.
    pub fn apply_cbor(&mut self, address: u32, value: i64) -> ApplyOutcome {
        let Ok(value) = u16::try_from(value) else {
            return ApplyOutcome::empty();
        };
        self.apply_address(address, value)
    }

    /// Route one flat address to the field that tracks it, whichever transport
    /// carried it. Unknown addresses change nothing.
    fn apply_address(&mut self, address: u32, value: u16) -> ApplyOutcome {
        if address == generated::CURRENT_BANK_ADDRESS {
            if self.current_bank == Some(value) {
                return ApplyOutcome::empty();
            }
            self.current_bank = Some(value);
            ApplyOutcome::slow(vec![DeviceEvent::CurrentPosition {
                bank: Some(value),
                slot: self.current_rig_slot,
            }])
        } else if address == generated::CURRENT_RIG_SLOT_ADDRESS {
            if self.current_rig_slot == Some(value) {
                return ApplyOutcome::empty();
            }
            self.current_rig_slot = Some(value);
            ApplyOutcome::slow(vec![DeviceEvent::CurrentPosition {
                bank: self.current_bank,
                slot: Some(value),
            }])
        } else if address == generated::MORPH_ADDRESS {
            if self.morph == Some(value) {
                return ApplyOutcome::empty();
            }
            self.morph = Some(value);
            ApplyOutcome::slow(vec![DeviceEvent::MorphChanged(value)])
        } else {
            ApplyOutcome::empty()
        }
    }

    /// Route a `$03` page-0 string tag into the tree.
    fn apply_string(&mut self, number: u8, vals: &[u8]) -> ApplyOutcome {
        let text: String = vals
            .iter()
            .take_while(|&&b| b != 0)
            .map(|&b| b as char)
            .collect();
        let tracked = self.apply_string_tag(number, text);
        let mut events = vec![DeviceEvent::StringTag { number }];
        if number == nrpn::STRING_RIG_NAME {
            events.push(DeviceEvent::RigChanged);
        }
        ApplyOutcome {
            events,
            slow_changed: tracked,
        }
    }

    /// Store a decoded page-0 string tag into the matching field by its tag
    /// `number`. Returns `true` if the tag maps to a tracked field (a slow
    /// change), `false` for tags this tree does not model.
    fn apply_string_tag(&mut self, number: u8, text: String) -> bool {
        match number {
            1 => self.rig.name = Some(text),
            2 => self.rig.author = Some(text),
            3 => self.rig.date = Some(text),
            4 => self.rig.comment = Some(text),
            10 => self.amp.name = Some(text),
            32 => self.cabinet.name = Some(text),
            _ => return false,
        }
        true
    }

    /// Route a `$07` Extended String Parameter message — how the rig-load dump
    /// delivers the amp/cab/author metadata that plain `$03` strings omit.
    ///
    /// The extended address decodes as `page * 128 + number`; a string on
    /// [`PAGE_STRINGS`] carries the same tag numbers as `$03`, so it routes via
    /// [`Self::apply_string_tag`]. Other pages are ignored. If
    /// [`nrpn::parse_extended_string`] returns `None`, the message is ignored.
    fn apply_ext_string(&mut self, msg: &[u8]) -> ApplyOutcome {
        let Some((addr, text)) = nrpn::parse_extended_string(msg) else {
            return ApplyOutcome::empty();
        };
        let page = (addr / 128) as u8;
        let number = (addr % 128) as u8;
        if page == PAGE_BANK_PREVIEW {
            return self.apply_bank_preview(number, text);
        }
        if page != PAGE_STRINGS {
            return ApplyOutcome::empty();
        }
        let tracked = self.apply_string_tag(number, text);
        let mut events = vec![DeviceEvent::StringTag { number }];
        if number == nrpn::STRING_RIG_NAME {
            events.push(DeviceEvent::RigChanged);
        }
        ApplyOutcome {
            events,
            slow_changed: tracked,
        }
    }

    /// Store one bank-preview name (page `0x96`) into [`DeviceState::bank`]. The
    /// number selects the field (rig / amp / cabinet) and slot; an out-of-range
    /// number is ignored. A stored value is a SLOW change.
    fn apply_bank_preview(&mut self, number: u8, text: String) -> ApplyOutcome {
        let Some((field, slot)) = params::bank_preview_slot_field(number) else {
            return ApplyOutcome::empty();
        };
        let target = &mut self.bank.slots[slot];
        match field {
            params::BankPreviewField::RigName => target.rig_name = Some(text),
            params::BankPreviewField::AmpName => target.amp_name = Some(text),
            params::BankPreviewField::CabinetName => target.cabinet_name = Some(text),
        }
        ApplyOutcome::slow(vec![DeviceEvent::BankPreview { number }])
    }

    /// Route one decoded numeric address/value pair into the tree. Shared by
    /// `$01` single-param and each address of a `$02` rig-load dump.
    fn route_param(&mut self, page: u8, number: u8, value: u16) -> ApplyOutcome {
        // Beat pulse: volatile, not stored (FAST lane).
        if page == PAGE_REALTIME && number == BEAT_PULSE_NUMBER {
            return ApplyOutcome::fast(DeviceEvent::BeatPulse { on: value != 0 });
        }
        // Tuner pitch deviance: volatile (FAST lane).
        if page == PAGE_REALTIME && number == TUNER_DEVIANCE_NUMBER {
            self.tuner.deviance = Some(value);
            return ApplyOutcome::fast(DeviceEvent::TunerDeviance(value));
        }
        // Effect Type / On-Off / Mix: fold into the slot (SLOW).
        if params::is_effect_page(page)
            && matches!(
                number,
                EFFECT_PARAM_TYPE | EFFECT_PARAM_STATE | EFFECT_PARAM_MIX
            )
        {
            if let Some(slot) = self.apply_effect(page, number, value) {
                return ApplyOutcome::slow(vec![DeviceEvent::EffectChanged { slot }]);
            }
            return ApplyOutcome::empty();
        }
        // Amplifier On/Off and Gain (SLOW).
        if page == AMP_PAGE && number == AMP_ON_NUMBER {
            self.amp.on = Some(value != 0);
            return ApplyOutcome::slow(vec![generic(page, number, value)]);
        }
        if page == AMP_PAGE && number == GAIN_NUMBER {
            self.amp.gain = Some(value);
            return ApplyOutcome::slow(vec![generic(page, number, value)]);
        }
        // Tempo bpm (value is bpm * 64) and Rig Volume (SLOW).
        if page == PAGE_RIG_SETTINGS && number == TEMPO_NUMBER {
            let bpm = value / TEMPO_BPM_SCALE;
            self.rig.tempo_bpm = Some(bpm);
            return ApplyOutcome::slow(vec![DeviceEvent::TempoBpm(bpm)]);
        }
        if page == PAGE_RIG_SETTINGS && number == RIG_VOLUME_NUMBER {
            self.rig.volume = Some(value);
            return ApplyOutcome::slow(vec![generic(page, number, value)]);
        }
        // System output volumes (SLOW).
        if page == SYSTEM_PAGE && number == MAIN_VOL_NUMBER {
            self.output.main_volume = Some(value);
            return ApplyOutcome::slow(vec![generic(page, number, value)]);
        }
        if page == SYSTEM_PAGE && number == HEADPHONE_VOL_NUMBER {
            self.output.headphone_volume = Some(value);
            return ApplyOutcome::slow(vec![generic(page, number, value)]);
        }
        if page == SYSTEM_PAGE && number == MONITOR_VOL_NUMBER {
            self.output.monitor_volume = Some(value);
            return ApplyOutcome::slow(vec![generic(page, number, value)]);
        }
        // Morph (page 0 is dual-use with the string page). The position is SLOW;
        // the button is momentary, so it is an event and nothing more.
        if page == PAGE_MORPH && number == MORPH_BUTTON_NUMBER {
            return ApplyOutcome::fast(DeviceEvent::MorphButton(value != 0));
        }
        if page == PAGE_MORPH && number == MORPH_NUMBER {
            self.morph = Some(value);
            return ApplyOutcome::slow(vec![DeviceEvent::MorphChanged(value)]);
        }
        // Tuner detected note (SLOW).
        if page == PAGE_TUNER_NOTE && number == TUNER_NOTE_NUMBER {
            let note = (value & 0x7F) as u8;
            self.tuner.note = Some(note);
            return ApplyOutcome::slow(vec![DeviceEvent::TunerNote(note)]);
        }
        // Untracked generic parameter: leaves the snapshot unchanged, so it is
        // not a slow change — only the granular event stream sees it.
        ApplyOutcome::fast(generic(page, number, value))
    }

    /// Store a decoded effect-slot parameter into the matching slot: number 0
    /// sets the Type, number 3 sets On/Off (`value != 0`), number 4 sets Mix.
    /// Returns the slot index if the page is an effect slot.
    fn apply_effect(&mut self, page: u8, number: u8, value: u16) -> Option<usize> {
        let idx = params::effect_slot_index(page)?;
        let slot: &mut Effect = &mut self.effects[idx];
        match number {
            EFFECT_PARAM_TYPE => slot.kind = Some(value),
            EFFECT_PARAM_STATE => slot.on = Some(value != 0),
            EFFECT_PARAM_MIX => slot.mix = Some(value),
            _ => {}
        }
        Some(idx)
    }

    /// Route a `$3C` Rendered String reply — the transient response to
    /// [`DeviceModel::request_render`]. It carries a value's exact display text
    /// but is *not* stored in the snapshot tree, so it emits only a granular
    /// `RenderedString` event with `slow_changed = false`. A malformed reply
    /// (rejected by [`nrpn::parse_rendered_string`]) is ignored.
    fn apply_rendered_string(&mut self, msg: &[u8]) -> ApplyOutcome {
        let Some((page, number, value, text)) = nrpn::parse_rendered_string(msg) else {
            return ApplyOutcome::empty();
        };
        ApplyOutcome::fast(DeviceEvent::RenderedString {
            page,
            number,
            value,
            text,
        })
    }

    /// Route a `$02` multi-param message: the meter block, or a rig-load dump.
    fn apply_multi(&mut self, page: u8, number: u8, vals: &[u8]) -> ApplyOutcome {
        if page == PAGE_REALTIME && number == METER_NUMBER {
            self.status = RealtimeStatus::from_values(vals);
            return ApplyOutcome::fast(DeviceEvent::Status(self.status));
        }
        // Rig-load dump: consecutive addresses starting at `number`, each routed
        // exactly like a single-param.
        let mut out = ApplyOutcome::empty();
        for (num, value) in multi_values(number, vals) {
            let step = self.route_param(page, num, value);
            out.events.extend(step.events);
            out.slow_changed |= step.slow_changed;
        }
        out
    }
}

/// Build a generic [`DeviceEvent::ParamChanged`] for an untracked address.
fn generic(page: u8, number: u8, value: u16) -> DeviceEvent {
    DeviceEvent::ParamChanged {
        page,
        number,
        value,
    }
}

/// Error returned when a command cannot be issued.
///
/// `#[non_exhaustive]`: more failure modes may be added, so downstream `match`es
/// must include a wildcard arm.
#[derive(Debug, thiserror::Error)]
#[non_exhaustive]
pub enum CommandError {
    /// The ingest task has ended, so the command channel is closed.
    #[error("device model is disconnected; command channel closed")]
    Disconnected,
    /// An effect-slot name did not match A/B/C/D/X/MOD/DLY/REV.
    #[error("unknown effect slot {0:?}; use A B C D X MOD DLY REV")]
    UnknownSlot(String),
}

/// An async handle to a live [`DeviceState`] store synced from a Profiler's
/// stream.
///
/// Cheap to clone: all clones share one ingest task, state cache, the snapshot
/// and event broadcasts, and the command channel.
///
/// ```no_run
/// use std::net::Ipv4Addr;
/// use libkp::model::DeviceModel;
///
/// # async fn run() -> Result<(), Box<dyn std::error::Error>> {
/// let model = DeviceModel::connect(Ipv4Addr::new(192, 168, 1, 50)).await?;
/// // The store: a fresh snapshot each time slow state changes.
/// let mut snapshots = model.subscribe();
///
/// // Turn the reverb off — a tracked parameter, so the snapshot reflects it.
/// model.set_effect_enabled("REV", false).await?;
///
/// // Re-render whenever the snapshot changes.
/// while let Ok(state) = snapshots.recv().await {
///     if let Some(rev) = state.effect("REV") {
///         println!("REV on = {:?}", rev.on);
///     }
/// }
/// # Ok(())
/// # }
/// ```
#[derive(Clone)]
pub struct DeviceModel {
    state: Arc<RwLock<DeviceState>>,
    snapshots: broadcast::Sender<DeviceState>,
    events: broadcast::Sender<DeviceEvent>,
    commands: mpsc::Sender<Vec<u8>>,
}

impl DeviceModel {
    /// Connect to `ip:5727`, open the streaming protocol, spawn the ingest task,
    /// and kick off a read-only initial sync (rig strings + effect slots).
    ///
    /// Returns once the session is established; the state fills in as the
    /// device's replies stream back. Subscribe *before* awaiting fresh events.
    pub async fn connect(ip: Ipv4Addr) -> Result<DeviceModel, SessionError> {
        let mut session = Session::connect(ip).await?;
        let outcome = session
            .handshake(&[PROTOCOL_MIDI3_STREAM], READ_IDLE)
            .await?;
        session.write_session_preamble().await?;
        let tail = outcome.response_tail().to_vec();

        let state = Arc::new(RwLock::new(DeviceState::new()));
        // Connecting is a SLOW transition: flip the flag, then broadcast the
        // first snapshot and the granular Connected event.
        write_state(&state).connection = Connection::Connected;
        let (snapshots_tx, _) = broadcast::channel(256);
        let (events_tx, _) = broadcast::channel(1024);
        let (commands_tx, commands_rx) = mpsc::channel(64);
        let _ = events_tx.send(DeviceEvent::Connected);
        let _ = snapshots_tx.send(read_state(&state).clone());

        spawn_ingest(
            session,
            state.clone(),
            snapshots_tx.clone(),
            events_tx.clone(),
            commands_rx,
            tail,
        );

        let model = DeviceModel {
            state,
            snapshots: snapshots_tx,
            events: events_tx,
            commands: commands_tx,
        };
        // Read-only sync so the snapshot populates from the replies.
        let _ = model.refresh_rig().await;
        let _ = model.refresh_bank().await;
        let _ = model.refresh_position().await;
        Ok(model)
    }

    /// Subscribe to the **store**: a fresh [`DeviceState`] snapshot each time
    /// *slow* state changes, coalesced to at most one per ingested stream chunk.
    /// This is the channel a UI binds to for re-rendering; the FAST meter lane is
    /// polled separately via [`status`](Self::status).
    ///
    /// Each subscriber receives every snapshot broadcast after it subscribed (a
    /// lagging slow reader may drop intermediate snapshots, but the latest is
    /// always a complete state, so no information is lost).
    pub fn subscribe(&self) -> broadcast::Receiver<DeviceState> {
        self.snapshots.subscribe()
    }

    /// Subscribe to the granular delta stream — every [`DeviceEvent`], including
    /// the FAST ones (`Status`, `BeatPulse`, `TunerDeviance`). For callers that
    /// want per-message deltas rather than coalesced snapshots.
    pub fn events(&self) -> broadcast::Receiver<DeviceEvent> {
        self.events.subscribe()
    }

    /// A cloned snapshot of the current state under the read lock (`getState`).
    pub fn state(&self) -> DeviceState {
        read_state(&self.state).clone()
    }

    /// The latest FAST meter/tuner frame — poll this per animation frame. Equals
    /// `self.state().status`, but skips cloning the whole tree.
    pub fn status(&self) -> RealtimeStatus {
        read_state(&self.state).status
    }

    // ------------------------------------------------------------------
    // Parameters — NRPN `$01`, 14-bit (0–16383), state-tracked.
    // ------------------------------------------------------------------

    /// Set the amp Gain, 0–16383 (NRPN `0x0A/4`). Tracked parameter.
    pub async fn set_gain(&self, v: u16) -> Result<(), CommandError> {
        self.set_param(AMP_PAGE, GAIN_NUMBER, v).await
    }

    /// Set the Rig Volume, 0–16383 (NRPN `0x04/1`). Tracked parameter.
    pub async fn set_rig_volume(&self, v: u16) -> Result<(), CommandError> {
        self.set_param(PAGE_RIG_SETTINGS, RIG_VOLUME_NUMBER, v)
            .await
    }

    /// Set the Main Output Volume, 0–16383 (NRPN `0x7F/0`). Tracked parameter.
    pub async fn set_main_volume(&self, v: u16) -> Result<(), CommandError> {
        self.set_param(SYSTEM_PAGE, MAIN_VOL_NUMBER, v).await
    }

    /// Set the Monitor Output Volume, 0–16383 (NRPN `0x7F/2`). Tracked parameter.
    pub async fn set_monitor_volume(&self, v: u16) -> Result<(), CommandError> {
        self.set_param(SYSTEM_PAGE, MONITOR_VOL_NUMBER, v).await
    }

    /// Turn an effect slot (A/B/C/D/X/MOD/DLY/REV, case-insensitive) on or off
    /// via a `$01` write to number 3. Tracked parameter — folds into the slot's
    /// `on` state. Errors with [`CommandError::UnknownSlot`] on an unknown name.
    pub async fn set_effect_enabled(&self, slot: &str, on: bool) -> Result<(), CommandError> {
        let page = params::effect_slot_page(slot)
            .ok_or_else(|| CommandError::UnknownSlot(slot.to_string()))?;
        self.set_param(page, EFFECT_PARAM_STATE, u16::from(on))
            .await
    }

    /// Set an effect slot's dry/wet Mix, 0–16383 (NRPN `<slot>/4`). Tracked
    /// parameter. Errors with [`CommandError::UnknownSlot`] on an unknown name.
    pub async fn set_effect_mix(&self, slot: &str, v: u16) -> Result<(), CommandError> {
        let page = params::effect_slot_page(slot)
            .ok_or_else(|| CommandError::UnknownSlot(slot.to_string()))?;
        self.set_param(page, EFFECT_PARAM_MIX, v).await
    }

    /// Set the Tempo in whole beats per minute (NRPN `0x04/0`). The wire value
    /// is `bpm * 64`, saturating-multiplied and clamped to the 14-bit maximum
    /// (≈255 BPM). Tracked parameter; the momentary counterpart is
    /// [`tap_tempo`](Self::tap_tempo).
    pub async fn set_tempo_bpm(&self, bpm: u16) -> Result<(), CommandError> {
        let value = bpm.saturating_mul(TEMPO_BPM_SCALE).min(NRPN_MAX);
        self.set_param(PAGE_RIG_SETTINGS, TEMPO_NUMBER, value).await
    }

    /// Set an arbitrary numeric parameter (`$01` Single Parameter Change) — the
    /// escape hatch for any address without a named setter. Mutating; the
    /// backbone every parameter setter above routes through.
    pub async fn set_param(&self, page: u8, number: u8, value: u16) -> Result<(), CommandError> {
        self.enqueue(set_single(PRODUCT, DEVICE, page, number, value))
            .await
    }

    /// Re-request the rig strings and every effect slot's Type/State (read-only).
    /// Also the initial sync run at connect time. Neither a parameter nor an
    /// action: it only issues value *requests* and changes nothing on the device.
    pub async fn refresh_rig(&self) -> Result<(), CommandError> {
        // Rig Name, Author, Comment, Date, Amp Name, Cab Name.
        for tag in [1u8, 2, 4, 3, 10, 32] {
            self.enqueue(request_string(PRODUCT, DEVICE, PAGE_STRINGS, tag))
                .await?;
        }
        for (_, page) in params::effect_slots() {
            self.enqueue(request_single(PRODUCT, DEVICE, *page, EFFECT_PARAM_TYPE))
                .await?;
            self.enqueue(request_single(PRODUCT, DEVICE, *page, EFFECT_PARAM_STATE))
                .await?;
        }
        Ok(())
    }

    /// Request the current bank's five-slot name preview (rig / amp / cabinet
    /// names) as extended strings (`$47`). The `$07` replies fold into
    /// [`DeviceState::bank`]. Read-only: it changes nothing on the device. The
    /// device also pushes this block unasked on a bank change, so a controller
    /// need only call this once at connect.
    pub async fn refresh_bank(&self) -> Result<(), CommandError> {
        use params::BankPreviewField::{AmpName, CabinetName, RigName};
        for field in [RigName, AmpName, CabinetName] {
            for slot in 1..=params::BANK_SLOTS as u8 {
                let addr = params::bank_preview_address(field, slot);
                self.enqueue(request_extended_string(PRODUCT, DEVICE, addr))
                    .await?;
            }
        }
        Ok(())
    }

    /// Ask the device where it is: the current bank and rig slot, as two `$46`
    /// extended-parameter requests. The `$06` replies fold into
    /// [`DeviceState::current_bank`] and [`DeviceState::current_rig_slot`].
    /// Read-only.
    ///
    /// Only needed once, at connect: the device pushes an unsolicited `$06` for
    /// whichever of the two changed on every subsequent rig change, whoever
    /// caused it.
    pub async fn refresh_position(&self) -> Result<(), CommandError> {
        for address in [
            generated::CURRENT_BANK_ADDRESS,
            generated::CURRENT_RIG_SLOT_ADDRESS,
        ] {
            self.enqueue(request_extended_param(PRODUCT, DEVICE, address))
                .await?;
        }
        Ok(())
    }

    /// Fold one value from a [`CborSession`](crate::cbor::CborSession) into this
    /// model's state tree, emitting whatever events it raises and republishing
    /// the snapshot.
    ///
    /// The two channels are one event universe in two wire formats, so a client
    /// holding both hands the CBOR side's pushes here and reads a single tree.
    /// This is how the morph position reaches a model whose own session cannot
    /// carry it.
    pub fn apply_cbor(&self, address: u32, value: i64) {
        let outcome = write_state(&self.state).apply_cbor(address, value);
        for event in outcome.events {
            let _ = self.events.send(event);
        }
        if outcome.slow_changed {
            let _ = self.snapshots.send(read_state(&self.state).clone());
        }
    }

    /// Fold a [`StateSnapshot`](crate::cbor::StateSnapshot)'s current bank and
    /// rig slot into the state tree, emitting a
    /// [`DeviceEvent::CurrentPosition`] and broadcasting a fresh snapshot.
    ///
    /// For a client that already holds a `StateSnapshot` — read over the CBOR
    /// channel *before* this streaming session opened — this seeds the tree
    /// without waiting for a reply. A session that is already up should call
    /// [`refresh_position`](Self::refresh_position) instead and let the device
    /// answer. Only the `Some` fields overwrite; a `None` leaves the current
    /// value untouched.
    pub fn set_current_position(&self, bank: Option<u16>, slot: Option<u16>) {
        {
            let mut st = write_state(&self.state);
            if bank.is_some() {
                st.current_bank = bank;
            }
            if slot.is_some() {
                st.current_rig_slot = slot;
            }
        }
        let _ = self
            .events
            .send(DeviceEvent::CurrentPosition { bank, slot });
        let _ = self.snapshots.send(read_state(&self.state).clone());
    }

    /// Request one numeric parameter's current value (function `$41`). The
    /// device answers with a `$01` message on the same stream, which the ingest
    /// task folds into the snapshot. Read-only: it changes nothing on the
    /// device. This is the read-back to issue after a
    /// [`set_param`](Self::set_param), which the device applies without echoing.
    pub async fn request_param(&self, page: u8, number: u8) -> Result<(), CommandError> {
        self.enqueue(request_single(PRODUCT, DEVICE, page, number))
            .await
    }

    /// Request a parameter value rendered to its exact display text (function
    /// `$7C`) — ask the device for the string a value shows on screen (e.g.
    /// `"5.2"`, `"120 BPM"`, `"<0.0>"`) instead of a generic percentage.
    /// Read-only: it changes nothing on the device.
    ///
    /// This is a **streaming request/response**, consistent with the store
    /// model: it enqueues the request and returns immediately — there is no
    /// blocking correlation. Watch [`events`](Self::events) for the matching
    /// [`DeviceEvent::RenderedString`] carrying the rendered `text`.
    pub async fn request_render(
        &self,
        page: u8,
        number: u8,
        value: u16,
    ) -> Result<(), CommandError> {
        self.enqueue(request_rendered_string(
            PRODUCT, DEVICE, page, number, value,
        ))
        .await
    }

    // ------------------------------------------------------------------
    // Actions — CC, momentary/expression, NOT stored in state.
    // ------------------------------------------------------------------

    /// Send an arbitrary [`Control`] on the command channel ([`CC_CHANNEL`]).
    /// The generic entry point behind every action convenience method below.
    pub async fn send_control(&self, c: control::Control) -> Result<(), CommandError> {
        self.enqueue(c.message(CC_CHANNEL)).await
    }

    /// Select rig slot 1–5 in the current bank (CC50–54). Changes the rig.
    pub async fn select_rig(&self, rig: u8) -> Result<(), CommandError> {
        self.send_control(Control::LoadSlot(rig)).await
    }

    /// Step to the next rig (CC48). Changes the rig.
    pub async fn rig_up(&self) -> Result<(), CommandError> {
        self.send_control(Control::Up).await
    }

    /// Step to the previous rig (CC49). Changes the rig.
    pub async fn rig_down(&self) -> Result<(), CommandError> {
        self.send_control(Control::Down).await
    }

    /// Preselect bank `n` (1-based; CC47). Takes effect with the next rig.
    pub async fn bank(&self, n: u16) -> Result<(), CommandError> {
        self.send_control(Control::BankPreselect(n.saturating_sub(1) as u8))
            .await
    }

    /// Load a rig by its flat, 0-based index — the device's own numbering, and
    /// the only address that reaches a rig outside the current bank.
    ///
    /// Sent as the documented pair: the absolute bank preselect (CC47) followed
    /// by the slot load (CC50–54) that commits it. The index divides by
    /// [`generated::BANK_SLOTS`], so index 123 is bank 25, slot 4.
    ///
    /// Nothing here assumes how many banks a device has. Aim past the end and
    /// the device simply stays where it is — and says so in the `$06` position
    /// push that follows, so
    /// [`DeviceState::current_rig_index`](crate::state::DeviceState::current_rig_index)
    /// always reflects where it actually landed, not where this aimed.
    pub async fn select_rig_index(&self, index: u16) -> Result<(), CommandError> {
        let slots = generated::BANK_SLOTS as u16;
        self.bank(index / slots + 1).await?;
        self.select_rig((index % slots) as u8 + 1).await
    }

    /// Tap the tempo (CC30). Mutating — advances the tap-tempo clock.
    pub async fn tap_tempo(&self) -> Result<(), CommandError> {
        self.send_control(Control::TapTempo).await
    }

    /// Open (`true`) or close (`false`) the tuner (CC31). Mutating.
    pub async fn tuner_mode(&self, open: bool) -> Result<(), CommandError> {
        self.send_control(Control::TunerMode(open)).await
    }

    /// Morph button (CC80): `rise` = rise to the morph target, else fall back.
    /// Mutating — moves the morph state.
    pub async fn morph_button(&self, rise: bool) -> Result<(), CommandError> {
        self.send_control(Control::MorphButton(rise)).await
    }

    /// Set the morph pedal position 0–127 (CC11). Mutating.
    pub async fn morph_pedal(&self, v: u8) -> Result<(), CommandError> {
        self.send_control(Control::MorphPedal(v)).await
    }

    /// Delay + Reverb Freeze (CC35): `on` engages the freeze. Mutating.
    pub async fn freeze(&self, on: bool) -> Result<(), CommandError> {
        self.send_control(Control::Freeze(on)).await
    }

    /// Rotary speaker speed (CC33): `fast` = fast, else slow. Mutating.
    pub async fn rotary_fast(&self, fast: bool) -> Result<(), CommandError> {
        self.send_control(Control::RotaryFast(fast)).await
    }

    /// Delay Infinity (CC34): `on` holds the delay indefinitely. Mutating.
    pub async fn delay_infinity(&self, on: bool) -> Result<(), CommandError> {
        self.send_control(Control::DelayInfinity(on)).await
    }

    /// Toggle every module A–REV on/off (CC16). Mutating.
    pub async fn toggle_all_modules(&self) -> Result<(), CommandError> {
        self.send_control(Control::ToggleAllModules).await
    }

    /// Press Effect Button `n` (I–IIII, clamped to 1..=4; CC75–78). Mutating.
    pub async fn effect_button(&self, n: u8) -> Result<(), CommandError> {
        self.send_control(Control::EffectButton(n)).await
    }

    /// Set the wah pedal position 0–127 (CC1). Mutating.
    pub async fn wah_pedal(&self, v: u8) -> Result<(), CommandError> {
        self.send_control(Control::WahPedal(v)).await
    }

    /// Set the pitch pedal position 0–127 (CC4). Mutating.
    pub async fn pitch_pedal(&self, v: u8) -> Result<(), CommandError> {
        self.send_control(Control::PitchPedal(v)).await
    }

    /// Set the volume pedal position 0–127 (CC7). Mutating.
    pub async fn volume_pedal(&self, v: u8) -> Result<(), CommandError> {
        self.send_control(Control::VolumePedal(v)).await
    }

    /// Set the panorama 0–127 (CC10). Mutating.
    pub async fn panorama(&self, v: u8) -> Result<(), CommandError> {
        self.send_control(Control::Panorama(v)).await
    }

    /// Enqueue raw (pre-framing) MIDI bytes for the ingest task to write.
    async fn enqueue(&self, bytes: Vec<u8>) -> Result<(), CommandError> {
        self.commands
            .send(bytes)
            .await
            .map_err(|_| CommandError::Disconnected)
    }
}

/// Spawn the task that owns the socket: reads and decodes the stream into the
/// shared state, drives the coalesced snapshot store + granular event broadcast,
/// and drains the command channel to the wire.
fn spawn_ingest(
    mut session: Session,
    state: Arc<RwLock<DeviceState>>,
    snapshots: broadcast::Sender<DeviceState>,
    events: broadcast::Sender<DeviceEvent>,
    mut commands: mpsc::Receiver<Vec<u8>>,
    tail: Vec<u8>,
) {
    tokio::spawn(async move {
        let mut unframer = Unframer::new();
        // Decode anything that rode in on the handshake acceptance tail.
        apply_chunk(&state, &snapshots, &events, unframer.push(&tail));
        loop {
            tokio::select! {
                read = session.read_once(READ_IDLE, READ_MAX) => match read {
                    Ok(chunk) => {
                        apply_chunk(&state, &snapshots, &events, unframer.push(&chunk));
                    }
                    // A closed/errored socket is a SLOW Disconnected transition.
                    Err(_) => {
                        disconnect(&state, &snapshots, &events);
                        break;
                    }
                },
                cmd = commands.recv() => match cmd {
                    Some(bytes) => {
                        if session.write_all(&midi3::frame(&bytes)).await.is_err() {
                            disconnect(&state, &snapshots, &events);
                            break;
                        }
                    }
                    // All handles dropped: nothing more can be requested.
                    None => break,
                },
            }
        }
    });
}

/// Apply every message in one ingested chunk: broadcast each granular event, and
/// — if any message changed a slow field — emit exactly one coalesced snapshot.
fn apply_chunk(
    state: &RwLock<DeviceState>,
    snapshots: &broadcast::Sender<DeviceState>,
    events: &broadcast::Sender<DeviceEvent>,
    msgs: Vec<Vec<u8>>,
) {
    let mut slow_changed = false;
    for msg in msgs {
        let outcome = write_state(state).apply(&msg);
        for ev in outcome.events {
            let _ = events.send(ev);
        }
        slow_changed |= outcome.slow_changed;
    }
    if slow_changed {
        let _ = snapshots.send(read_state(state).clone());
    }
}

/// Record the Disconnected transition: flip the flag, then broadcast a final
/// snapshot (a SLOW change) and the granular Disconnected event.
fn disconnect(
    state: &RwLock<DeviceState>,
    snapshots: &broadcast::Sender<DeviceState>,
    events: &broadcast::Sender<DeviceEvent>,
) {
    write_state(state).connection = Connection::Disconnected;
    let _ = events.send(DeviceEvent::Disconnected);
    let _ = snapshots.send(read_state(state).clone());
}

/// Acquire the write guard, recovering from a poisoned lock rather than panicking.
fn write_state(state: &RwLock<DeviceState>) -> std::sync::RwLockWriteGuard<'_, DeviceState> {
    match state.write() {
        Ok(g) => g,
        Err(poisoned) => poisoned.into_inner(),
    }
}

/// Acquire the read guard, recovering from a poisoned lock rather than panicking.
fn read_state(state: &RwLock<DeviceState>) -> std::sync::RwLockReadGuard<'_, DeviceState> {
    match state.read() {
        Ok(g) => g,
        Err(poisoned) => poisoned.into_inner(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::nrpn::{sysex, u14_split};
    use crate::state::Connection;

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
            10,
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

        // An untracked string tag leaves the snapshot unchanged (not slow).
        let out = st.apply(&sysex(
            0x00,
            0x7F,
            FUNCTION_STRING_PARAM,
            PAGE_STRINGS,
            99,
            b"x",
        ));
        assert!(!out.slow_changed);
        assert_eq!(out.events, vec![DeviceEvent::StringTag { number: 99 }]);
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
            METER_NUMBER,
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
        let out = st.apply(&set_single(0x00, 0x00, SYSTEM_PAGE, MAIN_VOL_NUMBER, 9000));
        assert!(out.slow_changed);
        assert_eq!(st.output.main_volume, Some(9000));

        let out = st.apply(&set_single(
            0x00,
            0x00,
            SYSTEM_PAGE,
            MONITOR_VOL_NUMBER,
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
        // Amp Name is string tag 10 on the string page.
        let msg = ext_string(PAGE_STRINGS, 10, b"JCM800");
        let out = st.apply(&msg);
        assert_eq!(st.amp.name.as_deref(), Some("JCM800"));
        assert!(out.slow_changed);
        assert_eq!(out.events, vec![DeviceEvent::StringTag { number: 10 }]);
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
        // A $07 addressed to a non-string page is not routed.
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
        // An out-of-range bank number is ignored.
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
            MONITOR_VOL_NUMBER,
            5000,
        ));
        assert_eq!(st.output.master_volume(), Some(5000));
        let out = st.apply(&set_single(
            0x00,
            0x00,
            SYSTEM_PAGE,
            HEADPHONE_VOL_NUMBER,
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
        // Page 0 / number 0x77, half-morphed.
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
    /// in the same field, and raises the same event, as one arriving over MIDI3.
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
        // A value too wide for the field is dropped, not truncated.
        assert_eq!(
            st.apply_cbor(generated::MORPH_ADDRESS, 70_000),
            ApplyOutcome::empty()
        );
        assert_eq!(st.morph, Some(8192));
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

    #[test]
    fn new_state_is_disconnected() {
        assert_eq!(DeviceState::new().connection, Connection::Disconnected);
    }
}
