import Foundation

// MARK: - The data shape

/// Whether the model has a live stream to the Profiler, and how whole it is.
///
/// The stream link is what "connected" means: without it there is no state.
/// The control link is best-effort by default, so its absence is a lesser
/// condition, ``degraded``, in which everything but the control channel's own
/// addresses (the morph position) keeps working. Every transition raises
/// ``DeviceEvent/connectionChanged(_:)``.
public enum Connection: Sendable, Equatable {
    /// No stream (initial state, the device closed it, or ``DeviceModel/close()``).
    case disconnected
    /// The stream was lost and the model is waiting out the backoff before
    /// dialling again — only with ``ReconnectPolicy/stream`` set. `attempt`
    /// counts from 1 and resets once a dial succeeds.
    case reconnecting(attempt: UInt32)
    /// The stream is open, and the control link is either open or was never
    /// asked for (``ControlPolicy/off``).
    case connected
    /// The stream is open, but the control link was asked for and is
    /// ``ChannelState/unavailable`` or ``ChannelState/lost``. The morph position
    /// stops moving; nothing else is affected.
    case degraded
}

/// Where one of the model's two links is in its life.
public enum ChannelState: Sendable, Equatable {
    /// Not open, and not by the device's doing: before the first attempt,
    /// after ``DeviceModel/close()``, a control link never asked for
    /// (``ControlPolicy/off``), or one the stream took down with it.
    case closed
    /// Dialling, handshaking, or writing the preamble.
    case connecting
    /// Open and being ingested.
    case open
    /// The open failed — the dial, the handshake, the preamble, or (for the
    /// control link) the dump-trigger write.
    case unavailable
    /// Was open, then the device ended the socket. For the stream that is the
    /// end of the life (``Connection/disconnected``, or a reconnect); for the
    /// control link it is ``Connection/degraded`` while the stream stays up.
    case lost
}

/// The state of the model's two links, one per ``Channel``.
public struct Channels: Sendable, Equatable {
    /// The MIDI3 stream — the link ``Connection`` is about.
    public var stream: ChannelState
    /// The CBOR control link, which carries the state dump and the morph.
    public var control: ChannelState

    public init(stream: ChannelState = .closed, control: ChannelState = .closed) {
        self.stream = stream
        self.control = control
    }
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
    /// The morph position changed (0 = base, 16383 = fully morphed).
    ///
    /// Only the control link carries this: the position is never sent on the
    /// MIDI3 stream, even while a morph is ramping, so a model whose control
    /// link is down never raises it. See ``morphButton(on:)``.
    case morphChanged(UInt16)
    /// The morph button was pressed (`true`) or released (`false`) — momentary,
    /// so nothing about it is stored in the snapshot.
    ///
    /// This is what a MIDI3 client sees of a morph: *that* one happened, and
    /// what it did to the audio parameters, but never where the fader sits.
    case morphButton(on: Bool)
    /// The tuner pitch deviance changed (8192 = in tune).
    case tunerDeviance(UInt16)
    /// The tuner's detected note changed.
    case tunerNote(UInt8)
    /// A rendered-string reply arrived (`$3C`), carrying a value's exact display
    /// text. Transient — not stored in the snapshot tree.
    case renderedString(page: UInt8, number: UInt8, value: UInt16, text: String)
    /// The device's current position changed, or was learned for the first
    /// time. Read ``DeviceState/currentBank`` and ``DeviceState/currentRigSlot``
    /// for the new values (both 0-based); a `nil` here is a half not yet known,
    /// not a cleared one.
    case currentPosition(bank: UInt16?, slot: UInt16?)
    /// The stream came up: at connect, and again after a reconnect. Kept
    /// alongside ``connectionChanged(_:)`` for callers that only care about the
    /// two ends of the life.
    case connected
    /// The stream is gone and the model is not going to dial again: the device
    /// closed it under the default ``ReconnectPolicy``, or ``DeviceModel/close()``
    /// was called.
    case disconnected
    /// ``DeviceState/connection`` moved — every transition, including the ones
    /// ``connected`` and ``disconnected`` also announce.
    case connectionChanged(Connection)
    /// One of the two links moved. This is the only event that names a
    /// channel; everything else is one tree, whichever wire fed it.
    case channelChanged(channel: Channel, state: ChannelState)
    /// A sync finished. ``Channel/stream``: the connect-time request burst's
    /// last reply landed, or timed out. ``Channel/control``: the state dump
    /// ended — its last run was folded, or the settle time elapsed.
    case syncCompleted(source: Channel)
    /// A request at `address` (`page * 128 + number`, or the extended address)
    /// drew no reply inside ``Generated/requestTimeoutMs`` and was dropped.
    case requestTimedOut(address: UInt32)
    /// The device reported the rig index the Navigator was aiming at; the aim
    /// is retired and ``DeviceState/navigation`` is empty again.
    case navigationSettled(index: UInt16)
    /// The Navigator gave up on an aim: the device never confirmed it inside
    /// ``Generated/pendingWindowMs`` of its move settling (an index past the
    /// last rig, typically), or the stream was not open to send it.
    case navigationDropped(index: UInt16, reason: NavDrop)
}

