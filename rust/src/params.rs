//! Parameter-name lookups — pure, offline `page/number → human name`.
//!
//! The names themselves are **not** written here: they live in the generated
//! [`crate::generated`] tables, which are produced from the shared `spec/`
//! (sourced from the Kemper MIDI Parameter Documentation and PySwitch, with a
//! handful of realtime addresses established by observed experimentation). This
//! module is only the thin lookup layer over those tables, so all three
//! language implementations answer identically.

use crate::generated;

/// Name of a SysEx function code.
pub fn function_name(function: u8) -> Option<&'static str> {
    lookup_u8(generated::FUNCTION_NAMES, function)
}

/// Name of an address page (section), or `None` if the page is unmapped.
pub fn page_name(page: u8) -> Option<&'static str> {
    lookup_u8(generated::PAGE_NAMES, page)
}

/// Look up a parameter name by page/number.
///
/// Page 0 resolves through the string-tag table; the eight effect-module pages
/// share one parameter map; everything else comes from the per-page table.
pub fn param_name(page: u8, number: u8) -> Option<&'static str> {
    if page == generated::PAGE_STRINGS {
        return string_tag_name(number);
    }
    if is_effect_page(page) {
        return effect_param_name(number);
    }
    generated::NON_EFFECT_PARAMS
        .iter()
        .find(|(p, n, _)| *p == page && *n == number)
        .map(|(_, _, name)| *name)
}

/// The shared parameter name for an effect-module number (Type, Mix, …).
pub fn effect_param_name(number: u8) -> Option<&'static str> {
    lookup_u8(generated::EFFECT_PARAMS, number)
}

/// True if `page` is one of the eight effect-module pages (A..REV).
pub fn is_effect_page(page: u8) -> bool {
    generated::EFFECT_SLOTS.iter().any(|(_, p)| *p == page)
}

/// The eight effect slots in signal-chain order: `(short name, address page)`.
pub fn effect_slots() -> &'static [(&'static str, u8)] {
    generated::EFFECT_SLOTS
}

/// Effect-slot parameter number: the effect Type (value → [`effect_type_name`]).
pub const EFFECT_PARAM_TYPE: u8 = generated::EFFECT_PARAM_TYPE;
/// Effect-slot parameter number: On/Off state (1 = on, 0 = off).
pub const EFFECT_PARAM_STATE: u8 = generated::EFFECT_PARAM_STATE;
/// Effect-slot parameter number: dry/wet Mix (0–16383).
pub const EFFECT_PARAM_MIX: u8 = generated::EFFECT_PARAM_MIX;
/// Effect-slot parameter number: output Volume (0–16383).
pub const EFFECT_PARAM_VOLUME: u8 = generated::EFFECT_PARAM_VOLUME;

/// Resolve a slot name (case-insensitive: `"a"`, `"REV"`, `"dly"`…) to its page.
pub fn effect_slot_page(name: &str) -> Option<u8> {
    generated::EFFECT_SLOTS
        .iter()
        .find(|(n, _)| n.eq_ignore_ascii_case(name))
        .map(|(_, page)| *page)
}

/// Index of the effect slot addressed by `page` within [`effect_slots`].
pub fn effect_slot_index(page: u8) -> Option<usize> {
    generated::EFFECT_SLOTS.iter().position(|(_, p)| *p == page)
}

/// Page 0 is dual-use: string tags via function $03/$43, but *numeric*
/// parameters via $01/$41 (this is how the morph state is read).
pub fn page0_numeric_name(n: u8) -> Option<&'static str> {
    lookup_u8(generated::PAGE0_NUMERIC, n)
}

/// Page holding the loaded bank's five-slot name preview (rig/amp/cabinet).
pub const PAGE_BANK_PREVIEW: u8 = generated::PAGE_BANK_PREVIEW;
/// Number of rig slots in a bank.
pub const BANK_SLOTS: usize = generated::BANK_SLOTS;

/// Which of the three five-slot name groups on [`PAGE_BANK_PREVIEW`] to address.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BankPreviewField {
    /// Rig names (numbers [`generated::BANK_RIG_NAME_BASE`]..).
    RigName,
    /// Amp names (numbers [`generated::BANK_AMP_NAME_BASE`]..).
    AmpName,
    /// Cabinet names (numbers [`generated::BANK_CABINET_NAME_BASE`]..).
    CabinetName,
}

impl BankPreviewField {
    /// First controller number of this group on [`PAGE_BANK_PREVIEW`].
    pub fn base(self) -> u8 {
        match self {
            BankPreviewField::RigName => generated::BANK_RIG_NAME_BASE,
            BankPreviewField::AmpName => generated::BANK_AMP_NAME_BASE,
            BankPreviewField::CabinetName => generated::BANK_CABINET_NAME_BASE,
        }
    }
}

