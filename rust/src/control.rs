//! Typed MIDI control-message vocabulary for the Profiler.
//!
//! These are the **7-bit** channel-voice messages — Control Change (CC),
//! Program Change (PC), and Bank Select — that the Profiler responds to,
//! distinct from the 14-bit NRPN-over-SysEx traffic in [`crate::nrpn`]. Every
//! CC number comes from the generated table sourced from the Kemper MIDI
//! Parameter Documentation (cross-checked against PySwitch).
//!
//! Two ways to use it:
//! - the named [`CC_*`](CC_WAH_PEDAL) constants, for hand-building messages, and
//! - the [`Control`] enum plus [`Control::message`], a discoverable,
//!   misuse-resistant API that clamps ranges and emits the raw MIDI bytes.
//!
//! Everything is pure and offline. CC bytes are produced via
//! [`crate::nrpn::control_change`], which masks channel/controller/value to
//! 7 bits.

use crate::generated;
use crate::nrpn::control_change;

// ---------------------------------------------------------------------------
// Continuous controllers
// ---------------------------------------------------------------------------

/// CC1 — Wah Pedal (continuous, 0–127).
pub const CC_WAH_PEDAL: u8 = generated::CC_WAH_PEDAL;
/// CC4 — Pitch Pedal (continuous, 0–127).
pub const CC_PITCH_PEDAL: u8 = generated::CC_PITCH_PEDAL;
/// CC7 — Volume Pedal (continuous, 0–127).
pub const CC_VOLUME_PEDAL: u8 = generated::CC_VOLUME_PEDAL;
/// CC10 — Panorama (continuous, 0–127).
pub const CC_PANORAMA: u8 = generated::CC_PANORAMA;
/// CC11 — Morph Pedal (continuous, 0–127).
pub const CC_MORPH_PEDAL: u8 = generated::CC_MORPH_PEDAL;
/// CC68 — Delay Mix (continuous, 0–127).
pub const CC_DELAY_MIX: u8 = generated::CC_DELAY_MIX;
/// CC69 — Delay Feedback (continuous, 0–127).
pub const CC_DELAY_FEEDBACK: u8 = generated::CC_DELAY_FEEDBACK;
/// CC70 — Reverb Mix (continuous, 0–127).
pub const CC_REVERB_MIX: u8 = generated::CC_REVERB_MIX;
/// CC71 — Reverb Time (continuous, 0–127).
pub const CC_REVERB_TIME: u8 = generated::CC_REVERB_TIME;
/// CC72 — Gain (continuous, 0–127).
pub const CC_GAIN: u8 = generated::CC_GAIN;
/// CC73 — Monitor (Output) Volume (continuous, 0–127).
pub const CC_MONITOR_VOLUME: u8 = generated::CC_MONITOR_VOLUME;

// ---------------------------------------------------------------------------
// Switches
// ---------------------------------------------------------------------------

/// CC16 — toggle all modules (A–REV) on/off.
pub const CC_TOGGLE_ALL_MODULES: u8 = generated::CC_TOGGLE_ALL_MODULES;

/// CC17 — module A on/off (1 on / 0 off).
pub const CC_MODULE_A: u8 = generated::CC_MODULE_A;
/// CC18 — module B on/off (1 on / 0 off).
pub const CC_MODULE_B: u8 = generated::CC_MODULE_B;
/// CC19 — module C on/off (1 on / 0 off).
pub const CC_MODULE_C: u8 = generated::CC_MODULE_C;
/// CC20 — module D on/off (1 on / 0 off).
pub const CC_MODULE_D: u8 = generated::CC_MODULE_D;
/// CC22 — module X on/off (1 on / 0 off).
pub const CC_MODULE_X: u8 = generated::CC_MODULE_X;
/// CC24 — module MOD on/off (1 on / 0 off).
pub const CC_MODULE_MOD: u8 = generated::CC_MODULE_MOD;
/// CC26 — module DLY off, **without** spillover.
pub const CC_MODULE_DLY_NO_SPILL: u8 = generated::CC_MODULE_DLY_NO_SPILL;
/// CC27 — module DLY on/off, **with** spillover.
pub const CC_MODULE_DLY: u8 = generated::CC_MODULE_DLY;
/// CC28 — module REV off, **without** spillover.
pub const CC_MODULE_REV_NO_SPILL: u8 = generated::CC_MODULE_REV_NO_SPILL;
/// CC29 — module REV on/off, **with** spillover.
pub const CC_MODULE_REV: u8 = generated::CC_MODULE_REV;

