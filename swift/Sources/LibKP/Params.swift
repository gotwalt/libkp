import Foundation

// The generated helper structs hold only immutable value members, so they are
// safe to share across concurrency domains. The conformances live here rather
// than in the generated file, which stays a pure data drop.
extension NonEffectKey: @unchecked Sendable {}
extension MeterField: @unchecked Sendable {}
extension EffectCategory: @unchecked Sendable {}

/// Offline name lookups over the generated spec tables.
///
/// The parameter map, effect-type list, string tags and page names all come
/// from the Kemper MIDI Parameter Documentation, with a handful of additions
/// cross-checked against PySwitch. The realtime page's field identities come
/// from observed experimentation. Every table lives in ``Generated``; this type
/// is only the thin lookup layer over it.
public enum Params {
    // MARK: - Effect slots

    /// The eight effect slots in signal-chain order: (short name, address page).
    public static let effectSlots: [(name: String, page: UInt8)] =
        Generated.effectSlots.map { (name: $0.0, page: $0.1) }

    /// Effect-slot parameter number: the effect Type.
    public static let effectParamType: UInt8 = Generated.effectParamType
    /// Effect-slot parameter number: On/Off state (1 = on, 0 = off).
    public static let effectParamState: UInt8 = Generated.effectParamState
    /// Effect-slot parameter number: dry/wet Mix (0–16383).
    public static let effectParamMix: UInt8 = Generated.effectParamMix
    /// Effect-slot parameter number: Volume.
    public static let effectParamVolume: UInt8 = Generated.effectParamVolume

    /// True if `page` is one of the eight effect-module pages (A…REV).
    public static func isEffectPage(_ page: UInt8) -> Bool {
        effectSlots.contains { $0.page == page }
    }

    /// Resolve a slot name (case-insensitive) to its address page.
    public static func effectSlotPage(_ name: String) -> UInt8? {
        let upper = name.uppercased()
        return effectSlots.first { $0.name == upper }?.page
    }

    /// The index of the effect slot on `page` within ``effectSlots``, if any.
    public static func effectSlotIndex(page: UInt8) -> Int? {
        effectSlots.firstIndex { $0.page == page }
    }

    /// The short name of the effect slot on `page`, if any.
    public static func effectSlotName(page: UInt8) -> String? {
        effectSlots.first { $0.page == page }?.name
    }

    // MARK: - Name lookups

    /// The human name of a SysEx function code.
    public static func functionName(_ function: UInt8) -> String? {
        Generated.functionNames[function]
    }

    /// The human name of an address page.
    public static func pageName(_ page: UInt8) -> String? {
        Generated.pageNames[page]
    }

    /// Look up a parameter name by page/number. The eight effect modules share
    /// one map; page 0 resolves to its string tags.
    public static func paramName(page: UInt8, number: UInt8) -> String? {
        if page == Generated.pageStrings { return stringTagName(number) }
        if isEffectPage(page) { return Generated.effectParams[number] }
        return Generated.nonEffectParams[NonEffectKey(page, number)]
    }

    /// String-tag names on page 0 — functions `$03`/`$43` only.
    public static func stringTagName(_ number: UInt8) -> String? {
        Generated.stringTags[number]
    }

    /// Effect Type value (effect-module number 0) → name.
    public static func effectTypeName(_ value: UInt16) -> String? {
        Generated.effectTypes[value]
    }

    /// The category of an effect Type value — the group of the device's type
    /// knob it belongs to, derived from the block structure of Appendix B's
    /// value ranges. `nil` for 0 ("empty") and for values in no block.
    public static func effectCategoryName(_ value: UInt16) -> String? {
        Generated.effectCategories.first { value >= $0.min && value <= $0.max }?.name
    }

    /// Page 0 is dual-use: string tags via `$03`/`$43`, but *numeric* params via
    /// `$01`/`$41` (PySwitch reads the morph state this way).
    public static func page0NumericName(_ number: UInt8) -> String? {
        Generated.page0Numeric[number]
    }

    // MARK: - Bank preview

    /// Page holding the loaded bank's five-slot name preview (rig/amp/cabinet).
    public static let pageBankPreview: UInt8 = Generated.pageBankPreview
    /// Number of rig slots in a bank.
    public static let bankSlots: Int = Generated.bankSlots

    /// Which of the three five-slot name groups on ``pageBankPreview`` to address.
    public enum BankPreviewField: CaseIterable, Sendable {
        /// Rig names (numbers `bankRigNameBase`..).
        case rigName
        /// Amp names (numbers `bankAmpNameBase`..).
        case ampName
        /// Cabinet names (numbers `bankCabinetNameBase`..).
        case cabinetName

        /// First controller number of this group on ``pageBankPreview``.
        public var base: UInt8 {
            switch self {
            case .rigName: return Generated.bankRigNameBase
            case .ampName: return Generated.bankAmpNameBase
            case .cabinetName: return Generated.bankCabinetNameBase
            }
        }
    }

    /// Flat NRPN address (`page * 128 + number`) of a bank-preview field for a
    /// 1-based `slot` (1...``bankSlots``). The read-on-demand address for
    /// ``Nrpn/requestExtendedString(product:device:address:)``.
    public static func bankPreviewAddress(_ field: BankPreviewField, slot: Int) -> UInt32 {
        let clamped = min(max(slot, 1), bankSlots) - 1
        return UInt32(pageBankPreview) * 128 + UInt32(field.base) + UInt32(clamped)
    }

    /// Reverse of ``bankPreviewAddress(_:slot:)``: map a ``pageBankPreview``
    /// number (0..<15) to its `(field, 0-based slot index)`, or `nil` if out of
    /// range.
    public static func bankPreviewSlotField(_ number: UInt8) -> (BankPreviewField, Int)? {
        let slots = UInt8(bankSlots)
        for field in BankPreviewField.allCases
        where number >= field.base && number < field.base + slots {
            return (field, Int(number - field.base))
        }
        return nil
    }

    /// Render a "Page: Param" label for a page/number pair, falling back to hex.
    public static func describe(page: UInt8, number: UInt8) -> String {
        switch (pageName(page), paramName(page: page, number: number)) {
        case let (.some(p), .some(n)):
            return "\(p): \(n)"
        case let (.some(p), .none):
            return String(format: "%@: #%d (0x%02X)", p, Int(number), Int(number))
        case (.none, _):
            return String(format: "page 0x%02X #%d (0x%02X)", Int(page), Int(number), Int(number))
        }
    }

    /// Like ``describe(page:number:)``, but for numeric-function messages
    /// (`$01`/`$02`/`$41`), where page 0 addresses numeric parameters instead of
    /// string tags.
    public static func describeNumeric(page: UInt8, number: UInt8) -> String {
        guard page == Generated.pageStrings else { return describe(page: page, number: number) }
        if let name = page0NumericName(number) { return "Page 0: \(name)" }
        return String(format: "Page 0: #%d (0x%02X)", Int(number), Int(number))
    }

    // MARK: - Meters

    /// The eleven realtime meter fields carried by the status block, in NRPN
    /// order. Identities established by observed experimentation.
    public static let meterFields: [MeterField] = Generated.meterFields
}
