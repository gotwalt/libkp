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
///
/// Neither of the device's two lines is prompt. The greeting has been measured
/// arriving 777 ms after the socket opened on a device that had served a few
/// sessions, and the acceptance can lag the same way, so each is awaited with
/// two separate budgets: up to ``handshakeTimeout`` for its **first** byte, and
/// then the caller's `idle` gap — the 30 ms or so that separates one segment
/// from the next — to gather the rest. A single `idle`-sized wait would fail a
/// healthy but slow device with a spurious ``SessionError/timeout(phase:ms:)``.
public final class Session: @unchecked Sendable {
    /// The protocol identifier that streams live MIDI data (meters, params,
    /// tuner) — the only offered protocol observed to push data unprompted.
    public static let protocolMidi3Stream = Generated.protocolMidi3Stream
    /// Request/response protocol identifier: accepts the handshake but pushes
    /// nothing.
    public static let protocolRequestResponse = Generated.protocolRequestResponse
    /// The device's native CBOR control channel — the state-dump snapshot route
    /// (see ``Cbor`` and `docs/06`). Completes the same handshake and preamble as
    /// the MIDI3 stream, then speaks CBOR rather than MIDI3 frames.
    public static let protocolCborControl = Generated.protocolCborControl

    /// The 8 zero bytes the client writes to open the encapsulated stream.
    public static let sessionPreamble = [UInt8](repeating: 0, count: Generated.sessionPreambleLen)

    /// Minimum quiet gap between one open or close and the next open to the same
    /// peer. The device refuses to greet — or resets — a session opened too soon
    /// after a prior socket closed, and connection churn can wedge it until a
    /// power cycle (see `docs/06` and `docs/11`). The process-wide
    /// ``ConnectionLedger`` enforces this inside ``connect(host:port:timeout:)``
    /// itself, so every path that opens a socket — ``DeviceModel``,
    /// ``CborSession``, ``StateSnapshot/fetch(host:port:timeout:)`` — is spaced
    /// from the last open or close to that `host:port` without the caller
    /// sleeping. Opens to a different peer are never delayed. Callers still
    /// should not open and close in a loop: the ledger makes churn slow, not
    /// harmless.
    public static let connectionCooldown = TimeInterval(Generated.connectionCooldownMs) / 1000.0

    /// How long ``handshake(preferred:idle:greetingTimeout:)`` waits for the
    /// first byte of the greeting, and
    /// ``selectProtocol(_:idle:replyTimeout:)`` for the first byte of the
    /// device's answer, before giving the connection up as unresponsive. This
    /// is the budget for the device to *start* speaking; once it has, the
    /// shorter `idle` gap decides when the line is complete.
    public static let handshakeTimeout = TimeInterval(Generated.handshakeTimeoutMs) / 1000.0

    private let connection: NWConnection
    private let queue = DispatchQueue(label: "com.libkp.session")
    private let inbox = Inbox()
    /// Set by the first ``close()``; a second is a no-op, so the ledger is
    /// stamped once.
    private let closeGate = ResumeOnce()
    /// The ledger key this session opened under, stamped again on close.
    private let ledgerPeer: ConnectionLedger.Peer

    /// The address this session is connected to, for diagnostics.
    public let peer: String

    private init(connection: NWConnection, ledgerPeer: ConnectionLedger.Peer) {
        self.connection = connection
        self.ledgerPeer = ledgerPeer
        self.peer = ledgerPeer.description
    }

    // MARK: - Connect

