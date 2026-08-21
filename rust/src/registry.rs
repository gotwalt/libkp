//! Typed parameter descriptors — a thin semantic layer over the pure name
//! lookups in [`crate::params`]. Where `params` answers *"what is this address
//! called?"*, the registry answers *"what kind of value does it hold, and how
//! should a value be shown?"*.
//!
//! This is an offline lookup with no device or network involvement. It covers a
//! **common** set of addresses (amp, amp EQ, cabinet, rig settings, input,
//! system/global) plus the four shared effect-slot parameters (Type, On/Off,
//! Mix, Volume). It intentionally does not enumerate every one of the 100+
//! per-effect numbers — the full name map for those lives in
//! [`crate::params::param_name`].

use crate::params;

/// The kind of value a parameter holds — how to interpret its 14-bit value.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ParamKind {
    /// A fractional value spanning the full 0–16383 range (Gain, Volume…).
    Continuous,
    /// An integer switch: 0 = off, non-zero = on (On/Off, *Enable*, *State*).
    Switch,
    /// An enumerated value that maps to a name via a lookup fn (effect Type).
    Enum,
}

/// A typed descriptor for a single parameter address.
#[derive(Debug, Clone, Copy)]
pub struct ParamDescriptor {
    /// Address page (NRPN MSB).
    pub page: u8,
    /// Address number within the page (NRPN LSB).
    pub number: u8,
    /// Human-readable name (from [`crate::params`]).
    pub name: &'static str,
    /// How to interpret the value.
    pub kind: ParamKind,
    /// Display unit, if any (currently unused by the covered set; reserved).
    pub unit: Option<&'static str>,
    /// For [`ParamKind::Enum`], the value→name lookup (e.g.
    /// [`crate::params::effect_type_name`]); `None` for other kinds.
    pub enum_names: Option<fn(u16) -> Option<&'static str>>,
}

/// Derive a [`ParamKind`] from a parameter's name: On/Off and *Enable*/*State*
/// parameters are switches, `Type` is an enum, everything else is continuous.
fn derive_kind(name: &str) -> ParamKind {
    if name == "Type" {
        ParamKind::Enum
    } else if name.contains("On/Off") || name.contains("Enable") || name.contains("State") {
        ParamKind::Switch
    } else {
        ParamKind::Continuous
    }
}

/// Look up a typed descriptor for a page/number address, or `None` if the
/// address is outside the covered common set.
///
/// Covered: amp ($0A), amp EQ ($0B), cabinet ($0C), rig settings ($04), input
/// ($09), system/global ($7F), and the shared effect-slot parameters Type (0),
/// On/Off (3), Mix (4), Volume (6) on the eight effect pages.
pub fn descriptor(page: u8, number: u8) -> Option<ParamDescriptor> {
    if params::is_effect_page(page) {
        // Only the four common effect-slot parameters are typed here.
        if !matches!(
            number,
            params::EFFECT_PARAM_TYPE
                | params::EFFECT_PARAM_STATE
                | params::EFFECT_PARAM_MIX
                | params::EFFECT_PARAM_VOLUME
        ) {
            return None;
        }
        let name = params::param_name(page, number)?;
        let kind = derive_kind(name);
        let enum_names = if kind == ParamKind::Enum {
            Some(params::effect_type_name as fn(u16) -> Option<&'static str>)
        } else {
            None
        };
        return Some(ParamDescriptor {
            page,
            number,
            name,
            kind,
            unit: None,
            enum_names,
        });
    }

    // Common non-effect pages: reuse the params name map and derive the kind.
    match page {
        0x04 | 0x09 | 0x0A | 0x0B | 0x0C | 0x7F => {
            let name = params::param_name(page, number)?;
            Some(ParamDescriptor {
                page,
                number,
                name,
                kind: derive_kind(name),
                unit: None,
                enum_names: None,
            })
        }
        _ => None,
    }
}

/// Format a 14-bit `value` for display according to the descriptor's kind:
/// - [`ParamKind::Switch`] → `"On"` / `"Off"`.
/// - [`ParamKind::Enum`] → the enum name, or `"type {value}"` if unknown.
/// - [`ParamKind::Continuous`] → a percentage of full scale, e.g. `"42.3%"`.
///
/// The percentage is a generic approximation. For an exact, device-accurate
/// label (units, curves, note values), request the rendered string from the
/// device via [`crate::nrpn::request_rendered_string`] (function $7C → $3C).
pub fn format_value(desc: &ParamDescriptor, value: u16) -> String {
    match desc.kind {
        ParamKind::Switch => {
            if value != 0 {
                "On".to_string()
            } else {
                "Off".to_string()
            }
        }
        ParamKind::Enum => match desc.enum_names.and_then(|f| f(value)) {
            Some(name) => name.to_string(),
            None => format!("type {value}"),
        },
        ParamKind::Continuous => {
            let pct = value as f32 / crate::generated::FULL_SCALE as f32 * 100.0;
            format!("{pct:.1}%")
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn known_addresses_resolve() {
        let gain = descriptor(0x0A, 4).unwrap();
        assert_eq!(gain.name, "Gain");
        assert_eq!(gain.kind, ParamKind::Continuous);

        let amp_onoff = descriptor(0x0A, 2).unwrap();
        assert_eq!(
            (amp_onoff.name, amp_onoff.kind),
            ("On/Off", ParamKind::Switch)
        );

        let rig_vol = descriptor(0x04, 1).unwrap();
        assert_eq!(
            (rig_vol.name, rig_vol.kind),
            ("Rig Volume", ParamKind::Continuous)
        );

        let tempo_en = descriptor(0x04, 2).unwrap();
        assert_eq!(
            (tempo_en.name, tempo_en.kind),
            ("Tempo Enable", ParamKind::Switch)
        );

        let sysvol = descriptor(0x7F, 0).unwrap();
        assert_eq!(
            (sysvol.name, sysvol.kind),
            ("Main Output Volume", ParamKind::Continuous)
        );
    }

    #[test]
    fn effect_slot_params_only_common_four() {
        let ty = descriptor(0x3D, 0).unwrap();
        assert_eq!((ty.name, ty.kind), ("Type", ParamKind::Enum));
        assert_eq!(descriptor(0x3D, 3).unwrap().kind, ParamKind::Switch); // On/Off
        assert_eq!(descriptor(0x3D, 4).unwrap().name, "Mix");
        assert_eq!(descriptor(0x3D, 6).unwrap().name, "Volume");
        // an effect number outside the common four is not typed here
        assert!(descriptor(0x3D, 7).is_none());
        // realtime / undocumented pages have no descriptor
        assert!(descriptor(0x7C, 84).is_none());
    }

    #[test]
    fn format_switch_enum_continuous() {
        let sw = descriptor(0x0A, 2).unwrap();
        assert_eq!(format_value(&sw, 1), "On");
        assert_eq!(format_value(&sw, 0), "Off");

        let ty = descriptor(0x32, 0).unwrap();
        assert_eq!(format_value(&ty, 32), "Kemper Drive");
        assert_eq!(format_value(&ty, 5), "type 5"); // unknown value

        let cont = descriptor(0x0A, 4).unwrap();
        assert_eq!(format_value(&cont, 6925), "42.3%");
        assert_eq!(format_value(&cont, 0), "0.0%");
        assert_eq!(format_value(&cont, 16383), "100.0%");
    }
}
