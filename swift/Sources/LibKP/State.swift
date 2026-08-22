import Foundation

// MARK: - The data shape

/// Whether a live session to the Profiler is currently open.
public enum Connection: Sendable, Equatable {
    /// A session is open and the stream is being ingested.
    case connected
    /// No session (initial state, or the device closed the connection).
    case disconnected
}

/// The loaded rig's metadata and rig-wide settings.
public struct Rig: Sendable, Equatable {
    /// Rig Name (string tag 1).
    public var name: String?
    /// Rig Author (string tag 2).
    public var author: String?
    /// Rig Comment (string tag 4).
    public var comment: String?
    /// Rig Creation Date (string tag 3).
    public var date: String?
    /// Rig Volume (NRPN `0x04/1`, 14-bit), once seen.
    public var volume: UInt16?
    /// Tempo in whole beats per minute (NRPN `0x04/0`, wire value ÷ 64).
    public var tempoBpm: UInt16?

    public init() {}
}

/// The amplifier block.
public struct Amp: Sendable, Equatable {
    /// Amp Name (string tag 10).
    public var name: String?
    /// On/Off state (NRPN `0x0A/2`), once seen.
    public var on: Bool?
    /// Gain (NRPN `0x0A/4`, 14-bit), once seen.
    public var gain: UInt16?

    public init() {}
}

/// The cabinet block.
public struct Cabinet: Sendable, Equatable {
    /// Cabinet Name (string tag 32).
    public var name: String?
    /// On/Off state, once seen.
    public var on: Bool?

    public init() {}
}

/// One effect slot's identity and state within the loaded rig.
public struct Effect: Sendable, Equatable {
    /// Slot short name in signal-chain order (`"A"`…`"REV"`).
    public let slot: String
    /// The slot's address page.
    public let page: UInt8
    /// Effect Type value (effect number 0), if known.
    public var kind: UInt16?
    /// On/Off state (effect number 3), if known.
    public var on: Bool?
    /// Dry/wet Mix (effect number 4, 14-bit), if known.
    public var mix: UInt16?

    public init(slot: String, page: UInt8, kind: UInt16? = nil, on: Bool? = nil, mix: UInt16? = nil)
    {
        self.slot = slot
        self.page = page
        self.kind = kind
        self.on = on
        self.mix = mix
    }

    /// The effect Type's human name, or `nil` if unknown or unmapped.
    public var typeName: String? { kind.flatMap { Params.effectTypeName($0) } }

    /// The effect Type's category, or `nil` if unknown or in no block.
    public var categoryName: String? { kind.flatMap { Params.effectCategoryName($0) } }

    /// True if the slot holds no effect (Type == 0, "empty").
    public var isEmpty: Bool { kind == 0 }
}

/// The tuner readout.
public struct Tuner: Sendable, Equatable {
    /// Detected note index (NRPN `0x7D/0x54`, low 7 bits), once seen.
    public var note: UInt8?
    /// Pitch deviance (NRPN `0x7C/0x0F`; 8192 = perfectly in tune), once seen.
    public var deviance: UInt16?

    public init() {}

    /// Whether the detected pitch is within the in-tune window, or `nil` if no
    /// deviance has been seen yet.
    public var inTune: Bool? {
        guard let deviance else { return nil }
        let delta = abs(Int(deviance) - Int(Generated.tunerInTuneCenter))
        return delta <= Int(Generated.tunerInTuneWindow)
    }
}

/// The global output volumes.
public struct Output: Sendable, Equatable {
    /// Main Output Volume (NRPN `0x7F/0`, 14-bit), once seen.
    public var mainVolume: UInt16?
    /// Headphone Output Volume (NRPN `0x7F/1`, 14-bit), once seen. Driven 1:1 by
    /// the physical Master Volume knob.
    public var headphoneVolume: UInt16?
    /// Monitor Output Volume (NRPN `0x7F/2`, 14-bit), once seen. Driven 1:1 by
    /// the physical Master Volume knob.
    public var monitorVolume: UInt16?

    public init() {}

