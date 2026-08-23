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

/// The reader and writer for the device's native CBOR channel, and the
/// state-dump snapshot that reads the current bank and rig slot out of it.
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

    /// Read the current bank, rig slot and string parameters out of decoded dump
    /// items.
    ///
    /// Scans the two index-bearing shapes: a single `[1, addr, value]`, and a
    /// consecutive-run `[2, base, v0, v1, …]` where the whole run is walked because
    /// the target address can fall anywhere inside it. A leading negative
    /// source-flag word, if present, is skipped.
    public static func extractSnapshot(_ items: [CBORValue]) -> StateSnapshot {
        var snap = StateSnapshot()
        walk(
            items,
            numeric: { note(&snap, address: $0, value: $1) },
            text: { address, text in
                let ua = UInt32(truncatingIfNeeded: address)
                let value = isSensitive(ua) ? Generated.redactedPlaceholder : text
                snap.strings.append(SnapshotString(address: ua, text: value))
            })
        return snap
    }

    /// Every numeric address/value pair the items carry, in document order.
    ///
    /// The dump and a session's live pushes are the same shapes, so this is what
    /// a ``CborSession`` folds into a state tree as values move.
    public static func numericValues(_ items: [CBORValue]) -> [(address: UInt32, value: Int64)] {
        var out: [(address: UInt32, value: Int64)] = []
        walk(
            items,
            numeric: { out.append((UInt32(truncatingIfNeeded: $0), $1)) },
            text: { _, _ in })
        return out
    }

    /// Walk decoded items, handing each numeric and string element to a callback.
    ///
    /// Scans the value-bearing shapes: a single `[1, addr, value]`, a
    /// consecutive-run `[2, base, v0, v1, …]` where the whole run is walked
    /// because the target address can fall anywhere inside it, and a string
    /// `[4, addr, text]`. A leading negative source-flag word, if present, is
    /// skipped.
    private static func walk(
        _ items: [CBORValue],
        numeric: (Int64, Int64) -> Void,
        text: (Int64, String) -> Void
    ) {
        for item in items {
            guard let fields = item.asArray else { continue }
            // Skip a leading negative source-flags word.
            let rest: [CBORValue]
            if let first = fields.first?.asInt, first < 0 {
                rest = Array(fields.dropFirst())
            } else {
                rest = fields
            }
            guard let selector = rest.first?.asInt else { continue }
            let addr = rest.count > 1 ? rest[1].asInt : nil

            if selector == Generated.cborSelectorSingle {
                if let a = addr, rest.count > 2, let v = rest[2].asInt { numeric(a, v) }
            } else if selector == Generated.cborSelectorMulti {
                if let base = addr {
                    for (i, element) in rest.dropFirst(2).enumerated() {
                        if let v = element.asInt { numeric(base + Int64(i), v) }
                    }
                }
            } else if selector == Generated.cborSelectorString {
                if let a = addr, rest.count > 2, case .text(let t) = rest[2], !t.isEmpty {
                    text(a, t)
                }
            }
        }
    }

    /// Record the value at one address into the snapshot's indices.
    private static func note(_ snap: inout StateSnapshot, address: Int64, value: Int64) {
        let v = UInt16(exactly: value)
        if address == Int64(Generated.currentBankAddress) {
            snap.currentBank = v
        } else if address == Int64(Generated.currentRigSlotAddress) {
            snap.currentRigSlot = v
        } else if address == Int64(Generated.morphAddress) {
            snap.morph = v
        }
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
public struct StateSnapshot: Sendable, Equatable {
    /// Current bank, 0-based (``Generated/currentBankAddress``).
    public var currentBank: UInt16?
    /// Current rig slot within the bank, 0-based
    /// (``Generated/currentRigSlotAddress``).
    public var currentRigSlot: UInt16?
    /// The morph position (0 = base, 16383 = fully morphed), at
    /// ``Generated/morphAddress``.
    ///
    /// This dump is the only way a client that is not holding a CBOR session
    /// open can learn it: the position is never sent on the MIDI3 stream, and
    /// answers neither a `$41` nor a `$46` request. It is a live value, so it is
    /// true as of the read and stale the moment anyone morphs.
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

    /// Open a fresh CBOR session to `host`, trigger the state dump, and read back
    /// the current bank, rig slot and morph position.
    ///
    /// This opens its **own** short-lived connection, independent of any
    /// ``DeviceModel``, and closes it on return. The device is fragile under
    /// connection churn, so run this sparingly. Returns as soon as every value it
    /// reads is known (see ``StateSnapshot/isComplete``) or `timeout` elapses.
    ///
    /// For a *live* view, and to run alongside a streaming session rather than
    /// before it, use ``CborSession``.
    public static func fetch(
        host: String,
        port: UInt16 = Generated.port,
        timeout: TimeInterval = StateSnapshot.defaultTimeout
    ) async throws -> StateSnapshot {
        let idle: TimeInterval = 0.03
        let session = try await Session.connect(host: host, port: port)
        defer { session.close() }

        let outcome = try await session.handshake(
            preferred: [Session.protocolCborControl], idle: idle)
        try await session.writeSessionPreamble()

        var decoder = CBORDecoder()
        var items = decoder.push(outcome.responseTail)

        // Ask for the full state, then read until both indices land or the
        // deadline passes.
        try await session.writeAll(Cbor.encode(Cbor.stateDumpRequest()))

        let deadline = Date().addingTimeInterval(timeout)
        while !Cbor.extractSnapshot(items).isComplete {
            let remaining = deadline.timeIntervalSinceNow
            if remaining <= 0 { break }
            do {
                let chunk = try await session.readOnce(wait: min(idle, remaining))
                if !chunk.isEmpty { items.append(contentsOf: decoder.push(chunk)) }
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

/// A **live** session on the native CBOR channel: it opens, asks for the state
/// dump, and then streams every value the device pushes until it is closed.
///
/// This is the counterpart to ``StateSnapshot/fetch(host:port:timeout:)``, which
/// reads one dump and hangs up. Hold this open instead when you want to *watch*
/// the values the MIDI3 stream never carries — the morph position above all,
/// which is pushed here at about 40 Hz while a morph ramps and is unreachable by
/// any request on the streaming session.
///
/// **It may run alongside a ``DeviceModel``.** The device's fragility is about
/// connection *churn*, not concurrency: two read-only sessions coexist happily,
/// and the pair is how a client gets both the meter lane and the morph. Space
/// the two connections by ``Session/connectionCooldown`` when opening them, and
/// do not reopen either in a tight loop.
///
/// Read-only. The one thing it writes is the state-dump trigger, which is a flag
/// the device already carries — see [docs/06](../../../docs/06-cbor-channel.md).
public actor CborSession {
    private let session: Session
    private var decoder = CBORDecoder()
    private var ingestTask: Task<Void, Never>?
    private var continuations: [UUID: AsyncStream<CborUpdate>.Continuation] = [:]
    private var closed = false
    /// Values decoded before anyone subscribed, replayed to the first
    /// subscriber. `connect` returns only after the dump has been asked for, so
    /// the opening burst can land before the caller has had a chance to call
    /// ``updates()`` — and that burst is the only place several values, the
    /// morph among them, appear until something moves them.
    private var backlog: [CborUpdate] = []

    /// How long a read waits before looping. Short, so `close()` takes effect
    /// promptly.
    private static let readIdle: TimeInterval = 0.25
    /// How many pre-subscription values to hold. The state dump is a couple of
    /// thousand; this keeps the most recent of them rather than growing without
    /// bound if nobody ever subscribes.
    private static let backlogLimit = 4096

    private init(session: Session) {
        self.session = session
    }

    /// Connect to `host:5727`, open the CBOR protocol, ask for the state dump,
    /// and start streaming.
    ///
    /// Returns once the session is established; values arrive on ``updates()``.
    /// Subscribe *before* awaiting them, or the dump's own burst is missed.
    public static func connect(
        host: String, port: UInt16 = Generated.port
    ) async throws -> CborSession {
        let session = try await Session.connect(host: host, port: port)
        do {
            let outcome = try await session.handshake(
                preferred: [Session.protocolCborControl], idle: 0.03)
            try await session.writeSessionPreamble()
            let model = CborSession(session: session)
            await model.start(tail: outcome.responseTail)
            return model
        } catch {
            session.close()
            throw error
        }
    }

    private func start(tail: [UInt8]) async {
        emit(decoder.push(tail))
        // Writing one item asks for the whole state; the reply is the burst the
        // stream opens with.
        try? await session.writeAll(Cbor.encode(Cbor.stateDumpRequest()))
        ingestTask = Task { [weak self] in
            await self?.ingestLoop()
        }
    }

    private func ingestLoop() async {
        while !Task.isCancelled {
            do {
                let chunk = try await session.readOnce(wait: CborSession.readIdle)
                if chunk.isEmpty { continue }
                emit(decoder.push(chunk))
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
        session.close()
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
