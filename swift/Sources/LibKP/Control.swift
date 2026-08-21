import Foundation

/// One of the eight Profiler effect-module slots, in signal-chain order.
public enum ModuleSlot: String, CaseIterable, Sendable, Hashable {
    case a = "A"
    case b = "B"
    case c = "C"
    case d = "D"
    case x = "X"
    case mod = "MOD"
    case dly = "DLY"
    case rev = "REV"

    /// The slot's short name (`"A"` … `"REV"`).
    public var name: String { rawValue }

    /// The slot's NRPN address page.
    public var page: UInt8 {
        Generated.effectSlots.first { $0.0 == rawValue }.map(\.1) ?? 0
    }

    /// Resolve a slot name case-insensitively (`"a"`, `"REV"`, `"dly"`…).
    public init?(name: String) {
        let upper = name.uppercased()
        guard let match = ModuleSlot.allCases.first(where: { $0.rawValue == upper }) else {
            return nil
        }
        self = match
    }

    /// The on/off CC for this slot. DLY and REV use their **with-spillover**
    /// CCs (27/29); the no-spillover variants are
    /// `Generated.ccModuleDlyNoSpill` / `Generated.ccModuleRevNoSpill`.
    public var enableCC: UInt8 { Generated.slotEnableCc[rawValue] ?? 0 }
}

/// The on/off CC for an effect slot, from the Kemper MIDI Parameter
/// Documentation's CC map.
public func slotEnableCC(_ slot: ModuleSlot) -> UInt8 { slot.enableCC }

/// A 2-byte MIDI Program Change on `channel` (0–15): `[0xC0|ch, program]`.
/// `program` is masked to 7 bits.
public func programChange(channel: UInt8, program: UInt8) -> [UInt8] {
    Nrpn.programChange(channel: channel, program: program)
}

/// A typed MIDI control-message vocabulary for the Profiler.
///
/// These are the **7-bit** channel-voice messages — Control Change (CC),
/// Program Change (PC), and Bank Select — that the Profiler responds to,
/// distinct from the 14-bit NRPN-over-SysEx traffic in ``Nrpn``. Every CC number
/// comes from the Kemper MIDI Parameter Documentation's controller tables,
/// cross-checked against PySwitch.
///
/// Variants carrying a `UInt8` pass it straight through as the CC value (masked
/// to 7 bits); `Bool` switch variants emit 1 (on/rise/fast/open) or 0.
public enum Control: Equatable, Hashable, Sendable {
    /// Wah pedal position (CC1), 0–127.
    case wahPedal(UInt8)
    /// Pitch pedal position (CC4), 0–127.
    case pitchPedal(UInt8)
    /// Volume pedal position (CC7), 0–127.
    case volumePedal(UInt8)
    /// Panorama (CC10), 0–127.
    case panorama(UInt8)
    /// Morph pedal position (CC11), 0–127.
    case morphPedal(UInt8)
    /// Gain (CC72), 0–127.
    case gain(UInt8)
    /// Delay Mix (CC68), 0–127.
    case delayMix(UInt8)
    /// Delay Feedback (CC69), 0–127.
    case delayFeedback(UInt8)
    /// Reverb Mix (CC70), 0–127.
    case reverbMix(UInt8)
    /// Reverb Time (CC71), 0–127.
    case reverbTime(UInt8)
    /// Monitor (Output) Volume (CC73), 0–127.
    case monitorVolume(UInt8)
    /// Toggle every module A–REV on/off (CC16).
    case toggleAllModules
    /// Enable/disable one effect module. DLY/REV use the with-spillover CC.
    case slotEnable(slot: ModuleSlot, on: Bool)
    /// Rotary speaker speed (CC33): `true` fast / `false` slow.
    case rotaryFast(Bool)
    /// Delay Infinity (CC34): `true` on / `false` off.
    case delayInfinity(Bool)
    /// Delay + Reverb Freeze (CC35): `true` on / `false` off.
    case freeze(Bool)
    /// Tap Tempo (CC30). Any value taps; emits value 1.
    case tapTempo
    /// Tuner Mode (CC31): `true` open / `false` close.
    case tunerMode(Bool)
    /// Bank/Performance preselect (CC47). Value is the bank number − 1.
    case bankPreselect(UInt8)
    /// Performance/Rig up (CC48).
    case up
    /// Performance/Rig down (CC49).
    case down
    /// Load a performance slot 1–5 (CC50–54, value 1). Clamped to 1...5.
    case loadSlot(UInt8)
    /// Press an Effect Button I–IIII (CC75–78, value 1). Clamped to 1...4.
    case effectButton(UInt8)
    /// Morph button (CC80): `true` rise / `false` fall.
    case morphButton(Bool)
    /// Program Change (`0xC0|ch, program`), masked to 7 bits.
    case programChange(UInt8)
    /// Bank Select: the CC0 (MSB) + CC32 (LSB) pair, concatenated.
    case bankSelect(msb: UInt8, lsb: UInt8)