/// The result of applying one message to a ``DeviceState``: the granular events
/// it produced, plus whether any **slow** (snapshot) field changed.
public struct ApplyOutcome: Sendable, Equatable {
    /// The typed deltas this message produced, in order.
    public var events: [DeviceEvent]
    /// Whether a slow (snapshot-visible) field changed.
    public var slowChanged: Bool
    /// Every flat rig index this update reported for the Navigator: a
    /// position row that stored — or deduped an unchanged report — with both
    /// halves known and the index inside sixteen bits. A deduped report
    /// raises no event, but the Navigator still needs it: re-loading the rig
    /// already loaded is confirmed by a push that changes nothing.
    public var positions: [UInt16]

    public init(events: [DeviceEvent] = [], slowChanged: Bool = false, positions: [UInt16] = []) {
        self.events = events
        self.slowChanged = slowChanged
        self.positions = positions
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

    /// Fold another outcome into this one: events in order, the snapshot flag
    /// sticky. One `$02` block is several updates but one message.
    mutating func merge(_ other: ApplyOutcome) {
        events.append(contentsOf: other.events)
        slowChanged = slowChanged || other.slowChanged
        positions.append(contentsOf: other.positions)
    }
}

// MARK: - Updates

/// Which wire carried a value: the MIDI3 stream or the CBOR control channel.
///
/// The two are one event universe in two wire formats, but not every row of the
/// routing table trusts both: the control channel carries its own copies of the
/// realtime feeds, which the tree ignores in favour of the stream's, and the
/// morph position only ever arrives on the control channel. See the `wire`
/// column of `spec/state.toml`.
public enum Channel: Sendable, Equatable {
    /// The MIDI3 SysEx stream — the model's stream link.
    case stream
    /// The CBOR control channel — the model's control link, which asks for the
    /// state dump when it opens and then carries live pushes.
    case control
}

/// Whether a value was pushed as it changed, or is one item of the CBOR state
/// dump — a bulk read that can be stale by the time it lands.
public enum Phase: Sendable, Equatable {
    /// The device pushed this because it changed.
    case live
    /// This is one item of the state dump asked for at connect time.
    case dump
}

/// A value decoded off either wire, before the routing table has said what it is.
public enum Decoded: Sendable, Equatable {
    /// A numeric: 14 bits from a `$01`, up to 35 from a `$06` or a CBOR item.
    case num(UInt64)
    /// A string: a `$03`/`$07` tag, or a CBOR `[4, addr, text]` item.
    case text(String)
    /// The values of one `$02` multi-value message, at consecutive addresses
    /// starting from the update's own. Stream only.
    case block([UInt16])
}

/// One value on its way into the tree, tagged with where it came from.
///
/// This is what ``DeviceState/applyUpdate(_:)`` consumes: every entry point —
/// ``DeviceState/apply(_:)``, ``DeviceState/applyCbor(address:value:)``,
/// ``DeviceState/applyCborText(address:text:)`` — is a thin decoder that builds
/// one of these and hands it over, so the routing rules live in exactly one
/// place. `address` is the flat address, `page * 128 + number` for a paged
/// parameter or a bare extended address.
public struct Update: Sendable, Equatable {
    public var source: Channel
    public var phase: Phase
    public var address: UInt32
    public var decoded: Decoded

    public init(source: Channel, phase: Phase, address: UInt32, decoded: Decoded) {
        self.source = source
        self.phase = phase
        self.address = address
        self.decoded = decoded
    }
}

/// The bookkeeping ``DeviceState`` keeps between ``DeviceState/beginDump()``
/// and ``DeviceState/endDump()``: which addresses a live push has written while
/// the state dump is still streaming, so a stale dump item cannot roll one
/// back.
///
/// It is transient, not state: two trees that differ only here show the same
/// snapshot, so equality deliberately ignores it.
struct DumpGuard: Sendable, Equatable {
    /// Between `beginDump` and `endDump`.
    var active = false
    /// The addresses a live update has reached since `beginDump`.
    var touched: Set<UInt32> = []

