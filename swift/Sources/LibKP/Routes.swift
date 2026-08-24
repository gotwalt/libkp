import Foundation

// MARK: - The routing table

/// The state routing table, `spec/state.toml`, as the fold consumes it.
///
/// ``Generated/stateRoutes`` is the data: one ``Route`` per tracked flat
/// address, sorted by address. This is the lookup over it. The generated module
/// stays data-only, so the index is built here, once, on first use.
public enum Routes {
    /// The row that tracks `address`, or `nil` if the tree does not track it.
    public static func lookup(_ address: UInt32) -> Route? {
        byAddress[address]
    }

    /// Every row keyed by its address. Addresses are unique across the table,
    /// so the map is lossless.
    private static let byAddress: [UInt32: Route] = Dictionary(
        uniqueKeysWithValues: Generated.stateRoutes.map { ($0.address, $0) })
}

// MARK: - The funnel

/// What a routed value becomes once its row's `kind` has decoded it — the shape
/// the tree stores and compares. A momentary (the beat pulse, the morph button)
/// still decodes; it is just never written anywhere. Internal, not private,
/// so the tests can drive the decode of one row directly.
enum Stored: Equatable {
    /// A `u14`, `u16` or `u7` value, or a `bpm` already divided down.
    case num(UInt16)
    /// A `bool`: nonzero on the wire is on.
    case bool(Bool)
    /// A `text` row's string, a sensitive address already redacted.
    case text(String)
    /// The meter block, decoded as one unit (`multi`).
    case frame([UInt16])

    /// Decode a wire value the way the row's `kind` says (rule 5). `nil` is a
    /// value the row refuses: past 14 bits for a `u14`/`bpm` row, past 16 for a
    /// `u16` row. Those are dropped, never truncated into a bogus reading.
    init?(_ decoded: Decoded, as kind: Route.Kind, at address: UInt32) {
        switch (kind, decoded) {
        case (.u14, .num(let v)):
            guard v <= 16383 else { return nil }
            self = .num(UInt16(v))
        case (.bpm, .num(let v)):
            guard v <= 16383 else { return nil }
            self = .num(UInt16(v) / Generated.tempoBpmScale)
        case (.u16, .num(let v)):
            guard let n = UInt16(exactly: v) else { return nil }
            self = .num(n)
        case (.u7, .num(let v)):
            self = .num(UInt16(v & 0x7F))
        case (.bool, .num(let v)):
            self = .bool(v != 0)
        case (.text, .text(let t)):
            // The state dump volunteers the WiFi credentials in the clear; a
            // routed sensitive address stores the placeholder, never the secret.
            self = .text(Cbor.isSensitive(address) ? Generated.redactedPlaceholder : t)
        case (.multi, .block(let values)):
            self = .frame(values)
        default:
            // A kind/shape mismatch never reaches here: `accepts` screens it.
            return nil
        }
    }

    var num: UInt16? {
        if case .num(let v) = self { return v }
        return nil
    }
    var bool: Bool? {
        if case .bool(let b) = self { return b }
        return nil
    }
    var text: String? {
        if case .text(let t) = self { return t }
        return nil
    }
}

extension Route.Kind {
    /// Whether a value of this shape is what the row stores (rule 4). Page 0 is
    /// dual-use, so a numeric at a `text` row, or a text at a numeric one, is
    /// not this row's value at all. The meter block matches only as a block.
    fileprivate func accepts(_ decoded: Decoded) -> Bool {
        switch (self, decoded) {
        case (.text, .text), (.multi, .block): return true
        case (.u14, .num), (.u16, .num), (.u7, .num), (.bool, .num), (.bpm, .num): return true
        default: return false
        }
    }
}