/// Flat NRPN address (`page * 128 + number`) of a bank-preview field for a
/// 1-based `slot` (1..=[`BANK_SLOTS`]). This is the read-on-demand address for
/// [`crate::nrpn::request_extended_string`].
pub fn bank_preview_address(field: BankPreviewField, slot: u8) -> u32 {
    let slot = slot.clamp(1, BANK_SLOTS as u8) - 1;
    PAGE_BANK_PREVIEW as u32 * 128 + field.base() as u32 + slot as u32
}

/// Reverse of [`bank_preview_address`]: map a [`PAGE_BANK_PREVIEW`] number
/// (0..15) to its `(field, 0-based slot index)`, or `None` if out of range.
pub fn bank_preview_slot_field(number: u8) -> Option<(BankPreviewField, usize)> {
    let slots = BANK_SLOTS as u8;
    for field in [
        BankPreviewField::RigName,
        BankPreviewField::AmpName,
        BankPreviewField::CabinetName,
    ] {
        let base = field.base();
        if number >= base && number < base + slots {
            return Some((field, usize::from(number - base)));
        }
    }
    None
}

/// String-tag names on page 0 — function $03/$43 only.
pub fn string_tag_name(n: u8) -> Option<&'static str> {
    lookup_u8(generated::STRING_TAGS, n)
}

/// Effect Type value (effect-module number 0) → name.
pub fn effect_type_name(value: u16) -> Option<&'static str> {
    generated::EFFECT_TYPES
        .binary_search_by_key(&value, |(v, _)| *v)
        .ok()
        .map(|i| generated::EFFECT_TYPES[i].1)
}

/// The category of an effect Type value — the group of the device's type knob it
/// belongs to, derived from the block structure of Appendix B's value ranges.
/// `None` for 0 ("empty") and for values in no block.
pub fn effect_category_name(value: u16) -> Option<&'static str> {
    generated::EFFECT_CATEGORIES
        .iter()
        .find(|c| value >= c.min && value <= c.max)
        .map(|c| c.name)
}

/// Render a "Page: Param" label for a page/number pair, falling back to hex.
pub fn describe(page: u8, number: u8) -> String {
    match (page_name(page), param_name(page, number)) {
        (Some(p), Some(n)) => format!("{p}: {n}"),
        (Some(p), None) => format!("{p}: #{number} (0x{number:02X})"),
        (None, _) => format!("page 0x{page:02X} #{number} (0x{number:02X})"),
    }
}

/// Like [`describe`], but for numeric-function messages ($01/$02/$41), where
/// page 0 addresses numeric parameters instead of string tags.
pub fn describe_numeric(page: u8, number: u8) -> String {
    if page == generated::PAGE_STRINGS {
        return match page0_numeric_name(number) {
            Some(n) => format!("Page 0: {n}"),
            None => format!("Page 0: #{number} (0x{number:02X})"),
        };
    }
    describe(page, number)
}

/// The eleven realtime status fields, in stream order:
/// `(index, NRPN number, id, name, render hint)`.
pub fn meter_fields() -> &'static [(usize, u8, &'static str, &'static str, &'static str)] {
    generated::METER_FIELDS
}

