import Foundation

@testable import LibKP

// MARK: - The fake device

/// An in-process stand-in for a Profiler, for exercising the model's links
/// without a device.
///
/// It speaks just enough of the transport to drive ``Session``,
/// ``DeviceModel`` and the CBOR tooling: any number of concurrent connections,
/// each with the greeting, the protocol-selection ack (`+` only for a protocol
/// in ``accepts``, `-` otherwise — the reserved GUID is offered and rejected,
/// as on the device), the preamble, then the protocol's own traffic:
///
/// - **MIDI3**: framed MIDI in both directions. Received messages are unframed
///   and recorded per connection; while ``answers`` is on, every request form
///   is answered from ``values`` / ``strings`` / ``renders`` (or a placeholder)
///   unless its address is in ``silent``; ``FakeConnection/push(_:)`` sends
///   arbitrary messages.
/// - **CBOR**: decoded items are recorded per connection; the dump trigger is
///   answered with ``dumpItems`` in one write, and
///   ``FakeConnection/pushItems(_:)`` sends more at any time.
///
/// Either kind of connection, or all of them, can be hung up.
///
/// A plain BSD socket rather than `NWListener`, for the same reason
/// `DiscoveryPort` is: `NWListener` fails with `EINVAL` in some sandboxed test
/// runs. One thread accepts; one thread per connection serves it.
final class FakeDevice: @unchecked Sendable {
    /// The dump a CBOR connection serves by default: a rig name, the position
    /// as one run, the morph, and the run that always ends a real dump.
    static let defaultDump: [CBORValue] = [
        .tag(1, .array([.uint(4), .uint(UInt64(Generated.stringRigName)), .text("Dump Rig")])),
        .tag(
            1,
            .array([
                .uint(2), .uint(UInt64(Generated.currentBankAddress) - 1), .uint(0), .uint(3),
                .uint(1),
            ])),
        Cbor.paramWrite(addr: Generated.morphAddress, value: 8192),
        .tag(1, .array([.uint(2), .uint(UInt64(Generated.dumpEndAddress)), .uint(0), .uint(0)])),
    ]

    /// A `$06` Extended Parameter message.
    static func extParam(address: UInt32, value: UInt64) -> [UInt8] {
        var out: [UInt8] = [0xF0]
        out.append(contentsOf: Generated.manufacturerId)
        out.append(contentsOf: [0x00, 0x00, Generated.fnExtParam, 0x00])
        out.append(contentsOf: Nrpn.extEncode(UInt64(address), count: 5))
        out.append(contentsOf: Nrpn.extEncode(value, count: 5))
        out.append(0xF7)
        return out
    }

    /// A `$07` Extended String Parameter message.
    static func extString(address: UInt32, text: String) -> [UInt8] {
        var out: [UInt8] = [0xF0]
        out.append(contentsOf: Generated.manufacturerId)
        out.append(contentsOf: [0x00, 0x00, Generated.fnExtStringParam, 0x00])
        out.append(contentsOf: Nrpn.extEncode(UInt64(address), count: 5))
        out.append(contentsOf: Array(text.utf8))
        out.append(contentsOf: [0x00, 0xF7])
        return out
    }

    struct Failure: Error {
        let call: String
    }

    /// The identity of a rendered-string request, for ``renders``.
    struct RenderKey: Hashable {
        let page: UInt8
        let number: UInt8
        let value: UInt16
    }

    let port: UInt16
    private let fd: Int32
    private let lock = NSLock()
    private var accepted: [FakeConnection] = []

    // Configuration. Set before the first connection; read on its thread.
    private var config: Config

    struct Config {
        /// The greeting, in order.
        var offers: [String]
        /// The protocols answered with `+`.
        var accepts: Set<String>
        /// Whether MIDI3 requests are answered at all.
        var answers = true
        /// Addresses whose requests draw no reply even while `answers` is on.
        var silent: Set<UInt32> = []
        /// Numeric answers by flat address; 0 for any other.
        var values: [UInt32: UInt64] = [:]
        /// String answers by flat address; `"X"` for any other.
        var strings: [UInt32: String] = [:]
        /// Rendered-string answers; `"<0.0>"` for any other.
        var renders: [RenderKey: String] = [:]
        /// MIDI messages framed onto the acceptance line of a MIDI3 connection.
        var tailMessages: [[UInt8]] = []
        /// What a CBOR connection serves when the dump trigger arrives.
        var dumpItems: [CBORValue] = FakeDevice.defaultDump

