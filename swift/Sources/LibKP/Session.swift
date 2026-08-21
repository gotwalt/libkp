import Foundation
import Network

/// Result of the protocol-selection handshake.
public struct HandshakeOutcome: Sendable {
    /// Raw greeting bytes the device sent on connect.
    public let greeting: [UInt8]
    /// Protocol names parsed from the greeting.
    public let offered: [String]
    /// The protocol name we selected and sent.
    public let selected: String
    /// Raw device response to our selection (first byte `+`/`-`).
    public let response: [UInt8]

    /// Stream bytes that arrived piggybacked after the `+<name>\r\n` ack line.
    ///
    /// The device often sends the first burst of session data in the same
    /// packet as the acceptance; feed this to the unframer before reading more.
    public var responseTail: [UInt8] {
        let terminator = Array(Generated.handshakeTerminator.utf8)
        guard response.count >= terminator.count else { return [] }
        for i in 0...(response.count - terminator.count)
        where Array(response[i..<(i + terminator.count)]) == terminator {
            return Array(response[(i + terminator.count)...])
        }
        return []
    }
}

/// An open TCP session with a Profiler.
///
/// The handshake sequence, established by observed experimentation:
/// 1. TCP connect to the device on port 5727.
/// 2. The **device sends first**: a list of supported protocol identifiers, one
///    per CRLF-terminated line, ending with a line containing just `"."`.
/// 3. The client writes the chosen protocol name followed by `"\r\n"`.
/// 4. The device replies with a line beginning `+` (accept) or `-` (reject).
/// 5. For the streaming session the client then writes an 8-byte zero preamble
///    and the encapsulated stream begins.
public final class Session: @unchecked Sendable {
    /// The protocol identifier that streams live MIDI data (meters, params,
    /// tuner) — the only offered protocol observed to push data unprompted.
    public static let protocolMidi3Stream = Generated.protocolMidi3Stream
    /// Request/response protocol identifier: accepts the handshake but pushes
    /// nothing.
    public static let protocolRequestResponse = Generated.protocolRequestResponse

    /// The 8 zero bytes the client writes to open the encapsulated stream.
    public static let sessionPreamble = [UInt8](repeating: 0, count: Generated.sessionPreambleLen)

    private let connection: NWConnection
    private let queue = DispatchQueue(label: "com.libkp.session")
    private let inbox = Inbox()

    /// The address this session is connected to, for diagnostics.
    public let peer: String

    private init(connection: NWConnection, peer: String) {
        self.connection = connection
        self.peer = peer
    }

    // MARK: - Connect

    /// Connect to `host:port` (default 5727) with an explicit connect timeout.
    public static func connect(
        host: String,
        port: UInt16 = Generated.port,
        timeout: TimeInterval = TimeInterval(Generated.connectTimeoutSecs)
    ) async throws -> Session {
        guard let nwPort = NWEndpoint.Port(rawValue: port) else {
            throw SessionError.connect(address: "\(host):\(port)", detail: "invalid port")
        }
        let parameters = NWParameters.tcp
        // The device is latency-sensitive for live control; disable Nagle.
        if let tcp = parameters.defaultProtocolStack.transportProtocol as? NWProtocolTCP.Options {
            tcp.noDelay = true
            tcp.connectionTimeout = Int(timeout)
        }
        let connection = NWConnection(host: NWEndpoint.Host(host), port: nwPort, using: parameters)
        let session = Session(connection: connection, peer: "\(host):\(port)")
        try await session.start(timeout: timeout)
        session.pump()
        return session
    }

    /// Bring the connection to `.ready`, or throw.
    private func start(timeout: TimeInterval) async throws {
        let gate = ResumeOnce()
        try await withCheckedThrowingContinuation {
            (continuation: CheckedContinuation<Void, Error>) in
            connection.stateUpdateHandler = { [peer] state in
                switch state {
                case .ready:
                    gate.finish { continuation.resume() }
                case let .failed(error):
                    gate.finish {
                        continuation.resume(
                            throwing: SessionError.connect(
                                address: peer, detail: error.localizedDescription
                            ))
                    }
                case .cancelled:
                    gate.finish { continuation.resume(throwing: SessionError.closed) }
                default:
                    break
                }
            }
            connection.start(queue: queue)
            queue.asyncAfter(deadline: .now() + timeout) { [peer] in
                gate.finish {
                    continuation.resume(
                        throwing: SessionError.timeout(
                            phase: "connect", ms: UInt64(timeout * 1000)
                        ))
                }
                _ = peer
            }
        }
        // From here on, state changes only matter as stream termination.
        connection.stateUpdateHandler = { [inbox] state in
            switch state {
            case .failed, .cancelled:
                inbox.fail(SessionError.closed)
            default:
                break
            }
        }
    }