    static func == (lhs: DumpGuard, rhs: DumpGuard) -> Bool { true }
}

// MARK: - The state tree

/// The immutable device-state snapshot — the store's value type.
///
/// A cheap-to-copy bag of plain data. Callers read fields directly
/// (`state.rig.name`, `state.effects[0].on`, …). The decode logic in
/// ``apply(_:)`` and ``applyUpdate(_:)`` is pure: no IO, no clock, so tests
/// drive it with synthesized messages and hand-built updates. `connection`,
/// `channels` and `navigation` are the one part the model writes on its own,
/// from what its sockets and its Navigator do rather than from anything the
/// device sent.
public struct DeviceState: Sendable, Equatable {
    /// Whether the stream is up, and whether the control link is beside it.
    public var connection: Connection
    /// Where each of the two links is. `connection` summarises this; a client
    /// that wants to know *which* link is down reads it here.
    public var channels: Channels
    /// What the Navigator has outstanding: the rig index last aimed at and not
    /// yet confirmed by the device, and whether a load is in flight. Empty
    /// whenever the device's own position is the whole truth.
    public var navigation: Navigation
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
    /// Current bank, 0-based, once known. Kept live by the `$06` Extended
    /// Parameter the device sends at ``Generated/currentBankAddress`` whenever
    /// the bank changes — from the front panel as readily as from a controller —
    /// by the reply to ``DeviceModel/refreshPosition()``, and by the control
    /// link's copy, which the state dump carries too. Four sources, one row:
    /// the tree dedupes, so a change is reported exactly once.
    public var currentBank: UInt16?
    /// Current rig slot within the bank, 0-based, once known. Same source as
    /// ``currentBank``, at ``Generated/currentRigSlotAddress``; slot 0 is rig
    /// slot 1.
    public var currentRigSlot: UInt16?
    /// The flat, 0-based rig index — `currentBank * bankSlots + currentRigSlot`
    /// — once both halves are known. This is the device's own numbering, and the
    /// only address that can name a rig outside the current bank.
    public var currentRigIndex: UInt16? {
        guard let currentBank, let currentRigSlot else { return nil }
        // Wide math, then bounds-checked: a garbled bank value off the wire
        // must yield no index, not a trap or a wrap into a plausible one.
        let flat = UInt32(currentBank) * UInt32(Params.bankSlots) + UInt32(currentRigSlot)
        return UInt16(exactly: flat)
    }
    /// The flat rig index navigation steps *from*: the Navigator's outstanding
    /// aim while it has one, otherwise ``currentRigIndex``. The device takes a
    /// moment to report a move, so two taps inside that gap would both step
    /// from the same stale index and the second would re-send the first one's
    /// target; stepping from the aim makes them compose.
    public var aimedRigIndex: UInt16? { navigation.aim ?? currentRigIndex }
    /// Latest morph position (0 = base, 16383 = fully morphed), once seen.
    ///
    /// Filled only by the control link — from the state dump it asks for on
    /// opening, then from live pushes — because the MIDI3 stream never carries
    /// the position and never answers a request for it. While the control link
    /// is down (``Connection/degraded``) this holds its last value and stops
    /// moving; with ``ControlPolicy/off`` it stays `nil`.
    public var morph: UInt16?
    /// The most recent realtime status / meter frame (the FAST lane).
    public var status: RealtimeStatus
    /// Dump-phase bookkeeping (``beginDump()``); never part of the snapshot.
    var dumpGuard = DumpGuard()

