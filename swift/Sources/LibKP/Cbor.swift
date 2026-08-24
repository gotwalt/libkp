import Foundation

// MARK: - Value

/// A decoded CBOR (RFC 8949) data item.
///
/// The device's native control channel (``Session/protocolCborControl``) speaks
/// CBOR rather than MIDI3 frames — see `docs/06`. This is one item off that
/// stream. `neg` holds the *actual* negative value (CBOR encodes `-1 - n`); the
/// values seen on this channel fit comfortably in `Int64`.
public indirect enum CBORValue: Sendable, Equatable {
    case uint(UInt64)
    case neg(Int64)
    case bytes([UInt8])
    case text(String)
    case array([CBORValue])
    case map([Pair])
    case tag(UInt64, CBORValue)
    case bool(Bool)
    case null
    case undefined
    case simple(UInt8)
    case float(Double)
    /// The `0xFF` break stop-code; only valid inside an indefinite-length item.
    case breakStop

    /// One CBOR map entry (a nominal type so ``CBORValue`` stays `Equatable`).
    public struct Pair: Sendable, Equatable {
        public let key: CBORValue
        public let value: CBORValue
        public init(_ key: CBORValue, _ value: CBORValue) {
            self.key = key
            self.value = value
        }
    }

    /// The item as an integer, for the `[selector, addr, value]` shapes.
    public var asInt: Int64? {
        switch self {
        case .uint(let u): return Int64(exactly: u)
        case .neg(let n): return n
        default: return nil
        }
    }

    /// The elements, if this is an array (unwrapping one layer of tag first).
    public var asArray: [CBORValue]? {
        switch self {
        case .array(let items): return items
        case .tag(_, let inner): return inner.asArray
        default: return nil
        }
    }
}

// MARK: - Decoder

/// A streaming CBOR reader: feed it TCP chunks, take whole items back.
///
/// The channel pads between top-level items with runs of the filler byte
/// ``Generated/cborFillerByte`` (`0xC0`) — a `tag(0)` head with no content, which
/// is not well-formed CBOR on its own. Parsed naively it swallows the following
/// item, so the decoder skips it. A genuine `tag(0)` is an RFC 8949 date/time
/// whose content must be a text string, so a `0xC0` is treated as filler only
/// when the next head is *not* a text head, leaving real datetimes intact.
public struct CBORDecoder: Sendable {
    private var buffer: [UInt8] = []
    private var fillerCount = 0

    public init() {}

    /// Bytes buffered but not yet forming a complete item.
    public var pending: Int { buffer.count }

    /// How many filler bytes have been skipped over this stream's life.
    public var fillerBytes: Int { fillerCount }

    /// Feed raw stream bytes; return every item completed by this input.
    ///
    /// A byte that cannot start a valid item is dropped to resync (the same
    /// strategy ``Midi3/Unframer`` uses), so a mid-stream join or a wrong-protocol
    /// guess degrades to noise rather than a permanent stall.
    public mutating func push(_ data: [UInt8]) -> [CBORValue] {
        buffer.append(contentsOf: data)
        var out: [CBORValue] = []
        var off = 0
        // `skipFiller` returning nil means a trailing filler byte we cannot
        // classify until more bytes arrive — stop and keep it buffered.
        loop: while let next = skipFiller(from: off) {
            off = next
            switch parseItem(buffer, off, 0) {
            case .success(let value, let used):
                off += used
                out.append(value)
            case .failure(.incomplete):
                break loop
            case .failure(.invalid):
                off += 1  // resync
            }
        }
        if off > 0 { buffer.removeFirst(off) }
        return out
    }

    /// Step past any inter-item filler. `nil` means the buffer ends on a filler
    /// byte whose role cannot be decided until the next byte arrives.
    private mutating func skipFiller(from start: Int) -> Int? {
        var off = start
        while off < buffer.count && buffer[off] == Generated.cborFillerByte {
            guard off + 1 < buffer.count else { return nil }
            if isTextHead(buffer[off + 1]) { return off }  // real datetime tag
            off += 1
            fillerCount += 1
        }
        return off
    }
}

