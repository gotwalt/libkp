import Foundation

/// The kind of value a parameter holds — how to interpret its 14-bit value.
public enum ParamKind: Sendable, Equatable {
    /// A fractional value spanning the full 0–16383 range (Gain, Volume…).
    case continuous
    /// An integer switch: 0 = off, non-zero = on (On/Off, *Enable*, *State*).
    case `switch`
    /// An enumerated value that maps to a name via a lookup (effect Type).
    case enumerated
}

/// A typed descriptor for a single parameter address.
///
/// Where ``Params`` answers *"what is this address called?"*, the registry
/// answers *"what kind of value does it hold, and how should a value be
/// shown?"*. It is an offline lookup with no device or network involvement.
public struct ParamDescriptor: Sendable {
    /// Address page (NRPN MSB).
    public let page: UInt8
    /// Address number within the page (NRPN LSB).
    public let number: UInt8
    /// Human-readable name.
    public let name: String
    /// How to interpret the value.
    public let kind: ParamKind
    /// Display unit, if any (reserved).
    public let unit: String?
    /// For ``ParamKind/enumerated``, the value → name lookup.
    public let enumNames: (@Sendable (UInt16) -> String?)?

    public init(
        page: UInt8,
        number: UInt8,
        name: String,
        kind: ParamKind,
        unit: String? = nil,
        enumNames: (@Sendable (UInt16) -> String?)? = nil
    ) {
        self.page = page
        self.number = number
        self.name = name
        self.kind = kind
        self.unit = unit
        self.enumNames = enumNames
    }
}

/// Typed parameter descriptors — a thin semantic layer over ``Params``.
public enum Registry {
    /// Pages that carry a typed descriptor outside the effect slots: rig
    /// settings, input, amp, amp EQ, cabinet, and system/global.
    private static let commonPages: Set<UInt8> = [0x04, 0x09, 0x0A, 0x0B, 0x0C, 0x7F]

    /// The four shared effect-slot parameters that carry a descriptor.
    private static let commonEffectNumbers: Set<UInt8> = [
        Generated.effectParamType,
        Generated.effectParamState,
        Generated.effectParamMix,
        Generated.effectParamVolume,
    ]

    /// Derive a ``ParamKind`` from a parameter's name: On/Off and
    /// *Enable*/*State* parameters are switches, `Type` is an enum, everything
    /// else is continuous.
    static func derivedKind(for name: String) -> ParamKind {
        if name == "Type" { return .enumerated }
        if name.contains("On/Off") || name.contains("Enable") || name.contains("State") {
            return .switch
        }
        return .continuous
    }

    /// Look up a typed descriptor for a page/number address, or `nil` if the
    /// address is outside the seeded common set.
    ///
    /// Covered: amp (`$0A`), amp EQ (`$0B`), cabinet (`$0C`), rig settings
    /// (`$04`), input (`$09`), system/global (`$7F`), and the shared effect-slot
    /// parameters Type (0), On/Off (3), Mix (4), Volume (6) on the eight effect
    /// pages.
    public static func descriptor(page: UInt8, number: UInt8) -> ParamDescriptor? {
        if Params.isEffectPage(page) {
            guard commonEffectNumbers.contains(number),
                  let name = Params.paramName(page: page, number: number)
            else { return nil }
            let kind = derivedKind(for: name)
            var lookup: (@Sendable (UInt16) -> String?)?
            if kind == .enumerated {
                lookup = { value in Params.effectTypeName(value) }
            }
            return ParamDescriptor(
                page: page,
                number: number,
                name: name,
                kind: kind,
                enumNames: lookup
            )
        }
        guard commonPages.contains(page),
              let name = Params.paramName(page: page, number: number)
        else { return nil }
        return ParamDescriptor(page: page, number: number, name: name, kind: derivedKind(for: name))
    }

    /// Format a 14-bit `value` for display according to the descriptor's kind:
    /// - ``ParamKind/switch`` → `"On"` / `"Off"`.
    /// - ``ParamKind/enumerated`` → the enum name, or `"type <value>"`.
    /// - ``ParamKind/continuous`` → a percentage of full scale, e.g. `"42.3%"`.
    ///
    /// The percentage is a generic approximation. For an exact, device-accurate
    /// label (units, curves, note values), request the rendered string from the
    /// device via function `$7C`.
    public static func formatValue(_ descriptor: ParamDescriptor, _ value: UInt16) -> String {
        switch descriptor.kind {
        case .switch:
            return value != 0 ? "On" : "Off"
        case .enumerated:
            return descriptor.enumNames?(value) ?? "type \(value)"
        case .continuous:
            let pct = Double(value) / Double(Generated.fullScale) * 100.0
            return String(format: "%.1f%%", pct)
        }
    }
}