    /// Connect to `host:port` (default 5727) with an explicit connect timeout.
    ///
    /// Waits out ``connectionCooldown`` from the last open or close to the same
    /// `host:port` before dialling (see ``ConnectionLedger``); the `timeout`
    /// covers only the dial itself. A dial the peer refuses — nothing
    /// listening on the port, no route to the host — fails at once with
    /// ``SessionError/connect(address:detail:)`` rather than waiting out the
    /// timeout for a path that will not change, as Rust and Python fail; the
    /// timeout is for a dial that simply gets no answer. Cancelling the task
    /// while it waits throws `CancellationError` without touching the socket.
    public static func connect(
        host: String,
        port: UInt16 = Generated.port,
        timeout: TimeInterval = TimeInterval(Generated.connectTimeoutSecs)
    ) async throws -> Session {
        guard let nwPort = NWEndpoint.Port(rawValue: port) else {
            throw SessionError.connect(address: "\(host):\(port)", detail: "invalid port")
        }
        let ledgerPeer = ConnectionLedger.Peer(host: host, port: port)
        try await ConnectionLedger.shared.waitTurn(
            for: ledgerPeer, cooldown: .seconds(connectionCooldown))
        let parameters = NWParameters.tcp
        // The device is latency-sensitive for live control; disable Nagle.
        if let tcp = parameters.defaultProtocolStack.transportProtocol as? NWProtocolTCP.Options {
            tcp.noDelay = true
            tcp.connectionTimeout = Int(timeout)
        }
        let connection = NWConnection(host: NWEndpoint.Host(host), port: nwPort, using: parameters)
        let session = Session(connection: connection, ledgerPeer: ledgerPeer)
        try await session.start(timeout: timeout)
        ConnectionLedger.shared.noteOpen(ledgerPeer)
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
                case let .failed(error), let .waiting(error):
                    // `Network` parks a refused dial in `waiting` for a path
                    // change; for a device on the LAN there is none coming,
                    // and the refusal is the answer.
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
                ConnectionLedger.shared.noteClose(self.ledgerPeer)
                self.inbox.fail(SessionError.io(phase: "read", detail: error.localizedDescription))
                return
            }
            if isComplete {
                // The device hung up. That is a close on the wire as much as
                // ours is, so it starts the cooldown too.
                ConnectionLedger.shared.noteClose(self.ledgerPeer)
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
    /// bytes, or EOF). Used to drain the live stream; the handshake lines, which
    /// may take far longer than one gap to begin, go through
    /// ``readAvailable(first:idle:max:)``. Returns the bytes collected, possibly
    /// empty.
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

    /// Read one reply from the device: wait up to `first` for it to begin, then
    /// collect until an `idle` gap with no data (or `max` bytes, or EOF).
    ///
    /// This is ``readAvailable(idle:max:)`` with a separate, longer budget for
    /// the opening byte, for the handshake lines that a device may take far
    /// longer than one inter-segment gap to produce. Returns empty only when
    /// nothing at all arrived inside `first`; the caller names the phase that
    /// timed out. EOF before the first byte is a closed connection; EOF after
    /// it hands back what arrived, as ``readAvailable(idle:max:)`` does.
    public func readAvailable(
        first: TimeInterval, idle: TimeInterval, max: Int
    ) async throws -> [UInt8] {
        var buffer = try await readOnce(wait: first)
        guard !buffer.isEmpty else { return [] }
        while buffer.count < max {
            do {
                guard let chunk = try await inbox.next(timeout: idle) else { break }
                buffer.append(contentsOf: chunk)
            } catch {
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
    ///
    /// Stamps the ``ConnectionLedger`` synchronously, so a `connect` to the same
    /// peer issued right after this call — even from the same task, with no
    /// suspension in between — waits out ``connectionCooldown``. Idempotent:
    /// a second close does nothing, and in particular does not stamp the
    /// ledger again — the model's teardown and a caller's own `close` can
    /// both reach a session, and the cooldown runs from the first.
    public func close() {
        closeGate.finish {
            ConnectionLedger.shared.noteClose(ledgerPeer)
            connection.cancel()
            inbox.fail(SessionError.closed)
        }
    }

    // MARK: - Handshake

    /// Send `name` + `"\r\n"` and read the device's response line.
    ///
    /// Waits up to `replyTimeout` (``handshakeTimeout`` by default) for the
    /// response to begin — a device can be as slow to answer the selection as
    /// it is to greet — then `idle` for the rest of it. Throws
    /// ``SessionError/timeout(phase:ms:)`` for phase `"protocol selection"` if
    /// nothing at all arrives in `replyTimeout`, and
    /// ``SessionError/protocolRejected(name:detail:)`` if the response begins
    /// with `-`; anything else is handed back for the caller to judge.
    public func selectProtocol(
        _ name: String, idle: TimeInterval, replyTimeout: TimeInterval = Session.handshakeTimeout
    ) async throws -> [UInt8] {
        var message = Array(name.utf8)
        message.append(contentsOf: Array(Generated.handshakeTerminator.utf8))
        try await writeAll(message)
        let response = try await readAvailable(first: replyTimeout, idle: idle, max: 256)
        guard !response.isEmpty else {
            throw SessionError.timeout(
                phase: "protocol selection", ms: UInt64((replyTimeout * 1000).rounded()))
        }
        if response.first == Array(Generated.handshakeRejectPrefix.utf8).first {
            let detail = String(decoding: response, as: UTF8.self)
                .trimmingCharacters(in: .whitespacesAndNewlines)
            throw SessionError.protocolRejected(name: name, detail: detail)
        }
        return response  // '+' accept, or unknown — hand back for inspection.
    }

    /// Full handshake: read the greeting, pick the first `preferred` protocol
    /// the device offers (falling back to its first offered), and select it.
    ///
    /// The greeting is given `greetingTimeout` to begin and `idle` to finish,
    /// and the selection's reply the same two budgets; a device that says
    /// nothing at all in `greetingTimeout` fails with
    /// ``SessionError/timeout(phase:ms:)`` for phase `"greeting"`, reporting
    /// the wait actually spent. One that greets without offering anything
    /// usable — an empty list — is the same failure: no greeting worth the
    /// name arrived. The default is ``handshakeTimeout``; tests shorten it.
    public func handshake(
        preferred: [String], idle: TimeInterval,
        greetingTimeout: TimeInterval = Session.handshakeTimeout
    ) async throws -> HandshakeOutcome {
        let greeting = try await readAvailable(first: greetingTimeout, idle: idle, max: 256)
        guard !greeting.isEmpty else {
            throw SessionError.timeout(
                phase: "greeting", ms: UInt64((greetingTimeout * 1000).rounded()))
        }
        let offered = Session.parseProtocolList(greeting)
        guard let selected = preferred.first(where: { offered.contains($0) }) ?? offered.first
        else {
            throw SessionError.timeout(
                phase: "greeting", ms: UInt64((greetingTimeout * 1000).rounded()))
        }
        let response = try await selectProtocol(
            selected, idle: idle, replyTimeout: greetingTimeout)
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

/// The process-wide record of when each peer was last dialled or hung up, and
/// the gate every ``Session/connect(host:port:timeout:)`` passes through.
///
/// The device tolerates concurrent sessions but not connection *churn*: a
/// session opened inside about a second of the previous open or close to it is
/// refused or reset, and enough of that wedges the device until a power cycle
/// (`docs/11`). Rather than ask each caller to remember to sleep — the model,
/// ``CborSession`` and ``StateSnapshot/fetch(host:port:timeout:)`` each open
/// their own socket and can be composed in any order — the spacing is enforced
/// once, here, keyed by `host:port` so that fakes on other ports and other
/// devices are never held up.
///
/// Three moments are recorded per peer and only the latest matters:
/// - a dial being **admitted** by ``waitTurn(for:cooldown:)`` — so two opens
///   racing for the same peer are serialised a cooldown apart rather than both
///   passing an empty ledger, and a dial that fails still counts as a poke;
/// - a dial **succeeding** (``noteOpen(_:at:)``), the moment the device saw a
///   new session;
/// - a **close**, ours or the device's (``noteClose(_:at:)``).
///
/// This is a lock-guarded class rather than an actor for one reason:
/// ``Session/close()`` is synchronous and its stamp must be visible to a
/// `connect` issued immediately afterwards, with no executor hop for the two to
/// reorder across. Time is ``ContinuousClock`` so a suspended machine does not
/// look like a long-elapsed cooldown.
final class ConnectionLedger: @unchecked Sendable {
    /// The ledger every session in the process shares.
    static let shared = ConnectionLedger()

    /// A `host:port` pair as the caller spelled it. Two spellings of one device
    /// (an address and a hostname, say) are two peers; the ledger does not
    /// resolve names.
    struct Peer: Hashable, Sendable, CustomStringConvertible {
        let host: String
        let port: UInt16

        var description: String { "\(host):\(port)" }
    }

    private let lock = NSLock()
    private var lastTouch: [Peer: ContinuousClock.Instant] = [:]

    init() {}

    /// How long an open to `peer` at `now` must still wait, or zero if the
    /// cooldown has already elapsed (or the peer has never been touched).
    func delay(before peer: Peer, cooldown: Duration, now: ContinuousClock.Instant) -> Duration {
        lock.lock()
        defer { lock.unlock() }
        return ConnectionLedger.remaining(since: lastTouch[peer], cooldown: cooldown, now: now)
    }

    /// Wait until an open to `peer` is allowed, then claim it: on return the
    /// ledger already shows this dial, so a second waiter for the same peer
    /// sleeps another `cooldown`. Throws `CancellationError` if the task is
    /// cancelled while waiting.
    func waitTurn(for peer: Peer, cooldown: Duration) async throws {
        let clock = ContinuousClock()
        while true {
            let now = clock.now
            let wait = admit(peer, cooldown: cooldown, now: now)
            if wait == .zero { return }
            // Sleep, then look again: another open may have been admitted in
            // the meantime and pushed the deadline out.
            try await Task.sleep(until: now + wait, clock: clock)
        }
    }

    /// Claim a dial to `peer` at `now` if the cooldown has elapsed, returning
    /// zero; otherwise leave the ledger alone and return the wait still owed.
    /// Checking and claiming under one lock is what keeps two waiters from both
    /// seeing the same empty slot.
    func admit(_ peer: Peer, cooldown: Duration, now: ContinuousClock.Instant) -> Duration {
        lock.lock()
        defer { lock.unlock() }
        let wait = ConnectionLedger.remaining(since: lastTouch[peer], cooldown: cooldown, now: now)
        if wait == .zero { lastTouch[peer] = now }
        return wait
    }

    /// Record that a dial to `peer` succeeded at `now`.
    func noteOpen(_ peer: Peer, at now: ContinuousClock.Instant = .now) {
        touch(peer, at: now)
    }

    /// Record that a session with `peer` closed at `now`, whichever side hung up.
    func noteClose(_ peer: Peer, at now: ContinuousClock.Instant = .now) {
        touch(peer, at: now)
    }

    /// Move the peer's stamp forward to `now`; a stamp never moves back, so an
    /// out-of-order note cannot shorten a cooldown already in force.
    private func touch(_ peer: Peer, at now: ContinuousClock.Instant) {
        lock.lock()
        if let previous = lastTouch[peer], previous > now {
            lock.unlock()
            return
        }
        lastTouch[peer] = now
        lock.unlock()
    }

    private static func remaining(
        since touch: ContinuousClock.Instant?, cooldown: Duration, now: ContinuousClock.Instant
    ) -> Duration {
        guard let touch else { return .zero }
        let elapsed = touch.duration(to: now)
        return elapsed >= cooldown ? .zero : cooldown - elapsed
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