/// Heads that open a text string (major type 3), definite or indefinite.
private func isTextHead(_ b: UInt8) -> Bool { b >> 5 == 3 }

/// Nesting limit — guards the recursive descent against hostile/desynced input.
private let maxDepth = 32

private enum CBORParseError: Error {
    /// Ran out of bytes mid-item — wait for more of the stream.
    case incomplete
    /// Not valid CBOR (reserved additional-info, bad UTF-8, nesting too deep).
    case invalid
}

/// The outcome of parsing one item: the item and bytes consumed, or why not.
private enum ParseResult {
    case success(CBORValue, Int)
    case failure(CBORParseError)
}

/// Parse one item from `b` starting at `off`. `used` is relative to `off`.
private func parseItem(_ b: [UInt8], _ off: Int, _ depth: Int) -> ParseResult {
    if depth > maxDepth { return .failure(.invalid) }
    guard off < b.count else { return .failure(.incomplete) }
    let head = b[off]
    let major = head >> 5
    let ai = head & 0x1F

    // Additional info 31 = indefinite length (or the break stop-code).
    if ai == 31 {
        switch major {
        case 2: return parseIndefiniteChunks(b, off, depth, bytesMode: true)
        case 3: return parseIndefiniteChunks(b, off, depth, bytesMode: false)
        case 4: return parseIndefiniteArray(b, off, depth)
        case 5: return parseIndefiniteMap(b, off, depth)
        case 7: return .success(.breakStop, 1)
        default: return .failure(.invalid)
        }
    }

    let arg: UInt64
    let headLen: Int
    switch parseArgument(b, off, ai) {
    case .success(let parsed): arg = parsed.0; headLen = parsed.1
    case .failure(let e): return .failure(e)
    }
    let rest = off + headLen

    switch major {
    case 0:
        return .success(.uint(arg), headLen)
    case 1:
        return .success(.neg(-1 - Int64(bitPattern: arg)), headLen)
    case 2, 3:
        guard let n = Int(exactly: arg) else { return .failure(.invalid) }
        guard rest + n <= b.count else { return .failure(.incomplete) }
        let slice = Array(b[rest..<(rest + n)])
        if major == 2 {
            return .success(.bytes(slice), headLen + n)
        }
        guard let text = String(bytes: slice, encoding: .utf8) else { return .failure(.invalid) }
        return .success(.text(text), headLen + n)
    case 4:
        guard let n = Int(exactly: arg) else { return .failure(.invalid) }
        var items: [CBORValue] = []
        items.reserveCapacity(min(n, 64))
        var cursor = off + headLen
        for _ in 0..<n {
            switch parseItem(b, cursor, depth + 1) {
            case .success(let v, let used): cursor += used; items.append(v)
            case .failure(let e): return .failure(e)
            }
        }
        return .success(.array(items), cursor - off)
    case 5:
        guard let n = Int(exactly: arg) else { return .failure(.invalid) }
        var pairs: [CBORValue.Pair] = []
        pairs.reserveCapacity(min(n, 64))
        var cursor = off + headLen
        for _ in 0..<n {
            let key: CBORValue
            switch parseItem(b, cursor, depth + 1) {
            case .success(let v, let used): cursor += used; key = v
            case .failure(let e): return .failure(e)
            }
            switch parseItem(b, cursor, depth + 1) {
            case .success(let v, let used): cursor += used; pairs.append(.init(key, v))
            case .failure(let e): return .failure(e)
            }
        }
        return .success(.map(pairs), cursor - off)
    case 6:
        switch parseItem(b, rest, depth + 1) {
        case .success(let inner, let used): return .success(.tag(arg, inner), headLen + used)
        case .failure(let e): return .failure(e)
        }
    case 7:
        return parseSimple(ai, arg, headLen)
    default:
        return .failure(.invalid)
    }
}