    /// The physical Master Volume knob's value. The knob is a potentiometer that
    /// drives Headphone (`0x7F/1`) and Monitor (`0x7F/2`) 1:1 under the default
    /// output routing, so report Headphone, falling back to Monitor. This is a
    /// **read-only** readout: the pot has no soft-takeover, so a written value is
    /// authoritative only until the knob next moves.
    public var masterVolume: UInt16? { headphoneVolume ?? monitorVolume }
}

/// One bank slot's preview names — the identity of one of the current bank's
/// five rigs, as shown on the device's rig browser.
public struct BankSlot: Sendable, Equatable {
    /// Rig name in this slot (Bank Preview number 0…4), once seen.
    public var rigName: String?
    /// The rig's amp name (Bank Preview number 5…9), once seen.
    public var ampName: String?
    /// The rig's cabinet name (Bank Preview number 10…14), once seen.
    public var cabinetName: String?

    public init() {}
}

/// The loaded bank's five-slot name preview (page `0x96`). The device pushes the
/// whole block on a bank change, and it is readable on demand via
/// ``DeviceModel/refreshBank()``. `slots[0]` is rig slot 1.
public struct Bank: Sendable, Equatable {
    /// The five preview slots, in slot order (index 0 = slot 1).
    public var slots: [BankSlot]

    public init() {
        slots = Array(repeating: BankSlot(), count: Generated.bankSlots)
    }
}

/// A decoded realtime status / meter frame: the eleven 14-bit values the stream
/// pushes at NRPN `0x7C/78..88` (function `$02`, page `0x7C`, number `0x4E`).
/// The field identities were established by observed experimentation.
public struct RealtimeStatus: Sendable, Equatable {
    /// The eleven raw 14-bit values (v0…v10) in NRPN order.
    public var raw: [UInt16]

    public init(raw: [UInt16] = [UInt16](repeating: 0, count: Generated.meterCount)) {
        precondition(raw.count == Generated.meterCount, "a status frame holds exactly 11 values")
        self.raw = raw
    }

    /// Decode an 11×14-bit value block (MSB/LSB pairs) into a status frame.
    /// Missing trailing values default to 0; extra bytes are ignored.
    public init(values: [UInt8]) {
        var raw = [UInt16](repeating: 0, count: Generated.meterCount)
        var i = 0
        var index = 0
        while i + 1 < values.count && index < raw.count {
            raw[index] = Nrpn.u14(values[i], values[i + 1])
            i += 2
            index += 1
        }
        self.raw = raw
    }

    /// Tuner strobe phase (v3): a wrapping 0–16383 phase whose rotation rate
    /// tracks pitch deviance (stationary = in tune).
    public var strobePhase: UInt16 { raw[Generated.strobePhaseIndex] }
    /// Stack tap level (v4): pre-rig-volume amplitude.
    public var stackLevel: UInt16 { raw[4] }
    /// Rig output level (v6): post-rig-volume amplitude.
    public var rigOutLevel: UInt16 { raw[6] }
    /// Loudness (v9): slow RMS of the output.
    public var loudness: UInt16 { raw[9] }
    /// The three strobe segment drivers (v0/v1/v2).
    public var strobeSegments: [UInt16] { Generated.strobeSegmentIndices.map { raw[$0] } }
    /// True while the tuner block is producing data (any of v0…v3 non-zero).
    public var strobeActive: Bool { raw[0...Generated.strobePhaseIndex].contains { $0 > 0 } }
}