/// Shared `&[(u8, &str)]` table lookup.
fn lookup_u8(table: &'static [(u8, &'static str)], key: u8) -> Option<&'static str> {
    table.iter().find(|(k, _)| *k == key).map(|(_, name)| *name)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_knob_turn_resolves() {
        assert_eq!(param_name(0x09, 0x03), Some("Noise Gate Intensity"));
        assert_eq!(describe(0x09, 0x03), "Input Section: Noise Gate Intensity");
    }

    #[test]
    fn effect_modules_share_the_map() {
        for page in [0x32u8, 0x33, 0x34, 0x35, 0x38, 0x3A, 0x3C, 0x3D] {
            assert!(is_effect_page(page));
            assert_eq!(param_name(page, 4), Some("Mix"));
            assert_eq!(param_name(page, 0), Some("Type"));
        }
        assert!(!is_effect_page(0x04));
    }

    #[test]
    fn key_addresses() {
        assert_eq!(param_name(0x04, 0), Some("Tempo bpm"));
        assert_eq!(param_name(0x0A, 4), Some("Gain"));
        assert_eq!(param_name(0x7F, 0), Some("Main Output Volume"));
        assert_eq!(param_name(0x7D, 88), Some("Looper Record/Playback/Overdub"));
        assert_eq!(param_name(0x00, 1), Some("Rig Name"));
        // gaps stay unknown
        assert_eq!(param_name(0x04, 5), None);
        assert_eq!(param_name(0x7D, 112), None);
    }

    #[test]
    fn effect_types() {
        assert_eq!(effect_type_name(32), Some("Kemper Drive"));
        assert_eq!(effect_type_name(193), Some("Spring Reverb"));
        assert_eq!(effect_type_name(5), None);
        assert_eq!(effect_type_name(0), Some("empty"));
    }

    #[test]
    fn effect_categories() {
        assert_eq!(effect_category_name(16), Some("Wah"));
        assert_eq!(effect_category_name(17), Some("Shaper"));
        // A type with no name still resolves to its block.
        assert_eq!(effect_category_name(76), Some("Modulation"));
        assert_eq!(effect_category_name(0), None);
        assert_eq!(effect_category_name(300), None);
    }

    #[test]
    fn realtime_page_addresses() {
        assert_eq!(page_name(0x7C), Some("Realtime/Meters"));
        assert_eq!(
            param_name(0x7C, 0x4E),
            Some("Tuner Strobe Segment (phase-low)")
        );
        assert_eq!(param_name(0x7C, 81), Some("Tuner Strobe Phase"));
        assert_eq!(param_name(0x7C, 84), Some("Meter: Rig Output Level"));
        assert_eq!(param_name(0x7C, 88), Some("Meter: (unused v10)"));
        assert_eq!(param_name(0x7C, 0), Some("Tempo/Beat Pulse"));
        assert_eq!(param_name(0x7C, 15), Some("Tuner Deviance"));
        assert_eq!(
            describe(0x7C, 0x4E),
            "Realtime/Meters: Tuner Strobe Segment (phase-low)"
        );
    }

    #[test]
    fn dual_use_page_zero() {
        assert_eq!(param_name(0x05, 6), Some("Fixed Noise Gate On/Off"));
        assert_eq!(param_name(0x7D, 84), Some("Tuner Note"));
        assert_eq!(param_name(0x7F, 126), Some("Tuner Mode State"));
        assert_eq!(page0_numeric_name(0x77), Some("Morph Position"));
        assert_eq!(page0_numeric_name(0x50), Some("Morph Button"));
        assert_eq!(describe_numeric(0x00, 0x77), "Page 0: Morph Position");
        // 0x0B is a string tag, and is *not* the morph — see the state tests.
        assert_eq!(page0_numeric_name(0x0B), None);
        assert_eq!(describe(0x00, 0x0B), "String Tags: Amp Author");
    }

    #[test]
    fn slot_helpers() {
        assert_eq!(effect_slot_page("rev"), Some(0x3D));
        assert_eq!(effect_slot_page("A"), Some(0x32));
        assert_eq!(effect_slot_page("nope"), None);
        assert_eq!(effect_slot_index(0x3D), Some(7));
        assert_eq!(effect_slots().len(), 8);
    }

    #[test]
    fn function_names() {
        assert_eq!(function_name(0x01), Some("single-param"));
        assert_eq!(function_name(0x7E), Some("beacon"));
        assert_eq!(function_name(0x55), None);
    }

    #[test]
    fn bank_preview_addresses_and_names() {
        assert_eq!(page_name(0x96), Some("Bank Preview"));
        assert_eq!(param_name(0x96, 0), Some("Bank Rig Name"));
        assert_eq!(param_name(0x96, 7), Some("Bank Amp Name"));
        assert_eq!(param_name(0x96, 14), Some("Bank Cabinet Name"));
        assert_eq!(param_name(0x96, 15), None);
        // Slot 1 rig name is the flat page base; slot 5 cabinet is base+4.
        assert_eq!(bank_preview_address(BankPreviewField::RigName, 1), 19200);
        assert_eq!(
            bank_preview_address(BankPreviewField::CabinetName, 5),
            19214
        );
        // Out-of-range slots clamp into 1..=BANK_SLOTS.
        assert_eq!(bank_preview_address(BankPreviewField::AmpName, 0), 19205);
        assert_eq!(bank_preview_address(BankPreviewField::AmpName, 9), 19209);
        // The reverse map recovers (field, 0-based slot).
        assert_eq!(
            bank_preview_slot_field(3),
            Some((BankPreviewField::RigName, 3))
        );
        assert_eq!(
            bank_preview_slot_field(9),
            Some((BankPreviewField::AmpName, 4))
        );
        assert_eq!(
            bank_preview_slot_field(10),
            Some((BankPreviewField::CabinetName, 0))
        );
        assert_eq!(bank_preview_slot_field(15), None);
    }
}