extension DeviceState {
    /// Fold ONE update into the tree — the funnel every value goes through,
    /// whichever wire carried it. Public so the conformance vectors can drive
    /// it directly with dump-tagged items.
    ///
    /// The rules, in order (see `docs/09` and the header of `spec/state.toml`):
    ///
    /// 1. **Lookup** the row for the address. A `$02` block is the meter frame
    ///    only when it sits exactly on the meter block's base with the block's
    ///    full span; any other block is a run of singles at consecutive
    ///    addresses, each folded on its own.
    /// 2. **No route**: a numeric off the stream at a paged address is still a
    ///    generic ``DeviceEvent/paramChanged(page:number:value:)`` (FAST, no
    ///    state); anything else untracked is silent.
    /// 3. **Wire authority**: a `stream`-only row drops the control channel's
    ///    copy (the realtime feeds and momentaries it carries as a different,
    ///    unwanted feed). A `control`-only row still accepts the stream: the
    ///    morph position never appears there, but if it did it would be real.
    /// 4. **Kind mismatch** — a text at a numeric row or the reverse — is
    ///    untracked, exactly as if there were no row.
    /// 5. **Range / decode** per the row's `kind`; an out-of-range value is
    ///    dropped here, before it can touch anything below.
    /// 6. **Dump authority**: between ``beginDump()`` and ``endDump()`` a live
    ///    update marks its address, and a dump item for a marked address is
    ///    dropped — the push is newer than the dump's copy.
    /// 7. **Dedupe**: a row with `dedupe` that already holds the value is a
    ///    no-op — no event, no snapshot. The momentaries and the meter frame
    ///    never dedupe; their every arrival is the information.
    /// 8. **Store and report**: write the field, raise the row's event; a
    ///    `fast` row is event only, a `slow` row also flags the snapshot.
    @discardableResult
    public mutating func applyUpdate(_ update: Update) -> ApplyOutcome {
        // 1. Lookup.
        let route = Routes.lookup(update.address)
        if case .block(let values) = update.decoded,
            !(route?.kind == .multi && route?.slot == 0 && values.count == Generated.meterCount)
        {
            var out = ApplyOutcome.empty
            for (i, value) in values.enumerated() {
                let element = Update(
                    source: update.source, phase: update.phase,
                    address: update.address + UInt32(i), decoded: .num(UInt64(value)))
                out.merge(applyUpdate(element))
            }
            return out
        }
        // 2. No route.
        guard let route else { return untracked(update) }
        // 3. Wire authority.
        if route.wire == .stream && update.source == .control { return .empty }
        // 4. Kind mismatch.
        guard route.kind.accepts(update.decoded) else { return untracked(update) }
        // 5. Range / decode.
        guard let value = Stored(update.decoded, as: route.kind, at: update.address) else {
            return .empty
        }
        // 6. Dump authority.
        if dumpGuard.active {
            switch update.phase {
            case .live: dumpGuard.touched.insert(update.address)
            case .dump: if dumpGuard.touched.contains(update.address) { return .empty }
            }
        }
        // 7. Dedupe.
        let changed = set(route.field, slot: route.slot, value)
        if route.dedupe && !changed { return .empty }
        // 8. Report.
        let events = events(for: route, value, wire: update.decoded)
        return ApplyOutcome(events: events, slowChanged: route.lane == .slow)
    }

    /// Rule 2: what an untracked value still does. The stream reports any
    /// paged numeric as a generic parameter change so a client can watch
    /// addresses the tree does not model; the control channel's untracked
    /// values, every text, and an extended address are silent. The 14-bit
    /// event cannot carry a wider `$06` value, so one at a paged address is
    /// dropped too.
    private func untracked(_ update: Update) -> ApplyOutcome {
        guard update.source == .stream, case .num(let raw) = update.decoded,
            update.address < 16384, let value = UInt16(exactly: raw)
        else { return .empty }
        return .fast(
            .paramChanged(
                page: UInt8(update.address / 128), number: UInt8(update.address % 128),
                value: value))
    }