/// A typed change emitted by ``DeviceState/apply(_:)``.
public enum DeviceEvent: Sendable, Equatable {
    /// The loaded rig changed (its Rig Name was (re)applied).
    case rigChanged
    /// A page-0 string tag was applied (1 = Rig Name, 10 = Amp Name, …).
    case stringTag(number: UInt8)
    /// A bank-preview name was applied (page `0x96`): one of the current bank's
    /// rig / amp / cabinet names. Read ``DeviceState/bank`` for the new value.
    /// `number` is the Bank Preview number (0…14): 0–4 rig, 5–9 amp, 10–14 cab.
    case bankPreview(number: UInt8)
    /// An effect slot's Type, On/Off or Mix changed. `slot` indexes
    /// ``Params/effectSlots`` (0 = A … 7 = REV).
    case effectChanged(slot: Int)
    /// A numeric parameter changed that the model does not track specially.
    case paramChanged(page: UInt8, number: UInt8, value: UInt16)
    /// A new realtime status / meter frame arrived.
    case status(RealtimeStatus)
    /// The tempo/beat pulse toggled.
    case beatPulse(on: Bool)
    /// The tempo changed, in whole beats per minute.
    case tempoBpm(UInt16)
    /// The morph position changed (0–16383).
    case morphChanged(UInt16)
    /// The tuner pitch deviance changed (8192 = in tune).
    case tunerDeviance(UInt16)
    /// The tuner's detected note changed.
    case tunerNote(UInt8)
    /// A rendered-string reply arrived (`$3C`), carrying a value's exact display
    /// text. Transient — not stored in the snapshot tree.
    case renderedString(page: UInt8, number: UInt8, value: UInt16, text: String)
    /// The device's current position was learned from the CBOR state-dump
    /// snapshot. Read ``DeviceState/currentBank`` and
    /// ``DeviceState/currentRigSlot`` for the new values (both 0-based).
    case currentPosition(bank: UInt16?, slot: UInt16?)
    /// The model connected to a device.
    case connected
    /// The device closed the connection.
    case disconnected
}

/// The result of applying one message to a ``DeviceState``: the granular events
/// it produced, plus whether any **slow** (snapshot) field changed.
public struct ApplyOutcome: Sendable, Equatable {
    /// The typed deltas this message produced, in order.
    public var events: [DeviceEvent]
    /// Whether a slow (snapshot-visible) field changed.
    public var slowChanged: Bool

    public init(events: [DeviceEvent] = [], slowChanged: Bool = false) {
        self.events = events
        self.slowChanged = slowChanged
    }

    /// Nothing happened.
    static let empty = ApplyOutcome()

    /// One event that changed no slow field (FAST lane or untracked generic).
    static func fast(_ event: DeviceEvent) -> ApplyOutcome {
        ApplyOutcome(events: [event], slowChanged: false)
    }

    /// Events that changed a slow (snapshot-visible) field.
    static func slow(_ events: [DeviceEvent]) -> ApplyOutcome {
        ApplyOutcome(events: events, slowChanged: true)
    }
}

// MARK: - The state tree

/// The immutable device-state snapshot — the store's value type.
///
/// A cheap-to-copy bag of plain data. Callers read fields directly
/// (`state.rig.name`, `state.effects[0].on`, …). The decode logic in
/// ``apply(_:)`` is pure: no IO, no clock, so tests drive it with synthesized
/// messages.
public struct DeviceState: Sendable, Equatable {
    /// Whether a live session is open.
    public var connection: Connection
    /// The loaded rig's metadata and settings.
    public var rig: Rig
    /// The amplifier block.
    public var amp: Amp
    /// The cabinet block.
    public var cabinet: Cabinet
    /// The eight effect slots in signal-chain order (A…REV).
    public var effects: [Effect]
    /// The tuner readout.
    public var tuner: Tuner
    /// The global output volumes.
    public var output: Output
    /// The loaded bank's five-slot name preview (page `0x96`).
    public var bank: Bank
    /// Current bank, 0-based, once known. Seeded from the CBOR state-dump
    /// snapshot (``StateSnapshot/fetch(host:port:timeout:)``) via
    /// ``DeviceModel/setCurrentPosition(bank:slot:)``, then kept live by the
    /// Bank Select / Program Change pair the device sends on every rig change.
    public var currentBank: UInt16?
    /// Current rig slot within the bank, 0-based, once known. Same source as
    /// ``currentBank``; slot 0 is rig slot 1.
    public var currentRigSlot: UInt16?
    /// The high 7 bits of a rig index, held between the Bank Select that carries
    /// them and the Program Change that completes the pair.
    private var pendingRigIndexMsb: UInt8?
    /// The flat, 0-based rig index — `currentBank * bankSlots + currentRigSlot`
    /// — once both halves are known. This is the device's own numbering, and the
    /// only address that can name a rig outside the current bank.
    public var currentRigIndex: UInt16? {
        guard let currentBank, let currentRigSlot else { return nil }
        return currentBank * UInt16(Params.bankSlots) + currentRigSlot
    }
    /// Latest morph position (0–16383), once seen.
    public var morph: UInt16?
    /// The most recent realtime status / meter frame (the FAST lane).
    public var status: RealtimeStatus