/// Read the head byte's argument. `headLen` includes the head byte.
private func parseArgument(
    _ b: [UInt8], _ off: Int, _ ai: UInt8
) -> Result<(UInt64, Int), CBORParseError> {
    let width: Int
    switch ai {
    case 0...23: return .success((UInt64(ai), 1))
    case 24: width = 1
    case 25: width = 2
    case 26: width = 4
    case 27: width = 8
    default: return .failure(.invalid)  // 28...30 are reserved
    }
    guard off + 1 + width <= b.count else { return .failure(.incomplete) }
    var arg: UInt64 = 0
    for i in 0..<width { arg = (arg << 8) | UInt64(b[off + 1 + i]) }
    return .success((arg, 1 + width))
}

/// Major type 7: booleans, null, undefined, simple values and floats.
private func parseSimple(_ ai: UInt8, _ arg: UInt64, _ headLen: Int) -> ParseResult {
    switch ai {
    case 20: return .success(.bool(false), headLen)
    case 21: return .success(.bool(true), headLen)
    case 22: return .success(.null, headLen)
    case 23: return .success(.undefined, headLen)
    case 0...19, 24: return .success(.simple(UInt8(truncatingIfNeeded: arg)), headLen)
    case 25: return .success(.float(f16ToDouble(UInt16(truncatingIfNeeded: arg))), headLen)
    case 26:
        return .success(.float(Double(Float(bitPattern: UInt32(truncatingIfNeeded: arg)))), headLen)
    case 27: return .success(.float(Double(bitPattern: arg)), headLen)
    default: return .failure(.invalid)
    }
}

/// Indefinite-length byte/text string: definite chunks until the break code.
private func parseIndefiniteChunks(
    _ b: [UInt8], _ off: Int, _ depth: Int, bytesMode: Bool
) -> ParseResult {
    var cursor = off + 1
    var acc: [UInt8] = []
    while true {
        guard cursor < b.count else { return .failure(.incomplete) }
        if b[cursor] == 0xFF {
            cursor += 1
            break
        }
        switch parseItem(b, cursor, depth + 1) {
        case .success(.bytes(let chunk), let used) where bytesMode:
            acc.append(contentsOf: chunk)
            cursor += used
        case .success(.text(let chunk), let used) where !bytesMode:
            acc.append(contentsOf: Array(chunk.utf8))
            cursor += used
        case .success: return .failure(.invalid)  // chunks must match the outer type
        case .failure(let e): return .failure(e)
        }
    }
    if bytesMode { return .success(.bytes(acc), cursor - off) }
    guard let text = String(bytes: acc, encoding: .utf8) else { return .failure(.invalid) }
    return .success(.text(text), cursor - off)
}

private func parseIndefiniteArray(_ b: [UInt8], _ off: Int, _ depth: Int) -> ParseResult {
    var cursor = off + 1
    var items: [CBORValue] = []
    while true {
        guard cursor < b.count else { return .failure(.incomplete) }
        if b[cursor] == 0xFF {
            cursor += 1
            break
        }
        switch parseItem(b, cursor, depth + 1) {
        case .success(let v, let used): cursor += used; items.append(v)
        case .failure(let e): return .failure(e)
        }
    }
    return .success(.array(items), cursor - off)
}

private func parseIndefiniteMap(_ b: [UInt8], _ off: Int, _ depth: Int) -> ParseResult {
    var cursor = off + 1
    var pairs: [CBORValue.Pair] = []
    while true {
        guard cursor < b.count else { return .failure(.incomplete) }
        if b[cursor] == 0xFF {
            cursor += 1
            break
        }
        let key: CBORValue
        switch parseItem(b, cursor, depth + 1) {
        case .success(let v, let used): cursor += used; key = v
        case .failure(let e): return .failure(e)
        }
        switch parseItem(b, cursor, depth + 1) {
        case .success(let v, let used): cursor += used; pairs.append(.init(key, v))
        case .failure(let e): return .failure(e)
        }
    }
    return .success(.map(pairs), cursor - off)
}