        init(offerCbor: Bool) {
            offers = [Generated.protocolReserved, Generated.protocolMidi3Stream]
            if offerCbor { offers.append(Generated.protocolCborControl) }
            accepts = Set(offers).subtracting([Generated.protocolReserved])
        }
    }

    /// Listen on an ephemeral loopback port. `offerCbor` adds the control
    /// protocol to the greeting (and to `accepts`).
    init(offerCbor: Bool = false) throws {
        config = Config(offerCbor: offerCbor)
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { throw Failure(call: "socket") }
        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = 0  // ephemeral
        address.sin_addr.s_addr = inet_addr("127.0.0.1")
        let bound = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard bound == 0, listen(fd, 8) == 0 else {
            close(fd)
            throw Failure(call: "bind/listen")
        }
        var assigned = sockaddr_in()
        var length = socklen_t(MemoryLayout<sockaddr_in>.size)
        _ = withUnsafeMutablePointer(to: &assigned) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { getsockname(fd, $0, &length) }
        }
        self.fd = fd
        self.port = UInt16(bigEndian: assigned.sin_port)
        Thread.detachNewThread { [self] in self.acceptLoop() }
    }

    /// Change the configuration before connecting.
    func configure(_ body: (inout Config) -> Void) {
        lock.lock()
        defer { lock.unlock() }
        body(&config)
    }

    private func currentConfig() -> Config {
        lock.lock()
        defer { lock.unlock() }
        return config
    }

    /// Every connection accepted so far, oldest first. The accept thread runs
    /// a beat behind the kernel completing the handshake, so wait with
    /// ``connections(atLeast:within:)`` rather than reading straight after
    /// `connect` returns.
    var connections: [FakeConnection] {
        lock.lock()
        defer { lock.unlock() }
        return accepted
    }

    /// The connections, once at least `count` have been accepted or `within`
    /// has elapsed.
    func connections(atLeast count: Int, within: Duration = .seconds(3)) async -> [FakeConnection] {
        let deadline = ContinuousClock.Instant.now + within
        while true {
            let snapshot = connections
            if snapshot.count >= count || .now > deadline { return snapshot }
            try? await Task.sleep(for: .milliseconds(5))
        }
    }

    /// The latest connection that selected `protocolName`, once one has, or
    /// `nil` after `within`.
    func connection(
        selecting protocolName: String, within: Duration = .seconds(3)
    ) async -> FakeConnection? {
        let deadline = ContinuousClock.Instant.now + within
        while true {
            if let found = connections.last(where: { $0.selected == protocolName }) { return found }
            if .now > deadline { return nil }
            try? await Task.sleep(for: .milliseconds(5))
        }
    }

    /// Close every connection from the device side.
    func hangUpAll() {
        for connection in connections { connection.hangUp() }
    }

    /// Hang up everything and stop listening.
    func stop() {
        hangUpAll()
        // `shutdown` is what unblocks the thread parked in `accept`.
        shutdown(fd, SHUT_RDWR)
        close(fd)
    }

    private func acceptLoop() {
        while true {
            let client = accept(fd, nil, nil)
            guard client >= 0 else { return }  // the listener was shut down
            // A write to a socket the client has closed must not kill the
            // test process.
            var one: Int32 = 1
            setsockopt(client, SOL_SOCKET, SO_NOSIGPIPE, &one, socklen_t(MemoryLayout<Int32>.size))
            let connection = FakeConnection(fd: client, config: currentConfig())
            lock.lock()
            connection.index = accepted.count
            accepted.append(connection)
            lock.unlock()
            Thread.detachNewThread { connection.serve() }
        }
    }
}

// MARK: - One connection

/// One accepted socket and what happened on it.
final class FakeConnection: @unchecked Sendable {
    let acceptedAt = ContinuousClock.Instant.now
    fileprivate var index = 0
    private let fd: Int32
    private let config: FakeDevice.Config
    private let lock = NSLock()
    private let writeLock = NSLock()
    private var selectedName: String?
    private var messages: [[UInt8]] = []
    private var messageTimes: [ContinuousClock.Instant] = []
    private var items: [CBORValue] = []
    private var preambleSeen = false
    private var triggerSeen = false
    private var closed = false