    /// A fresh, empty state: disconnected, no rig data, all eight effect slots
    /// seeded in signal-chain order, zeroed meters.
    public init() {
        connection = .disconnected
        rig = Rig()
        amp = Amp()
        cabinet = Cabinet()
        effects = Params.effectSlots.map { Effect(slot: $0.name, page: $0.page) }
        tuner = Tuner()
        output = Output()
        bank = Bank()
        currentBank = nil
        currentRigSlot = nil
        morph = nil
        status = RealtimeStatus()
    }

    /// The effect slot named `slot` (case-insensitive), or `nil` if the name is
    /// not one of A/B/C/D/X/MOD/DLY/REV.
    public func effect(_ slot: String) -> Effect? {
        let upper = slot.uppercased()
        return effects.first { $0.slot == upper }
    }
}

// MARK: - Decode routing

extension DeviceState {
    /// Parse and apply ONE decoded MIDI message, returning the ``ApplyOutcome``.
    /// Non-Kemper or ignored messages return an empty outcome.
    ///
    /// Routing:
    /// - `$03` string on page 0 → rig/amp/cab name tags (SLOW).
    /// - `$01` single-param → beat pulse / effect / amp / rig / output / morph /
    ///   tuner / generic; FAST for the beat pulse and tuner deviance.
    /// - `$02` multi-param → the meter block (FAST) or a rig-load dump, each
    ///   consecutive address routed like a single-param.
    /// - `$07` ext-string on the string page → the amp/cab/author tags the
    ///   rig-load dump sends as extended strings.
    /// - `$3C` rendered-string reply → a transient event (FAST).
    /// - `$06` extended and any other traffic → ignored.
    @discardableResult
    public mutating func apply(_ msg: [UInt8]) -> ApplyOutcome {
        // Channel-voice messages ride the same stream as the SysEx: the device
        // announces every rig change as a Bank Select / Program Change pair.
        if let outcome = applyRigIndex(msg) { return outcome }
        guard let (header, values) = NrpnHeader.parse(msg) else { return .empty }

        // The extended-string ($07) address is 5×7-bit encoded and does not fit
        // the fixed header layout, so route it off the raw message.
        if header.function == Generated.fnExtStringParam { return applyExtString(msg) }

        switch header.function {
        case Generated.fnStringParam where header.page == Generated.pageStrings:
            return applyString(number: header.number, values: values)
        case Generated.fnSingleParam:
            guard values.count >= 2 else { return .empty }
            return routeParam(
                page: header.page, number: header.number, value: Nrpn.u14(values[0], values[1]))
        case Generated.fnMultiParam:
            return applyMulti(page: header.page, number: header.number, values: values)
        case Generated.fnRenderedStringReply:
            return applyRenderedString(msg)
        default:
            return .empty  // $06 extended, and anything else, ignored.
        }
    }