/// IEEE-754 half precision → Double (CBOR additional info 25).
private func f16ToDouble(_ bits: UInt16) -> Double {
    let sign = (bits & 0x8000) != 0 ? -1.0 : 1.0
    let exp = Int((bits >> 10) & 0x1F)
    let frac = Double(bits & 0x03FF)
    let mag: Double
    switch exp {
    case 0: mag = frac * pow(2.0, -24)  // subnormal
    case 31 where frac == 0: mag = .infinity
    case 31: mag = .nan
    default: mag = (frac / 1024.0 + 1.0) * pow(2.0, Double(exp - 15))
    }
    return sign * mag
}

// MARK: - Encode / snapshot

/// The reader and writer for the device's native CBOR channel: the item
/// encoder, the one item this library ever writes (the state-dump trigger),
/// and the decode from a raw item to the addresses and values the tree folds.
public enum Cbor {
    /// Encode a value as a fresh byte array, using minimal-length integer heads —
    /// the shortest head that fits each argument, as the device itself emits.
    public static func encode(_ value: CBORValue) -> [UInt8] {
        var out: [UInt8] = []
        encode(value, into: &out)
        return out
    }

    private static func encode(_ value: CBORValue, into out: inout [UInt8]) {
        switch value {
        case .uint(let u): writeHead(0, u, &out)
        case .neg(let n): writeHead(1, UInt64(-1 - n), &out)  // CBOR encodes -1-n
        case .bytes(let b):
            writeHead(2, UInt64(b.count), &out)
            out.append(contentsOf: b)
        case .text(let t):
            let u = Array(t.utf8)
            writeHead(3, UInt64(u.count), &out)
            out.append(contentsOf: u)
        case .array(let items):
            writeHead(4, UInt64(items.count), &out)
            for item in items { encode(item, into: &out) }
        case .map(let pairs):
            writeHead(5, UInt64(pairs.count), &out)
            for pair in pairs {
                encode(pair.key, into: &out)
                encode(pair.value, into: &out)
            }
        case .tag(let t, let inner):
            writeHead(6, t, &out)
            encode(inner, into: &out)
        case .bool(false): out.append(0xF4)
        case .bool(true): out.append(0xF5)
        case .null: out.append(0xF6)
        case .undefined: out.append(0xF7)
        case .simple(let s): writeHead(7, UInt64(s), &out)
        case .float(let f):
            out.append(0xFB)
            appendBigEndian(f.bitPattern, 8, &out)
        case .breakStop: out.append(0xFF)
        }
    }

    /// Write a CBOR head: the 3-bit major type plus the shortest argument
    /// encoding that fits.
    private static func writeHead(_ major: UInt8, _ arg: UInt64, _ out: inout [UInt8]) {
        let m = major << 5
        switch arg {
        case 0...23: out.append(m | UInt8(arg))
        case 24...0xFF: out.append(contentsOf: [m | 24, UInt8(arg)])
        case 0x100...0xFFFF:
            out.append(m | 25)
            appendBigEndian(arg, 2, &out)
        case 0x1_0000...0xFFFF_FFFF:
            out.append(m | 26)
            appendBigEndian(arg, 4, &out)
        default:
            out.append(m | 27)
            appendBigEndian(arg, 8, &out)
        }
    }

    private static func appendBigEndian(_ value: UInt64, _ width: Int, _ out: inout [UInt8]) {
        for i in stride(from: width - 1, through: 0, by: -1) {
            out.append(UInt8(truncatingIfNeeded: value >> (UInt64(i) * 8)))
        }
    }

    /// An integer as the right ``CBORValue`` variant for its sign.
    private static func int(_ n: Int64) -> CBORValue {
        n < 0 ? .neg(n) : .uint(UInt64(n))
    }

    /// Build a single-parameter write, `tag(1)([1, addr, value])` — the shape the
    /// channel uses to set a parameter (`docs/06`).
    ///
    /// This is a **write**: the device applies it and rebroadcasts it to every
    /// other open session. The one this library sends —
    /// `paramWrite(``Generated/stateDumpTriggerAddress``, 1)` — is non-mutating and
    /// asks the device for its full state.
    public static func paramWrite(addr: UInt32, value: Int64) -> CBORValue {
        .tag(
            Generated.cborItemTag,
            .array([.uint(UInt64(Generated.cborSelectorSingle)), .uint(UInt64(addr)), int(value)]))
    }