    /// Build the raw MIDI bytes for this control on `channel` (0–15).
    ///
    /// Most variants are a single 3-byte Control Change. ``programChange(_:)``
    /// is 2 bytes; ``bankSelect(msb:lsb:)`` is two Control Changes (CC0 then
    /// CC32) concatenated into 6 bytes. Controller/value bytes are masked to
    /// 7 bits and slot/button indices are clamped to their valid ranges.
    public func message(channel: UInt8 = 0) -> [UInt8] {
        func cc(_ controller: UInt8, _ value: UInt8) -> [UInt8] {
            Nrpn.controlChange(channel: channel, controller: controller, value: value)
        }
        func sw(_ on: Bool) -> UInt8 { on ? 1 : 0 }

        switch self {
        case let .wahPedal(v): return cc(Generated.ccWahPedal, v)
        case let .pitchPedal(v): return cc(Generated.ccPitchPedal, v)
        case let .volumePedal(v): return cc(Generated.ccVolumePedal, v)
        case let .panorama(v): return cc(Generated.ccPanorama, v)
        case let .morphPedal(v): return cc(Generated.ccMorphPedal, v)
        case let .gain(v): return cc(Generated.ccGain, v)
        case let .delayMix(v): return cc(Generated.ccDelayMix, v)
        case let .delayFeedback(v): return cc(Generated.ccDelayFeedback, v)
        case let .reverbMix(v): return cc(Generated.ccReverbMix, v)
        case let .reverbTime(v): return cc(Generated.ccReverbTime, v)
        case let .monitorVolume(v): return cc(Generated.ccMonitorVolume, v)
        case .toggleAllModules: return cc(Generated.ccToggleAllModules, 1)
        case let .slotEnable(slot, on): return cc(slot.enableCC, sw(on))
        case let .rotaryFast(fast): return cc(Generated.ccRotarySpeed, sw(fast))
        case let .delayInfinity(on): return cc(Generated.ccDelayInfinity, sw(on))
        case let .freeze(on): return cc(Generated.ccFreeze, sw(on))
        case .tapTempo: return cc(Generated.ccTapTempo, 1)
        case let .tunerMode(open): return cc(Generated.ccTunerMode, sw(open))
        case let .bankPreselect(bank): return cc(Generated.ccBankPreselect, bank)
        case .up: return cc(Generated.ccUp, 1)
        case .down: return cc(Generated.ccDown, 1)
        case let .loadSlot(slot):
            let n = min(max(slot, 1), 5)
            return cc(Generated.ccLoadSlot1 + (n - 1), 1)
        case let .effectButton(button):
            let n = min(max(button, 1), 4)
            return cc(Generated.ccEffectButtonI + (n - 1), 1)
        case let .morphButton(rise): return cc(Generated.ccMorphButton, sw(rise))
        case let .programChange(program):
            return Nrpn.programChange(channel: channel, program: program)
        case let .bankSelect(msb, lsb):
            return cc(Generated.ccBankSelectMsb, msb) + cc(Generated.ccBankSelectLsb, lsb)
        }
    }
}