    /// Fold the device's position report into the tree.
    ///
    /// The device says where it is with two channel-voice messages: CC32 (Bank
    /// Select LSB) carrying the high 7 bits, then a Program Change carrying the
    /// low 7. Together they are a flat, 0-based rig index — `128 * msb + program`
    /// — which divides by ``Params/bankSlots`` into bank and slot. Unlike the
    /// CBOR snapshot, which is a one-shot read at connect, this arrives on every
    /// change, from the front panel as readily as from a controller.
    ///
    /// Returns `nil` for anything that is not one of the two messages, so NRPN
    /// parsing carries on. A Program Change with no Bank Select ahead of it is
    /// ignored: half an index is not a position.
    private mutating func applyRigIndex(_ msg: [UInt8]) -> ApplyOutcome? {
        guard let status = msg.first else { return nil }
        switch status & 0xF0 {
        case Generated.controlChangeStatus:
            guard msg.count >= 3, msg[1] == Generated.ccBankSelectLsb else { return nil }
            pendingRigIndexMsb = msg[2] & 0x7F
            return .empty
        case Generated.programChangeStatus:
            guard msg.count >= 2, let msb = pendingRigIndexMsb else { return nil }
            pendingRigIndexMsb = nil
            let index = UInt16(msb) * 128 + UInt16(msg[1] & 0x7F)
            let bank = index / UInt16(Params.bankSlots)
            let slot = index % UInt16(Params.bankSlots)
            guard currentBank != bank || currentRigSlot != slot else { return .empty }
            currentBank = bank
            currentRigSlot = slot
            return .slow([.currentPosition(bank: bank, slot: slot)])
        default:
            return nil
        }
    }

    /// Route a `$03` page-0 string tag into the tree.
    private mutating func applyString(number: UInt8, values: [UInt8]) -> ApplyOutcome {
        let tracked = applyStringTag(number: number, text: Fmt.textUntilNul(values[...]))
        var events: [DeviceEvent] = [.stringTag(number: number)]
        if number == Generated.stringRigName { events.append(.rigChanged) }
        return ApplyOutcome(events: events, slowChanged: tracked)
    }

    /// Store a decoded page-0 string tag into the matching field. Returns `true`
    /// if the tag maps to a tracked field (a slow change).
    private mutating func applyStringTag(number: UInt8, text: String) -> Bool {
        switch number {
        case 1: rig.name = text
        case 2: rig.author = text
        case 3: rig.date = text
        case 4: rig.comment = text
        case 10: amp.name = text
        case 32: cabinet.name = text
        default: return false
        }
        return true
    }

    /// Route a `$07` Extended String Parameter message — how the rig-load dump
    /// delivers the amp/cab/author metadata that plain `$03` strings omit.
    ///
    /// The extended address decodes as `page * 128 + number`; a string on the
    /// string page carries the same tag numbers as `$03`. Other pages are
    /// ignored.
    private mutating func applyExtString(_ msg: [UInt8]) -> ApplyOutcome {
        guard let (address, text) = Nrpn.parseExtendedString(msg) else { return .empty }
        let page = UInt8(truncatingIfNeeded: address / 128)
        let number = UInt8(truncatingIfNeeded: address % 128)
        if page == Generated.pageBankPreview {
            return applyBankPreview(number: number, text: text)
        }
        guard page == Generated.pageStrings else { return .empty }
        let tracked = applyStringTag(number: number, text: text)
        var events: [DeviceEvent] = [.stringTag(number: number)]
        if number == Generated.stringRigName { events.append(.rigChanged) }
        return ApplyOutcome(events: events, slowChanged: tracked)
    }

    /// Store one bank-preview name (page `0x96`) into ``bank``. The number selects
    /// the field (rig / amp / cabinet) and slot; an out-of-range number is
    /// ignored. A stored value is a SLOW change.
    private mutating func applyBankPreview(number: UInt8, text: String) -> ApplyOutcome {
        guard let (field, slot) = Params.bankPreviewSlotField(number) else { return .empty }
        switch field {
        case .rigName: bank.slots[slot].rigName = text
        case .ampName: bank.slots[slot].ampName = text
        case .cabinetName: bank.slots[slot].cabinetName = text
        }
        return .slow([.bankPreview(number: number)])
    }