    /// The item that asks the device for its full parameter state.
    public static func stateDumpRequest() -> CBORValue {
        paramWrite(addr: Generated.stateDumpTriggerAddress, value: Generated.stateDumpTriggerValue)
    }

    /// Is `addr` one whose string value is a device secret (WiFi credentials)?
    /// The state dump volunteers these in the clear; a reader must never surface
    /// them. See ``Generated/sensitiveAddresses``.
    public static func isSensitive(_ addr: UInt32) -> Bool {
        Generated.sensitiveAddresses.contains(addr)
    }

    /// Read the current bank, rig slot, morph and string parameters out of
    /// decoded dump items.
    ///
    /// The items are folded into a scratch ``DeviceState`` — the same routing
    /// table a live session uses, so the dump cannot disagree with the tree
    /// about what an address means — and the snapshot's fields are read off it.
    /// The strings are collected alongside, in document order, because the
    /// snapshot lists every string the dump carried rather than only the ones
    /// the tree tracks; a sensitive one is redacted before it is kept.
    public static func extractSnapshot(_ items: [CBORValue]) -> StateSnapshot {
        var state = DeviceState()
        var strings: [SnapshotString] = []
        for entry in items.compactMap(controlItem).flatMap(\.entries) {
            switch entry {
            case let .num(address, value):
                state.applyCbor(address: address, value: value)
            case let .text(address, text):
                state.applyCborText(address: address, text: text)
                let value = isSensitive(address) ? Generated.redactedPlaceholder : text
                strings.append(SnapshotString(address: address, text: value))
            }
        }
        return StateSnapshot(
            currentBank: state.currentBank, currentRigSlot: state.currentRigSlot,
            morph: state.morph, strings: strings)
    }

    /// Every numeric address/value pair the items carry, in document order.
    ///
    /// The dump and a session's live pushes are the same shapes, so this is what
    /// a ``CborSession`` hands out as values move.
    public static func numericValues(_ items: [CBORValue]) -> [(address: UInt32, value: Int64)] {
        var out: [(address: UInt32, value: Int64)] = []
        for entry in items.compactMap(controlItem).flatMap(\.entries) {
            if case let .num(address, value) = entry { out.append((address, value)) }
        }
        return out
    }

    /// Decode one item off the channel into the addresses and values it names.
    ///
    /// Reads the value-bearing shapes: a single `[1, addr, value]`, a
    /// consecutive-run `[2, base, v0, v1, …]` where every element is an address
    /// of its own, and a string `[4, addr, text]`. A leading negative
    /// source-flag word, if present, is skipped. Anything else — the `[5, …]`
    /// opaque blobs the dump carries, an item that is not an array, a selector
    /// this library does not know — is `nil`: not a value, so nothing to fold.
    /// An address outside the 32-bit space (negative, or past what any page or
    /// extended address can name) drops the item rather than wrapping onto some
    /// other parameter, and an empty string is no string at all.
    static func controlItem(_ item: CBORValue) -> ControlItem? {
        guard let fields = item.asArray else { return nil }
        // Skip a leading negative source-flags word.
        let rest: [CBORValue]
        if let first = fields.first?.asInt, first < 0 {
            rest = Array(fields.dropFirst())
        } else {
            rest = fields
        }
        guard let selector = rest.first?.asInt, rest.count > 1, let raw = rest[1].asInt,
            let base = UInt32(exactly: raw)
        else { return nil }

        if selector == Generated.cborSelectorSingle {
            guard rest.count > 2, let value = rest[2].asInt else { return nil }
            return ControlItem(base: base, entries: [.num(address: base, value: value)])
        } else if selector == Generated.cborSelectorMulti {
            var entries: [ControlItem.Entry] = []
            for (i, element) in rest.dropFirst(2).enumerated() {
                guard let address = UInt32(exactly: Int64(base) + Int64(i)),
                    let value = element.asInt
                else { continue }
                entries.append(.num(address: address, value: value))
            }
            return ControlItem(base: base, entries: entries)
        } else if selector == Generated.cborSelectorString {
            guard rest.count > 2, case .text(let text) = rest[2], !text.isEmpty else { return nil }
            return ControlItem(base: base, entries: [.text(address: base, text: text)])
        }
        return nil
    }
}