/// CC30 — Tap Tempo (any value taps; 1/0 also toggles the Beat Scanner).
pub const CC_TAP_TEMPO: u8 = generated::CC_TAP_TEMPO;
/// CC31 — Tuner Mode (1 open / 0 close).
pub const CC_TUNER_MODE: u8 = generated::CC_TUNER_MODE;
/// CC33 — Rotary speaker speed (1 fast / 0 slow).
pub const CC_ROTARY_SPEED: u8 = generated::CC_ROTARY_SPEED;
/// CC34 — Delay Infinity (1 on / 0 off).
pub const CC_DELAY_INFINITY: u8 = generated::CC_DELAY_INFINITY;
/// CC35 — Delay + Reverb Freeze (1 on / 0 off).
pub const CC_FREEZE: u8 = generated::CC_FREEZE;

/// CC47 — Bank/Performance preselect (value = bank − 1).
pub const CC_BANK_PRESELECT: u8 = generated::CC_BANK_PRESELECT;
/// CC48 — Performance/Rig up.
pub const CC_UP: u8 = generated::CC_UP;
/// CC49 — Performance/Rig down.
pub const CC_DOWN: u8 = generated::CC_DOWN;
/// CC50 — load Slot 1 (value 1).
pub const CC_LOAD_SLOT_1: u8 = generated::CC_LOAD_SLOT_1;
/// CC51 — load Slot 2 (value 1).
pub const CC_LOAD_SLOT_2: u8 = generated::CC_LOAD_SLOT_2;
/// CC52 — load Slot 3 (value 1).
pub const CC_LOAD_SLOT_3: u8 = generated::CC_LOAD_SLOT_3;
/// CC53 — load Slot 4 (value 1).
pub const CC_LOAD_SLOT_4: u8 = generated::CC_LOAD_SLOT_4;
/// CC54 — load Slot 5 (value 1).
pub const CC_LOAD_SLOT_5: u8 = generated::CC_LOAD_SLOT_5;

/// CC75 — Effect Button I.
pub const CC_EFFECT_BUTTON_I: u8 = generated::CC_EFFECT_BUTTON_I;
/// CC76 — Effect Button II.
pub const CC_EFFECT_BUTTON_II: u8 = generated::CC_EFFECT_BUTTON_II;
/// CC77 — Effect Button III.
pub const CC_EFFECT_BUTTON_III: u8 = generated::CC_EFFECT_BUTTON_III;
/// CC78 — Effect Button IIII.
pub const CC_EFFECT_BUTTON_IIII: u8 = generated::CC_EFFECT_BUTTON_IIII;

/// CC80 — Morph button (1 rise / 0 fall).
pub const CC_MORPH_BUTTON: u8 = generated::CC_MORPH_BUTTON;

// ---------------------------------------------------------------------------
// Bank Select (standard MIDI, used with Program Change for rig selection)
// ---------------------------------------------------------------------------

/// CC0 — Bank Select MSB.
pub const CC_BANK_SELECT_MSB: u8 = generated::CC_BANK_SELECT_MSB;
/// CC32 — Bank Select LSB (also the "rig index part 1" in select feedback).
pub const CC_BANK_SELECT_LSB: u8 = generated::CC_BANK_SELECT_LSB;

/// The MIDI Program Change status nibble (`0xC0 | channel`).
pub const PROGRAM_CHANGE_STATUS: u8 = generated::PROGRAM_CHANGE_STATUS;

/// A 2-byte MIDI Program Change on `channel` (0–15): `[0xC0|ch, program]`.
///
/// `program` is masked to 7 bits. Used with [`Control::BankSelect`] for the
/// Profiler's rig-index selection scheme (`rig_index = CC32*128 + PC`).
pub fn program_change(channel: u8, program: u8) -> [u8; 2] {
    [PROGRAM_CHANGE_STATUS | (channel & 0x0F), program & 0x7F]
}