    /// Route one decoded numeric address/value pair into the tree. Shared by
    /// `$01` single-param and each address of a `$02` rig-load dump.
    private mutating func routeParam(page: UInt8, number: UInt8, value: UInt16) -> ApplyOutcome {
        // Beat pulse: volatile, not stored (FAST lane).
        if page == Generated.pageRealtime && number == Generated.beatPulseNumber {
            return .fast(.beatPulse(on: value != 0))
        }
        // Tuner pitch deviance: volatile (FAST lane).
        if page == Generated.pageRealtime && number == Generated.tunerDevianceNumber {
            tuner.deviance = value
            return .fast(.tunerDeviance(value))
        }
        // Effect Type / On-Off / Mix: fold into the slot (SLOW).
        if Params.isEffectPage(page),
            number == Generated.effectParamType
                || number == Generated.effectParamState
                || number == Generated.effectParamMix
        {
            guard let slot = applyEffect(page: page, number: number, value: value) else {
                return .empty
            }
            return .slow([.effectChanged(slot: slot)])
        }
        // Amplifier On/Off and Gain (SLOW).
        if page == Generated.ampPage && number == Generated.ampOnNumber {
            amp.on = value != 0
            return .slow([.paramChanged(page: page, number: number, value: value)])
        }
        if page == Generated.ampPage && number == Generated.gainNumber {
            amp.gain = value
            return .slow([.paramChanged(page: page, number: number, value: value)])
        }
        // Tempo bpm (the wire value is bpm × 64) and Rig Volume (SLOW).
        if page == Generated.pageRigSettings && number == Generated.tempoNumber {
            let bpm = value / Generated.tempoBpmScale
            rig.tempoBpm = bpm
            return .slow([.tempoBpm(bpm)])
        }
        if page == Generated.pageRigSettings && number == Generated.rigVolumeNumber {
            rig.volume = value
            return .slow([.paramChanged(page: page, number: number, value: value)])
        }
        // System output volumes (SLOW).
        if page == Generated.systemPage && number == Generated.mainVolumeNumber {
            output.mainVolume = value
            return .slow([.paramChanged(page: page, number: number, value: value)])
        }
        if page == Generated.systemPage && number == Generated.headphoneVolumeNumber {
            output.headphoneVolume = value
            return .slow([.paramChanged(page: page, number: number, value: value)])
        }
        if page == Generated.systemPage && number == Generated.monitorVolumeNumber {
            output.monitorVolume = value
            return .slow([.paramChanged(page: page, number: number, value: value)])
        }
        // Morph position (page 0 is dual-use with the string page) (SLOW).
        if page == Generated.pageMorph && number == Generated.morphNumber {
            morph = value
            return .slow([.morphChanged(value)])
        }
        // Tuner detected note (SLOW).
        if page == Generated.pageTunerNote && number == Generated.tunerNoteNumber {
            let note = UInt8(value & 0x7F)
            tuner.note = note
            return .slow([.tunerNote(note)])
        }
        // Untracked generic parameter: the snapshot is unchanged, so this is not
        // a slow change — only the granular event stream sees it.
        return .fast(.paramChanged(page: page, number: number, value: value))
    }

    /// Store a decoded effect-slot parameter into the matching slot. Returns the
    /// slot index if the page is an effect slot.
    private mutating func applyEffect(page: UInt8, number: UInt8, value: UInt16) -> Int? {
        guard let index = Params.effectSlotIndex(page: page) else { return nil }
        switch number {
        case Generated.effectParamType: effects[index].kind = value
        case Generated.effectParamState: effects[index].on = value != 0
        case Generated.effectParamMix: effects[index].mix = value
        default: break
        }
        return index
    }

    /// Route a `$3C` Rendered String reply. It carries a value's exact display
    /// text but is *not* stored in the snapshot tree.
    private func applyRenderedString(_ msg: [UInt8]) -> ApplyOutcome {
        guard let (page, number, value, text) = Nrpn.parseRenderedString(msg) else { return .empty }
        return .fast(.renderedString(page: page, number: number, value: value, text: text))
    }

    /// Route a `$02` multi-param message: the meter block, or a rig-load dump.
    private mutating func applyMulti(page: UInt8, number: UInt8, values: [UInt8]) -> ApplyOutcome {
        if page == Generated.pageRealtime && number == Generated.meterBlockNumber {
            status = RealtimeStatus(values: values)
            return .fast(.status(status))
        }
        var out = ApplyOutcome.empty
        for (num, value) in Nrpn.multiValues(number: number, values: values) {
            let step = routeParam(page: page, number: num, value: value)
            out.events.append(contentsOf: step.events)
            out.slowChanged = out.slowChanged || step.slowChanged
        }
        return out
    }
}