/// One item off the control channel, as the tree consumes it.
struct ControlItem: Sendable, Equatable {
    /// The address the item names: a single's or a string's own, or a run's
    /// base. The state dump always ends with the run based at
    /// ``Generated/dumpEndAddress``, so this is what closes the dump phase.
    let base: UInt32
    /// The values, one per address the item covers.
    let entries: [Entry]

    enum Entry: Sendable, Equatable {
        case num(address: UInt32, value: Int64)
        case text(address: UInt32, text: String)
    }
}

// MARK: - Control link

/// The CBOR socket: the one place the control channel is opened and read.
///
/// ``DeviceModel`` holds one beside its stream; ``CborSession`` and
/// ``StateSnapshot/fetch(host:port:timeout:)`` are the same link without the
/// model. It writes exactly one thing, once, on opening — the state-dump
/// trigger — and has no write method after that: the CBOR channel also carries
/// the device's own command grammar, and this library structurally cannot
/// speak it.
///
/// Exactly one task reads a link. The decoder inside is not locked; a second
/// reader would be a bug, not a race to tolerate.
final class ControlLink: @unchecked Sendable {
    /// How long a read waits before looping. Short, so a close takes effect
    /// promptly and the model's ingest reacts per packet.
    static let readIdle: TimeInterval = 0.03

    let session: Session
    /// Stream bytes that rode in on the handshake acceptance, before the dump
    /// was asked for. Feed them through ``push(_:)`` before reading more.
    let tail: [UInt8]
    private var decoder = CBORDecoder()

    private init(session: Session, tail: [UInt8]) {
        self.session = session
        self.tail = tail
    }

    /// Dial `host:port`, select the CBOR protocol, write the preamble, and ask
    /// for the state dump.
    ///
    /// The dial passes the ``ConnectionLedger``, so a link opened beside a
    /// stream is spaced from it without the caller sleeping. The CBOR protocol
    /// is required, not preferred: a greeting that does not offer it, or a
    /// rejection of it, fails the open — there is no other protocol this link
    /// could usefully speak. A failed trigger write fails the open too; a
    /// control link that never asked for the dump would leave the morph
    /// unknown for as long as it stayed up. On any failure the socket is
    /// closed before the error propagates.
    static func open(host: String, port: UInt16) async throws -> ControlLink {
        let session = try await Session.connect(host: host, port: port)
        do {
            let greeting = try await session.readAvailable(idle: readIdle, max: 256)
            let offered = Session.parseProtocolList(greeting)
            guard offered.contains(Generated.protocolCborControl) else {
                throw SessionError.protocolRejected(
                    name: Generated.protocolCborControl, detail: "not offered in the greeting")
            }
            let response = try await session.selectProtocol(
                Generated.protocolCborControl, idle: readIdle)
            try await session.writeSessionPreamble()
            let outcome = HandshakeOutcome(
                greeting: greeting, offered: offered, selected: Generated.protocolCborControl,
                response: response)
            // Writing one item asks for the whole state; the reply is the burst
            // the stream opens with.
            try await session.writeAll(Cbor.encode(Cbor.stateDumpRequest()))
            return ControlLink(session: session, tail: outcome.responseTail)
        } catch {
            session.close()
            throw error
        }
    }

    /// Decode bytes into whole items; a partial item stays buffered.
    func push(_ bytes: [UInt8]) -> [CBORValue] {
        decoder.push(bytes)
    }

    /// Read once, waiting up to `wait`, and decode what arrived. Empty when
    /// nothing did; throws once the socket has ended.
    func read(wait: TimeInterval) async throws -> [CBORValue] {
        let chunk = try await session.readOnce(wait: wait)
        return chunk.isEmpty ? [] : push(chunk)
    }