    /// Continuously drain the socket into the inbox.
    private func pump() {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 64 * 1024) {
            [weak self] data, _, isComplete, error in
            guard let self else { return }
            if let data, !data.isEmpty { self.inbox.push([UInt8](data)) }
            if let error {
                self.inbox.fail(SessionError.io(phase: "read", detail: error.localizedDescription))
                return
            }
            if isComplete {
                self.inbox.fail(SessionError.closed)
                return
            }
            self.pump()
        }
    }

    // MARK: - Read / write

    /// Do a **single** read with a timeout, returning the bytes read.
    ///
    /// Returns as soon as any data arrives, or an empty array once `wait`
    /// elapses. Use it to drive a render loop that must react to every packet
    /// even while the device is streaming continuously.
    public func readOnce(wait: TimeInterval) async throws -> [UInt8] {
        try await inbox.next(timeout: wait) ?? []
    }

    /// Read whatever the device sends until an `idle` gap with no data (or `max`
    /// bytes, or EOF). Used both to capture the greeting and to drain the live
    /// stream. Returns the bytes collected, possibly empty.
    public func readAvailable(idle: TimeInterval, max: Int) async throws -> [UInt8] {
        var buffer = [UInt8]()
        while true {
            do {
                guard let chunk = try await inbox.next(timeout: idle) else { break }
                buffer.append(contentsOf: chunk)
                if buffer.count >= max { break }
            } catch {
                // EOF: report the closure unless data was already collected.
                if buffer.isEmpty { throw error }
                break
            }
        }
        return buffer
    }

    /// Write all `data` to the device.
    public func writeAll(_ data: [UInt8]) async throws {
        guard !data.isEmpty else { return }
        try await withCheckedThrowingContinuation {
            (continuation: CheckedContinuation<Void, Error>) in
            connection.send(
                content: Data(data),
                completion: .contentProcessed { error in
                    if let error {
                        continuation.resume(
                            throwing: SessionError.io(
                                phase: "write", detail: error.localizedDescription
                            ))
                    } else {
                        continuation.resume()
                    }
                })
        }
    }

    /// Write the 8-byte zero preamble that opens the encapsulated stream.
    public func writeSessionPreamble() async throws {
        try await writeAll(Session.sessionPreamble)
    }

    /// Close the connection.
    public func close() {
        connection.cancel()
        inbox.fail(SessionError.closed)
    }

    // MARK: - Handshake

    /// Send `name` + `"\r\n"` and read the device's response line.
    ///
    /// Throws ``SessionError/protocolRejected(name:detail:)`` if the response
    /// begins with `-`.
    public func selectProtocol(_ name: String, idle: TimeInterval) async throws -> [UInt8] {
        var message = Array(name.utf8)
        message.append(contentsOf: Array(Generated.handshakeTerminator.utf8))
        try await writeAll(message)
        let response = try await readAvailable(idle: idle, max: 256)
        if response.first == Array(Generated.handshakeRejectPrefix.utf8).first {
            let detail = String(decoding: response, as: UTF8.self)
                .trimmingCharacters(in: .whitespacesAndNewlines)
            throw SessionError.protocolRejected(name: name, detail: detail)
        }
        return response  // '+' accept, or unknown — hand back for inspection.
    }

    /// Full handshake: read the greeting, pick the first `preferred` protocol
    /// the device offers (falling back to its first offered), and select it.
    public func handshake(preferred: [String], idle: TimeInterval) async throws -> HandshakeOutcome
    {
        let greeting = try await readAvailable(idle: idle, max: 256)
        let offered = Session.parseProtocolList(greeting)
        guard let selected = preferred.first(where: { offered.contains($0) }) ?? offered.first
        else {
            throw SessionError.noProtocolOffered
        }
        let response = try await selectProtocol(selected, idle: idle)
        return HandshakeOutcome(
            greeting: greeting, offered: offered, selected: selected, response: response
        )
    }

    /// Parse the greeting's offered protocol list: one identifier per
    /// CRLF-terminated line, ending with a line containing just `"."`.
    public static func parseProtocolList(_ bytes: [UInt8]) -> [String] {
        let text = String(decoding: bytes, as: UTF8.self)
        var out = [String]()
        for line in text.split(whereSeparator: \.isNewline) {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.isEmpty { continue }
            if trimmed == Generated.handshakeListEnd { break }
            out.append(trimmed)
        }
        return out
    }
}

/// A one-shot latch so a continuation is resumed exactly once even though
/// several `Network` callbacks may race to finish it.
final class ResumeOnce: @unchecked Sendable {
    private let lock = NSLock()
    private var done = false

    func finish(_ body: () -> Void) {
        lock.lock()
        if done {
            lock.unlock()
            return
        }
        done = true
        lock.unlock()
        body()
    }
}