/// A Kemper control action, addressable to a MIDI channel via
/// [`Control::message`]. Variants carrying a `u8` value pass it straight
/// through as the CC value (masked to 7 bits); `bool`/switch variants emit
/// 1 (on/rise/fast/open) or 0.
///
/// `#[non_exhaustive]`: more Kemper controls may be added, so downstream
/// `match`es must include a wildcard arm.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
#[non_exhaustive]
pub enum Control {
    /// Wah pedal position (CC1), 0–127.
    WahPedal(u8),
    /// Pitch pedal position (CC4), 0–127.
    PitchPedal(u8),
    /// Volume pedal position (CC7), 0–127.
    VolumePedal(u8),
    /// Panorama (CC10), 0–127.
    Panorama(u8),
    /// Morph pedal position (CC11), 0–127.
    MorphPedal(u8),
    /// Gain (CC72), 0–127.
    Gain(u8),
    /// Delay Mix (CC68), 0–127.
    DelayMix(u8),
    /// Delay Feedback (CC69), 0–127.
    DelayFeedback(u8),
    /// Reverb Mix (CC70), 0–127.
    ReverbMix(u8),
    /// Reverb Time (CC71), 0–127.
    ReverbTime(u8),
    /// Monitor (Output) Volume (CC73), 0–127.
    MonitorVolume(u8),
    /// Toggle all modules A–REV (CC16).
    ToggleAllModules,
    /// Enable/disable one effect module (`on` → 1/0). `slot` maps to its
    /// enable CC via [`slot_enable_cc`]; DLY/REV use the **with-spillover** CC.
    SlotEnable {
        /// Which effect slot to switch.
        slot: ModuleSlot,
        /// `true` → value 1 (on), `false` → value 0 (off).
        on: bool,
    },
    /// Rotary speaker speed (CC33): `true` fast / `false` slow.
    RotaryFast(bool),
    /// Delay Infinity (CC34): `true` on / `false` off.
    DelayInfinity(bool),
    /// Delay + Reverb Freeze (CC35): `true` on / `false` off.
    Freeze(bool),
    /// Tap Tempo (CC30). Any value taps; emits value 1.
    TapTempo,
    /// Tuner Mode (CC31): `true` open / `false` close.
    TunerMode(bool),
    /// Bank/Performance preselect (CC47). Value is the bank number − 1,
    /// masked to 7 bits.
    BankPreselect(u8),
    /// Performance/Rig up (CC48).
    Up,
    /// Performance/Rig down (CC49).
    Down,
    /// Load a performance slot 1–5 (CC50–54, value 1). Out-of-range values are
    /// clamped to 1..=5.
    LoadSlot(u8),
    /// Press an Effect Button I–IIII (CC75–78, value 1). Out-of-range values
    /// are clamped to 1..=4.
    EffectButton(u8),
    /// Morph button (CC80): `true` rise / `false` fall.
    MorphButton(bool),
    /// Program Change (`0xC0|ch, program`). `program` masked to 7 bits.
    ProgramChange(u8),
    /// Bank Select: the CC0 (MSB) + CC32 (LSB) pair, concatenated.
    BankSelect {
        /// Bank Select MSB (CC0), masked to 7 bits.
        msb: u8,
        /// Bank Select LSB (CC32), masked to 7 bits.
        lsb: u8,
    },
}

/// One of the eight Profiler effect-module slots.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ModuleSlot {
    /// Module A.
    A,
    /// Module B.
    B,
    /// Module C.
    C,
    /// Module D.
    D,
    /// Module X.
    X,
    /// Modulation module.
    Mod,
    /// Delay module.
    Dly,
    /// Reverb module.
    Rev,
}