    /// Close the socket. Stamps the ledger, as every close does.
    func close() {
        session.close()
    }
}

// MARK: - Snapshot

/// One string parameter the state dump carried, by address, with any sensitive
/// value redacted.
public struct SnapshotString: Sendable, Equatable {
    public let address: UInt32
    public let text: String
    public init(address: UInt32, text: String) {
        self.address = address
        self.text = text
    }
}

/// The device's current position and the values carried alongside it, read out
/// of a state dump. Both indices are 0-based; any field is `nil` if the dump did
/// not carry it.
///
/// This is tooling: a way to read one dump without a ``DeviceModel``. A model
/// folds the same dump into its own tree when its control link opens, so an
/// app that holds a model already has everything here in ``DeviceState``.
public struct StateSnapshot: Sendable, Equatable {
    /// Current bank, 0-based (``Generated/currentBankAddress``).
    public var currentBank: UInt16?
    /// Current rig slot within the bank, 0-based
    /// (``Generated/currentRigSlotAddress``).
    public var currentRigSlot: UInt16?
    /// The morph position (0 = base, 16383 = fully morphed), at
    /// ``Generated/morphAddress``.
    ///
    /// The position is never sent on the MIDI3 stream and answers neither a
    /// `$41` nor a `$46` request, so the dump — this one, or the one the
    /// model's control link asks for — is the only way to learn it. It is a
    /// live value, so it is true as of the read and stale the moment anyone
    /// morphs.
    public var morph: UInt16?
    /// String parameters the dump carried, in document order, sensitive values
    /// redacted. Useful for the current rig name (address 1) and the bank name.
    public var strings: [SnapshotString]

    public init(
        currentBank: UInt16? = nil, currentRigSlot: UInt16? = nil, morph: UInt16? = nil,
        strings: [SnapshotString] = []
    ) {
        self.currentBank = currentBank
        self.currentRigSlot = currentRigSlot
        self.morph = morph
        self.strings = strings
    }

    /// True once every value this snapshot reads is known — the point at which
    /// the reader may stop before the dump has finished streaming.
    ///
    /// The morph counts: it arrives later in the dump than the two indices, so
    /// stopping at those would truncate the read just short of it and leave
    /// ``morph`` `nil` on a device that reported it perfectly well. Every dump
    /// observed carries all three, at base as readily as morphed.
    public var isComplete: Bool {
        currentBank != nil && currentRigSlot != nil && morph != nil
    }

    /// The string parameter at `addr`, if the dump carried one.
    public func string(_ addr: UInt32) -> String? {
        strings.first { $0.address == addr }?.text
    }

    /// Default time to keep reading the dump before giving up on the indices.
    public static let defaultTimeout: TimeInterval = 3

    /// Open a fresh control link to `host`, trigger the state dump, and read
    /// back the current bank, rig slot and morph position.
    ///
    /// This opens its **own** short-lived connection, independent of any
    /// ``DeviceModel``, and closes it on return. The dial passes the
    /// ``ConnectionLedger`` like every other, but the device is fragile under
    /// connection churn, so run this sparingly — and not at all when a model is
    /// already up, since its control link has folded the same dump. Returns as
    /// soon as every value it reads is known (see ``StateSnapshot/isComplete``)
    /// or `timeout` elapses.
    public static func fetch(
        host: String,
        port: UInt16 = Generated.port,
        timeout: TimeInterval = StateSnapshot.defaultTimeout
    ) async throws -> StateSnapshot {
        let link = try await ControlLink.open(host: host, port: port)
        defer { link.close() }

        var items = link.push(link.tail)
        let deadline = Date().addingTimeInterval(timeout)
        while !Cbor.extractSnapshot(items).isComplete {
            let remaining = deadline.timeIntervalSinceNow
            if remaining <= 0 { break }
            do {
                items.append(
                    contentsOf: try await link.read(wait: min(ControlLink.readIdle, remaining)))
            } catch SessionError.closed {
                break
            }
        }
        return Cbor.extractSnapshot(items)
    }
}

