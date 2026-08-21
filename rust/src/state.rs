//! The typed, immutable device state tree — the **store snapshot** the UI binds
//! to.
//!
//! Everything here is plain data: `Clone`, `Debug`, `PartialEq` (and `Copy`
//! where cheap), with no `HashMap`, no borrows beyond `&'static str` labels, and
//! no methods that read fields (callers touch the fields directly). That keeps
//! the tree FFI-friendly — a flat set of records and enums a foreign-function
//! layer can mirror as value types — and cheap to clone for each snapshot the
//! store emits.
//!
//! [`DeviceState`] is the root. The decode logic that fills it in lives on
//! [`DeviceState::apply`](crate::model) in `model.rs` (next to the wire address
//! constants); this module is the data shape only.

use crate::generated;
use crate::model::RealtimeStatus;
use crate::params;

/// Whether a live session to the Profiler is currently open.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Connection {
    /// A session is open and the stream is being ingested.
    Connected,
    /// No session (initial state, or the device closed the connection).
    Disconnected,
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
    /// On/Off state, once seen.
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
    /// Monitor Output Volume (NRPN `0x7F/2`, 14-bit), once seen.
    pub monitor_volume: Option<u16>,
}

/// The immutable device-state snapshot — the store's value type.
///
/// A fresh, cheap-to-clone bag of plain data. The UI reads fields directly
/// (`state.rig.name`, `state.effects[0].on`, …); no field has an accessor. The
/// async [`DeviceModel`](crate::model::DeviceModel) hands out clones of this via
/// [`state`](crate::model::DeviceModel::state) and its coalesced
/// [`subscribe`](crate::model::DeviceModel::subscribe) stream.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DeviceState {
    /// Whether a live session is open.
    pub connection: Connection,
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
    /// Latest morph position (0–16383), once seen (NRPN `0x00/0x0B`).
    pub morph: Option<u16>,
    /// The most recent realtime status / meter frame (the FAST lane).
    pub status: RealtimeStatus,
}

impl DeviceState {
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
            rig: Rig::default(),
            amp: Amp::default(),
            cabinet: Cabinet::default(),
            effects,
            tuner: Tuner::default(),
            output: Output::default(),
            morph: None,
            status: RealtimeStatus::default(),
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

#[cfg(test)]
mod tests {
    use super::*;

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
}