impl ModuleSlot {
    /// The slot's short name (`"A"`..`"REV"`), matching
    /// [`crate::params::effect_slots`].
    pub fn as_str(self) -> &'static str {
        match self {
            ModuleSlot::A => "A",
            ModuleSlot::B => "B",
            ModuleSlot::C => "C",
            ModuleSlot::D => "D",
            ModuleSlot::X => "X",
            ModuleSlot::Mod => "MOD",
            ModuleSlot::Dly => "DLY",
            ModuleSlot::Rev => "REV",
        }
    }

    /// Parse a slot short name, case-insensitively.
    pub fn from_name(name: &str) -> Option<ModuleSlot> {
        [
            ModuleSlot::A,
            ModuleSlot::B,
            ModuleSlot::C,
            ModuleSlot::D,
            ModuleSlot::X,
            ModuleSlot::Mod,
            ModuleSlot::Dly,
            ModuleSlot::Rev,
        ]
        .into_iter()
        .find(|s| s.as_str().eq_ignore_ascii_case(name))
    }
}

/// The on/off CC for an effect slot. DLY and REV return their
/// **with-spillover** CCs (27/29); the no-spillover variants (26/28) are
/// available as [`CC_MODULE_DLY_NO_SPILL`] / [`CC_MODULE_REV_NO_SPILL`].
///
/// Reads the generated `SLOT_ENABLE_CC` table:
/// A/B/C/D = 17/18/19/20, X = 22, MOD = 24, DLY = 27, REV = 29.
pub fn slot_enable_cc(slot: ModuleSlot) -> u8 {
    generated::SLOT_ENABLE_CC
        .iter()
        .find(|(name, _)| *name == slot.as_str())
        .map(|(_, cc)| *cc)
        .expect("every ModuleSlot has an entry in the generated SLOT_ENABLE_CC table")
}

/// Map a switch to its MIDI value: `true` → 1, `false` → 0.
fn sw(on: bool) -> u8 {
    on as u8
}