// MARK: - Live session

/// One value the device pushed on the CBOR channel.
public struct CborUpdate: Sendable, Equatable {
    /// The flat address, in the same space the NRPN pages decompose into.
    public let address: UInt32
    /// The value, as the channel encodes it (35-bit range, so wider than `$01`).
    public let value: Int64

    public init(address: UInt32, value: Int64) {
        self.address = address
        self.value = value
    }
}

/// A **live** view of the native CBOR channel: it opens, asks for the state
/// dump, and then hands out every numeric value the device pushes until it is
/// closed.
///
/// This is tooling — a raw tap on the channel for capturing or inspecting what
/// the device says, address by address. It is not how an app gets the morph:
/// ``DeviceModel`` opens the same link by default (``ControlPolicy/bestEffort``)
/// and folds its values into the one state tree, so an app that holds a model
/// should never open this beside it. Doing so costs a third session on a device
/// that objects to session churn, for values the model already has.
///
/// Read-only. The one thing it writes is the state-dump trigger, which is a flag
/// the device already carries — see [docs/06](../../../docs/06-cbor-channel.md).
public actor CborSession {
    private let link: ControlLink
    private var ingestTask: Task<Void, Never>?
    private var continuations: [UUID: AsyncStream<CborUpdate>.Continuation] = [:]
    private var closed = false
    /// Values decoded before anyone subscribed, replayed to the first
    /// subscriber. `connect` returns only after the dump has been asked for, so
    /// the opening burst can land before the caller has had a chance to call
    /// ``updates()`` — and that burst is the only place several values, the
    /// morph among them, appear until something moves them.
    private var backlog: [CborUpdate] = []

    /// How many pre-subscription values to hold. The state dump is a couple of
    /// thousand; this keeps the most recent of them rather than growing without
    /// bound if nobody ever subscribes.
    private static let backlogLimit = 4096

    private init(link: ControlLink) {
        self.link = link
    }

    /// Connect to `host:5727`, open the CBOR protocol, ask for the state dump,
    /// and start streaming.
    ///
    /// Returns once the session is established; values arrive on ``updates()``.
    /// Subscribe *before* awaiting them, or the dump's own burst is missed.
    public static func connect(
        host: String, port: UInt16 = Generated.port
    ) async throws -> CborSession {
        let link = try await ControlLink.open(host: host, port: port)
        let session = CborSession(link: link)
        await session.start()
        return session
    }

    private func start() {
        emit(link.push(link.tail))
        ingestTask = Task { [weak self] in
            await self?.ingestLoop()
        }
    }

    private func ingestLoop() async {
        while !Task.isCancelled {
            do {
                emit(try await link.read(wait: ControlLink.readIdle))
            } catch {
                finish()
                return
            }
        }
    }

    /// Every value the device pushes, in arrival order.
    public func updates() -> AsyncStream<CborUpdate> {
        let id = UUID()
        let (stream, continuation) = AsyncStream<CborUpdate>.makeStream(
            bufferingPolicy: .bufferingNewest(1024)
        )
        for update in backlog { continuation.yield(update) }
        backlog.removeAll()
        continuations[id] = continuation
        continuation.onTermination = { [weak self] _ in
            Task { await self?.drop(id) }
        }
        return stream
    }

    private func drop(_ id: UUID) { continuations[id] = nil }

    private func emit(_ items: [CBORValue]) {
        for pair in Cbor.numericValues(items) {
            let update = CborUpdate(address: pair.address, value: pair.value)
            guard !continuations.isEmpty else {
                backlog.append(update)
                if backlog.count > CborSession.backlogLimit { backlog.removeFirst() }
                continue
            }
            for (_, continuation) in continuations { continuation.yield(update) }
        }
    }

    /// Close the socket and finish the stream.
    public func close() {
        ingestTask?.cancel()
        ingestTask = nil
        link.close()
        finish()
    }

    private func finish() {
        guard !closed else { return }
        closed = true
        backlog.removeAll()
        for (_, continuation) in continuations { continuation.finish() }
        continuations.removeAll()
    }
}