    /// Write `value` into the field a row names. Returns whether the field
    /// changed, which is what rule 7 dedupes on — the decoded value, so a `bool`
    /// row arriving as `1` and then `5` is one change.
    ///
    /// Exhaustive over ``Route/Field``: adding a row to `spec/state.toml` fails
    /// to compile until the tree knows where it lands.
    private mutating func set(_ field: Route.Field, slot: UInt8?, _ value: Stored) -> Bool {
        let index = Int(slot ?? 0)
        switch field {
        case .rigName: return Self.write(&rig.name, value.text)
        case .rigAuthor: return Self.write(&rig.author, value.text)
        case .rigDate: return Self.write(&rig.date, value.text)
        case .rigComment: return Self.write(&rig.comment, value.text)
        case .ampName: return Self.write(&amp.name, value.text)
        case .cabinetName: return Self.write(&cabinet.name, value.text)
        case .morphPosition: return Self.write(&morph, value.num)
        case .tempoBpm: return Self.write(&rig.tempoBpm, value.num)
        case .rigVolume: return Self.write(&rig.volume, value.num)
        case .ampOn: return Self.write(&amp.on, value.bool)
        case .ampGain: return Self.write(&amp.gain, value.num)
        case .cabinetOn: return Self.write(&cabinet.on, value.bool)
        case .effectType: return Self.write(&effects[index].kind, value.num)
        case .effectOn: return Self.write(&effects[index].on, value.bool)
        case .effectMix: return Self.write(&effects[index].mix, value.num)
        case .tunerDeviance: return Self.write(&tuner.deviance, value.num)
        case .tunerNote: return Self.write(&tuner.note, value.num.map { UInt8($0) })
        case .mainVolume: return Self.write(&output.mainVolume, value.num)
        case .headphoneVolume: return Self.write(&output.headphoneVolume, value.num)
        case .monitorVolume: return Self.write(&output.monitorVolume, value.num)
        case .bankRigName: return Self.write(&bank.slots[index].rigName, value.text)
        case .bankAmpName: return Self.write(&bank.slots[index].ampName, value.text)
        case .bankCabinetName: return Self.write(&bank.slots[index].cabinetName, value.text)
        case .currentBank: return Self.write(&currentBank, value.num)
        case .currentRigSlot: return Self.write(&currentRigSlot, value.num)
        case .status:
            guard case .frame(let raw) = value else { return false }
            let changed = status.raw != raw
            status = RealtimeStatus(raw: raw)
            return changed
        case .morphButton, .beatPulse:
            // Momentary: the event is the whole story, nothing is stored.
            return true
        }
    }

    /// Store `value` into `field` if it is a change. A `nil` value is a shape
    /// the field cannot hold, which rule 4 has already screened out.
    private static func write<T: Equatable>(_ field: inout T?, _ value: T?) -> Bool {
        guard let value, field != value else { return false }
        field = value
        return true
    }

    /// The event(s) a row raises once its field is written (rule 8). Events
    /// keep their existing names and payloads: a position row reports both
    /// halves as now stored, the rig name says the rig changed after its tag,
    /// and the numeric rows that share the generic event carry the wire value.
    private func events(for route: Route, _ value: Stored, wire: Decoded) -> [DeviceEvent] {
        // The paged rows all sit below 16384, so both halves fit; the position
        // rows are extended addresses and never read these.
        let page = UInt8(truncatingIfNeeded: route.address / 128)
        let number = UInt8(truncatingIfNeeded: route.address % 128)
        switch route.field {
        case .rigName:
            return [.stringTag(number: number), .rigChanged]
        case .rigAuthor, .rigDate, .rigComment, .ampName, .cabinetName:
            return [.stringTag(number: number)]
        case .bankRigName, .bankAmpName, .bankCabinetName:
            return [.bankPreview(number: number)]
        case .effectType, .effectOn, .effectMix:
            return [.effectChanged(slot: Int(route.slot ?? 0))]
        case .currentBank, .currentRigSlot:
            return [.currentPosition(bank: currentBank, slot: currentRigSlot)]
        case .morphPosition:
            return [.morphChanged(value.num ?? 0)]
        case .morphButton:
            return [.morphButton(on: value.bool ?? false)]
        case .tempoBpm:
            return [.tempoBpm(value.num ?? 0)]
        case .beatPulse:
            return [.beatPulse(on: value.bool ?? false)]
        case .tunerDeviance:
            return [.tunerDeviance(value.num ?? 0)]
        case .tunerNote:
            return [.tunerNote(UInt8(value.num ?? 0))]
        case .status:
            return [.status(status)]
        case .rigVolume, .ampOn, .ampGain, .cabinetOn, .mainVolume, .headphoneVolume,
            .monitorVolume:
            // The wire value, as the stream's generic fallback would report it;
            // a `bool` row can arrive wider than 16 bits on the control channel.
            guard case .num(let raw) = wire else { return [] }
            return [.paramChanged(page: page, number: number, value: UInt16(clamping: raw))]
        }
    }
}