    fileprivate init(fd: Int32, config: FakeDevice.Config) {
        self.fd = fd
        self.config = config
    }

    /// The protocol name the client selected, once it has.
    var selected: String? {
        lock.lock()
        defer { lock.unlock() }
        return selectedName
    }
    /// Raw MIDI messages the client sent (unframed), on a MIDI3 connection.
    var received: [[UInt8]] {
        lock.lock()
        defer { lock.unlock() }
        return messages
    }
    /// When each of ``received`` arrived.
    var receivedAt: [ContinuousClock.Instant] {
        lock.lock()
        defer { lock.unlock() }
        return messageTimes
    }
    /// Decoded CBOR items the client sent, on a CBOR connection.
    var receivedItems: [CBORValue] {
        lock.lock()
        defer { lock.unlock() }
        return items
    }
    /// Whether the client wrote the session preamble.
    var sawPreamble: Bool {
        lock.lock()
        defer { lock.unlock() }
        return preambleSeen
    }
    /// Whether the dump trigger arrived, on a CBOR connection.
    var sawDumpTrigger: Bool {
        lock.lock()
        defer { lock.unlock() }
        return triggerSeen
    }
    /// Whether the socket is closed, by either side.
    var isClosed: Bool {
        lock.lock()
        defer { lock.unlock() }
        return closed
    }

    /// ``received``, once it holds at least `count` messages or `within` has
    /// elapsed.
    func received(atLeast count: Int, within: Duration = .seconds(3)) async -> [[UInt8]] {
        let deadline = ContinuousClock.Instant.now + within
        while true {
            let snapshot = received
            if snapshot.count >= count || .now > deadline { return snapshot }
            try? await Task.sleep(for: .milliseconds(5))
        }
    }

    /// Wait until `predicate` holds of this connection, or `within` elapses.
    @discardableResult
    func wait(
        within: Duration = .seconds(3), until predicate: (FakeConnection) -> Bool
    ) async -> Bool {
        let deadline = ContinuousClock.Instant.now + within
        while !predicate(self) {
            if .now > deadline { return false }
            try? await Task.sleep(for: .milliseconds(5))
        }
        return true
    }

    /// Send one raw MIDI message, framed.
    func push(_ message: [UInt8]) {
        write(Midi3.frame(message))
    }

    /// Send CBOR items, encoded back to back in one write.
    func pushItems(_ items: [CBORValue]) {
        write(items.flatMap(Cbor.encode))
    }

    /// Close the socket from the device side.
    func hangUp() {
        lock.lock()
        let wasClosed = closed
        closed = true
        lock.unlock()
        guard !wasClosed else { return }
        // `shutdown` is what unblocks the serving thread parked in `read`.
        shutdown(fd, SHUT_RDWR)
        close(fd)
    }

    private func write(_ bytes: [UInt8]) {
        guard !isClosed, !bytes.isEmpty else { return }
        writeLock.lock()
        defer { writeLock.unlock() }
        var offset = 0
        while offset < bytes.count {
            let sent = bytes[offset...].withUnsafeBufferPointer {
                Darwin.send(fd, $0.baseAddress, $0.count, 0)
            }
            guard sent > 0 else { return }
            offset += sent
        }
    }

    private func readChunk() -> [UInt8]? {
        var buffer = [UInt8](repeating: 0, count: 4096)
        let count = buffer.withUnsafeMutableBufferPointer { read(fd, $0.baseAddress, $0.count) }
        guard count > 0 else { return nil }
        return Array(buffer[0..<count])
    }

    /// Read until the buffer holds a CRLF-terminated line; return the line
    /// and whatever followed it.
    private func readLine() -> (line: String, rest: [UInt8])? {
        var buffer: [UInt8] = []
        let terminator = Array(Generated.handshakeTerminator.utf8)
        while true {
            if let range = buffer.firstRange(of: terminator) {
                let line = String(decoding: buffer[..<range.lowerBound], as: UTF8.self)
                return (line, Array(buffer[range.upperBound...]))
            }
            guard let chunk = readChunk() else { return nil }
            buffer.append(contentsOf: chunk)
        }
    }