impl Control {
    /// Build the raw MIDI bytes for this control on `channel` (0–15).
    ///
    /// Most variants are a single 3-byte Control Change. [`Control::ProgramChange`]
    /// is 2 bytes; [`Control::BankSelect`] is two Control Changes (CC0 then
    /// CC32) concatenated into 6 bytes. All controller/value bytes are masked
    /// to 7 bits and slot/button indices are clamped to their valid ranges.
    ///
    /// The navigation controls are **momentary**: the device treats value 1 as
    /// the press and needs the matching value-0 release to complete the gesture.
    /// Left held, it abandons the change and reloads the previous rig a couple
    /// of seconds later. [`Control::Up`], [`Control::Down`] and
    /// [`Control::LoadSlot`] therefore emit both halves as one 6-byte message.
    pub fn message(&self, channel: u8) -> Vec<u8> {
        let cc = |controller: u8, value: u8| control_change(channel, controller, value).to_vec();
        // A momentary press immediately followed by its release.
        let tap = |controller: u8| {
            let mut out = control_change(channel, controller, 1).to_vec();
            out.extend_from_slice(&control_change(channel, controller, 0));
            out
        };
        match *self {
            Control::WahPedal(v) => cc(CC_WAH_PEDAL, v),
            Control::PitchPedal(v) => cc(CC_PITCH_PEDAL, v),
            Control::VolumePedal(v) => cc(CC_VOLUME_PEDAL, v),
            Control::Panorama(v) => cc(CC_PANORAMA, v),
            Control::MorphPedal(v) => cc(CC_MORPH_PEDAL, v),
            Control::Gain(v) => cc(CC_GAIN, v),
            Control::DelayMix(v) => cc(CC_DELAY_MIX, v),
            Control::DelayFeedback(v) => cc(CC_DELAY_FEEDBACK, v),
            Control::ReverbMix(v) => cc(CC_REVERB_MIX, v),
            Control::ReverbTime(v) => cc(CC_REVERB_TIME, v),
            Control::MonitorVolume(v) => cc(CC_MONITOR_VOLUME, v),
            Control::ToggleAllModules => cc(CC_TOGGLE_ALL_MODULES, 1),
            Control::SlotEnable { slot, on } => cc(slot_enable_cc(slot), sw(on)),
            Control::RotaryFast(fast) => cc(CC_ROTARY_SPEED, sw(fast)),
            Control::DelayInfinity(on) => cc(CC_DELAY_INFINITY, sw(on)),
            Control::Freeze(on) => cc(CC_FREEZE, sw(on)),
            Control::TapTempo => cc(CC_TAP_TEMPO, 1),
            Control::TunerMode(open) => cc(CC_TUNER_MODE, sw(open)),
            Control::BankPreselect(bank) => cc(CC_BANK_PRESELECT, bank),
            Control::Up => tap(CC_UP),
            Control::Down => tap(CC_DOWN),
            Control::LoadSlot(slot) => {
                let slot = slot.clamp(1, 5);
                tap(CC_LOAD_SLOT_1 + (slot - 1))
            }
            Control::EffectButton(n) => {
                let n = n.clamp(1, 4);
                cc(CC_EFFECT_BUTTON_I + (n - 1), 1)
            }
            Control::MorphButton(rise) => cc(CC_MORPH_BUTTON, sw(rise)),
            Control::ProgramChange(program) => program_change(channel, program).to_vec(),
            Control::BankSelect { msb, lsb } => {
                let mut out = control_change(channel, CC_BANK_SELECT_MSB, msb).to_vec();
                out.extend_from_slice(&control_change(channel, CC_BANK_SELECT_LSB, lsb));
                out
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn continuous_controller_bytes() {
        assert_eq!(Control::Gain(64).message(0), vec![0xB0, 72, 64]);
        assert_eq!(Control::DelayMix(10).message(0), vec![0xB0, 68, 10]);
        assert_eq!(Control::ReverbTime(127).message(0), vec![0xB0, 71, 127]);
        assert_eq!(Control::MorphPedal(0).message(0), vec![0xB0, 11, 0]);
    }

    #[test]
    fn tap_tempo_is_cc30() {
        assert_eq!(Control::TapTempo.message(0), vec![0xB0, 30, 1]);
    }

    #[test]
    fn tuner_mode_open_is_cc31_val1() {
        assert_eq!(Control::TunerMode(true).message(0), vec![0xB0, 31, 1]);
        assert_eq!(Control::TunerMode(false).message(0), vec![0xB0, 31, 0]);
    }

    #[test]
    fn load_slot_maps_to_cc50_plus() {
        assert_eq!(
            Control::LoadSlot(3).message(0),
            vec![0xB0, 52, 1, 0xB0, 52, 0]
        );
        assert_eq!(
            Control::LoadSlot(1).message(0),
            vec![0xB0, 50, 1, 0xB0, 50, 0]
        );
        assert_eq!(
            Control::LoadSlot(5).message(0),
            vec![0xB0, 54, 1, 0xB0, 54, 0]
        );
        // Out-of-range clamps into 1..=5.
        assert_eq!(
            Control::LoadSlot(0).message(0),
            vec![0xB0, 50, 1, 0xB0, 50, 0]
        );
        assert_eq!(
            Control::LoadSlot(99).message(0),
            vec![0xB0, 54, 1, 0xB0, 54, 0]
        );
    }

    #[test]
    fn effect_button_maps_to_cc75_plus() {
        assert_eq!(Control::EffectButton(4).message(0), vec![0xB0, 78, 1]);
        assert_eq!(Control::EffectButton(1).message(0), vec![0xB0, 75, 1]);
        assert_eq!(Control::EffectButton(0).message(0), vec![0xB0, 75, 1]);
        assert_eq!(Control::EffectButton(9).message(0), vec![0xB0, 78, 1]);
    }

    #[test]
    fn morph_button_rise_is_cc80_val1() {
        assert_eq!(Control::MorphButton(true).message(0), vec![0xB0, 80, 1]);
        assert_eq!(Control::MorphButton(false).message(0), vec![0xB0, 80, 0]);
    }

    #[test]
    fn slot_enable_uses_spillover_cc_for_dly_rev() {
        assert_eq!(
            Control::SlotEnable {
                slot: ModuleSlot::Rev,
                on: true
            }
            .message(0),
            vec![0xB0, 29, 1]
        );
        assert_eq!(
            Control::SlotEnable {
                slot: ModuleSlot::Dly,
                on: false
            }
            .message(0),
            vec![0xB0, 27, 0]
        );
        assert_eq!(
            Control::SlotEnable {
                slot: ModuleSlot::A,
                on: true
            }
            .message(0),
            vec![0xB0, 17, 1]
        );
        assert_eq!(
            Control::SlotEnable {
                slot: ModuleSlot::X,
                on: true
            }
            .message(0),
            vec![0xB0, 22, 1]
        );
        assert_eq!(
            Control::SlotEnable {
                slot: ModuleSlot::Mod,
                on: false
            }
            .message(0),
            vec![0xB0, 24, 0]
        );
    }

    #[test]
    fn switch_variants_emit_one_or_zero() {
        assert_eq!(Control::RotaryFast(true).message(0), vec![0xB0, 33, 1]);
        assert_eq!(Control::RotaryFast(false).message(0), vec![0xB0, 33, 0]);
        assert_eq!(Control::DelayInfinity(true).message(0), vec![0xB0, 34, 1]);
        assert_eq!(Control::Freeze(true).message(0), vec![0xB0, 35, 1]);
        assert_eq!(Control::ToggleAllModules.message(0), vec![0xB0, 16, 1]);
    }

    #[test]
    fn momentary_variants_press_then_release() {
        // The device abandons a navigation whose button is never released.
        assert_eq!(Control::Up.message(0), vec![0xB0, 48, 1, 0xB0, 48, 0]);
        assert_eq!(Control::Down.message(0), vec![0xB0, 49, 1, 0xB0, 49, 0]);
        assert_eq!(Control::Up.message(3), vec![0xB3, 48, 1, 0xB3, 48, 0]);
    }

    #[test]
    fn program_change_bytes() {
        assert_eq!(Control::ProgramChange(5).message(0), vec![0xC0, 5]);
        assert_eq!(program_change(0, 5), [0xC0, 5]);
        assert_eq!(program_change(2, 0), [0xC2, 0]);
    }

    #[test]
    fn bank_select_is_cc0_then_cc32() {
        assert_eq!(
            Control::BankSelect { msb: 0, lsb: 3 }.message(0),
            vec![0xB0, 0, 0, 0xB0, 32, 3]
        );
    }

    #[test]
    fn bank_preselect_passes_value() {
        assert_eq!(Control::BankPreselect(3).message(0), vec![0xB0, 47, 3]);
    }

    #[test]
    fn channel_is_masked_into_status_byte() {
        assert_eq!(Control::Gain(64).message(15), vec![0xBF, 72, 64]);
        // Channel > 15 wraps into the low nibble.
        assert_eq!(Control::Gain(64).message(16), vec![0xB0, 72, 64]);
        assert_eq!(Control::ProgramChange(0).message(15), vec![0xCF, 0]);
    }

    #[test]
    fn seven_bit_values_are_masked() {
        // 200 = 0xC8 → masked to 0x48 = 72.
        assert_eq!(Control::WahPedal(200).message(0), vec![0xB0, 1, 72]);
        assert_eq!(Control::ProgramChange(200).message(0), vec![0xC0, 72]);
        assert_eq!(
            Control::BankSelect { msb: 130, lsb: 129 }.message(0),
            vec![0xB0, 0, 2, 0xB0, 32, 1]
        );
    }

    #[test]
    fn slot_enable_cc_table() {
        assert_eq!(slot_enable_cc(ModuleSlot::A), 17);
        assert_eq!(slot_enable_cc(ModuleSlot::B), 18);
        assert_eq!(slot_enable_cc(ModuleSlot::C), 19);
        assert_eq!(slot_enable_cc(ModuleSlot::D), 20);
        assert_eq!(slot_enable_cc(ModuleSlot::X), 22);
        assert_eq!(slot_enable_cc(ModuleSlot::Mod), 24);
        assert_eq!(slot_enable_cc(ModuleSlot::Dly), 27);
        assert_eq!(slot_enable_cc(ModuleSlot::Rev), 29);
    }

    #[test]
    fn slot_names_roundtrip() {
        for slot in [ModuleSlot::A, ModuleSlot::Mod, ModuleSlot::Rev] {
            assert_eq!(ModuleSlot::from_name(slot.as_str()), Some(slot));
        }
        assert_eq!(ModuleSlot::from_name("rev"), Some(ModuleSlot::Rev));
        assert_eq!(ModuleSlot::from_name("nope"), None);
    }
}