    /// A fresh, empty state: disconnected with both links closed, nothing
    /// aimed, no rig data, all eight effect slots seeded in signal-chain
    /// order, zeroed meters.
    public init() {
        connection = .disconnected
        channels = Channels()
        navigation = Navigation()
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

// MARK: - Decoding

extension DeviceState {
    /// Parse and apply ONE decoded MIDI message, returning the ``ApplyOutcome``.
    /// Non-Kemper or ignored messages return an empty outcome.
    ///
    /// This is a decoder, not a router: each message becomes one ``Update`` on
    /// the stream channel (a `$02` block becomes several) and goes through
    /// ``applyUpdate(_:)``, where the routing table decides what the tree does
    /// with it. The one message that stays outside the table is the `$3C`
    /// rendered-string reply, which is a transient event and never state.
    ///
    /// - `$01` single-param → a numeric at `page * 128 + number`.
    /// - `$02` multi-param → a block at `page * 128 + number`: the meter frame
    ///   when it sits exactly on the meter block, otherwise consecutive singles.
    /// - `$03` string → a text at `page * 128 + number` (page 0's tags, and the
    ///   bank preview pushed as a plain string).
    /// - `$06` extended param → a numeric at the extended address (the device's
    ///   current bank / rig slot).
    /// - `$07` ext-string → a text at the extended address (how the rig-load
    ///   dump delivers the amp/cab/author tags and the bank preview).
    /// - `$3C` rendered-string reply → a transient event (FAST).
    /// - Anything else → ignored.
    @discardableResult
    public mutating func apply(_ msg: [UInt8]) -> ApplyOutcome {
        switch DeviceState.decode(msg) {
        case .update(let update)?:
            return applyUpdate(update)
        case let .renderedString(page, number, value, text)?:
            return .fast(.renderedString(page: page, number: number, value: value, text: text))
        case nil:
            return .empty
        }
    }

    /// Decode ONE unframed MIDI message as far as the funnel needs it, without
    /// touching the tree. ``apply(_:)`` is this plus the fold; the model's
    /// ingest uses it directly so it can see the address a message names —
    /// that is what answers a pending request, whether or not the value
    /// changed anything.
    static func decode(_ msg: [UInt8]) -> StreamMessage? {
        guard let (header, values) = NrpnHeader.parse(msg) else { return nil }
        let flat = UInt32(header.page) * 128 + UInt32(header.number)

        switch header.function {
        case Generated.fnSingleParam:
            guard values.count >= 2 else { return nil }
            let value = Nrpn.u14(values[0], values[1])
            return .update(Update.stream(flat, .num(UInt64(value))))
        case Generated.fnMultiParam:
            let block = Nrpn.multiValues(number: header.number, values: values).map(\.value)
            return .update(Update.stream(flat, .block(block)))
        case Generated.fnStringParam:
            return .update(Update.stream(flat, .text(Fmt.textUntilNul(values[...]))))
        case Generated.fnExtParam:
            // The extended ($06) and extended-string ($07) addresses are 5×7-bit
            // encoded and do not fit the fixed header layout, so both decode off
            // the raw message. The value spans 35 bits; the row's range check
            // decides whether it fits the field.
            guard let (address, raw) = Nrpn.parseExtendedParam(msg) else { return nil }
            return .update(Update.stream(address, .num(raw)))
        case Generated.fnExtStringParam:
            guard let (address, text) = Nrpn.parseExtendedString(msg) else { return nil }
            return .update(Update.stream(address, .text(text)))
        case Generated.fnRenderedStringReply:
            guard let (page, number, value, text) = Nrpn.parseRenderedString(msg) else {
                return nil
            }
            return .renderedString(page: page, number: number, value: value, text: text)
        default:
            return nil
        }
    }

    /// Fold one **CBOR** address/value into the tree.
    ///
    /// The CBOR channel and the MIDI3 stream are two wire formats over one event
    /// universe, so a value arriving either way lands in the same field and
    /// raises the same event. This is what a live push off the control channel
    /// becomes; the state dump's items go through ``applyUpdate(_:)`` tagged
    /// ``Phase/dump`` so a live push can outrank them (``beginDump()``).
    ///
    /// A negative value is nothing the tree stores and is dropped; a value too
    /// wide for its row is dropped by the row rather than truncated into a
    /// bogus reading.
    @discardableResult
    public mutating func applyCbor(address: UInt32, value: Int64) -> ApplyOutcome {
        guard let value = UInt64(exactly: value) else { return .empty }
        return applyUpdate(
            Update(source: .control, phase: .live, address: address, decoded: .num(value)))
    }

    /// Fold one **CBOR** string item into the tree — the rig name and the other
    /// page-0 tags, or a bank-preview name, as the control channel carries them.
    @discardableResult
    public mutating func applyCborText(address: UInt32, text: String) -> ApplyOutcome {
        applyUpdate(Update(source: .control, phase: .live, address: address, decoded: .text(text)))
    }

    /// Mark the start of a CBOR state dump.
    ///
    /// The dump is a bulk read of a couple of thousand values, and the device
    /// keeps pushing live changes on both channels while it streams. Until
    /// ``endDump()``, a live update remembers its address, and a dump item for an
    /// address a live update has already reached is dropped — the dump's copy is
    /// older than the push, so it must not roll the field back.
    public mutating func beginDump() {
        dumpGuard = DumpGuard(active: true)
    }

    /// Mark the end of a CBOR state dump and forget which addresses live updates
    /// reached during it. After this, dump-tagged items fold exactly like live
    /// ones.
    public mutating func endDump() {
        dumpGuard = DumpGuard()
    }

}

/// One MIDI3 message decoded as far as the funnel needs it: an ``Update`` on
/// the stream channel, or the one message that stays outside the routing table
/// — the `$3C` rendered-string reply, which is a transient event and never
/// state.
enum StreamMessage: Sendable, Equatable {
    case update(Update)
    case renderedString(page: UInt8, number: UInt8, value: UInt16, text: String)
}

extension Update {
    /// A live update off the MIDI3 stream — what every decoded message becomes.
    static func stream(_ address: UInt32, _ decoded: Decoded) -> Update {
        Update(source: .stream, phase: .live, address: address, decoded: decoded)
    }
}