    fileprivate func serve() {
        defer {
            lock.lock()
            let wasClosed = closed
            closed = true
            lock.unlock()
            if !wasClosed { close(fd) }
        }

        let greeting = config.offers.map { $0 + Generated.handshakeTerminator }.joined()
        write(Array((greeting + Generated.handshakeListEnd + Generated.handshakeTerminator).utf8))

        guard let (name, leftover) = readLine() else { return }
        lock.lock()
        selectedName = name
        lock.unlock()
        guard config.accepts.contains(name) else {
            write(
                Array((Generated.handshakeRejectPrefix + "NO" + Generated.handshakeTerminator).utf8)
            )
            return
        }
        var response = Array(
            (Generated.handshakeAcceptPrefix + name + Generated.handshakeTerminator).utf8)
        if name == Generated.protocolMidi3Stream {
            for message in config.tailMessages { response.append(contentsOf: Midi3.frame(message)) }
        }
        write(response)

        // The preamble, and whatever the client sent in the same segment.
        var buffer = leftover
        while buffer.count < Generated.sessionPreambleLen {
            guard let chunk = readChunk() else { return }
            buffer.append(contentsOf: chunk)
        }
        lock.lock()
        preambleSeen = buffer[0..<Generated.sessionPreambleLen].allSatisfy { $0 == 0 }
        lock.unlock()
        buffer.removeFirst(Generated.sessionPreambleLen)

        if name == Generated.protocolCborControl {
            serveCbor(initial: buffer)
        } else {
            serveMidi3(initial: buffer)
        }
    }

    private func serveMidi3(initial: [UInt8]) {
        var unframer = Midi3.Unframer()
        var chunk = initial
        while true {
            for message in unframer.push(chunk) {
                lock.lock()
                messages.append(message)
                messageTimes.append(.now)
                lock.unlock()
                for reply in answer(message) { push(reply) }
            }
            guard let next = readChunk() else { return }
            chunk = next
        }
    }

    private func serveCbor(initial: [UInt8]) {
        var decoder = CBORDecoder()
        var chunk = initial
        while true {
            for item in decoder.push(chunk) {
                lock.lock()
                items.append(item)
                let isTrigger = item == Cbor.stateDumpRequest()
                if isTrigger { triggerSeen = true }
                lock.unlock()
                if isTrigger { pushItems(config.dumpItems) }
            }
            guard let next = readChunk() else { return }
            chunk = next
        }
    }

    /// The reply a request draws, if any: `$41` → `$01`, `$43` → `$03`, `$46`
    /// → `$06`, `$47` → `$07`, `$7C` → `$3C`. Anything else draws nothing.
    private func answer(_ message: [UInt8]) -> [[UInt8]] {
        guard config.answers, let (header, values) = NrpnHeader.parse(message) else { return [] }
        let flat = UInt32(header.page) * 128 + UInt32(header.number)
        switch header.function {
        case Generated.fnRequestSingle:
            guard !config.silent.contains(flat) else { return [] }
            let value = UInt16(clamping: config.values[flat] ?? 0)
            return [
                Nrpn.setSingle(
                    product: header.product, device: header.device, page: header.page,
                    number: header.number, value: value)
            ]
        case Generated.fnRequestString:
            guard !config.silent.contains(flat) else { return [] }
            let text = config.strings[flat] ?? "X"
            return [
                Nrpn.sysex(
                    product: header.product, device: header.device,
                    function: Generated.fnStringParam, page: header.page, number: header.number,
                    values: Array(text.utf8) + [0])
            ]
        case Generated.fnRequestExtParam, Generated.fnRequestExtString:
            guard message.count >= 13 else { return [] }
            let address = UInt32(truncatingIfNeeded: Nrpn.extDecode(Array(message[8..<13])))
            guard !config.silent.contains(address) else { return [] }
            if header.function == Generated.fnRequestExtParam {
                return [FakeDevice.extParam(address: address, value: config.values[address] ?? 0)]
            }
            return [FakeDevice.extString(address: address, text: config.strings[address] ?? "X")]
        case Generated.fnRequestRenderedString:
            guard values.count >= 2, !config.silent.contains(flat) else { return [] }
            let value = Nrpn.u14(values[0], values[1])
            let key = FakeDevice.RenderKey(page: header.page, number: header.number, value: value)
            let text = config.renders[key] ?? "<0.0>"
            return [
                Nrpn.sysex(
                    product: header.product, device: header.device,
                    function: Generated.fnRenderedStringReply, page: header.page,
                    number: header.number, values: [values[0], values[1]] + Array(text.utf8) + [0])
            ]
        default:
            return []
        }
    }
}
