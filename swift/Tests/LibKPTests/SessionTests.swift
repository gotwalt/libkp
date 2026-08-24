import Foundation
import XCTest

@testable import LibKP

// MARK: - The connection ledger, with fabricated instants

final class ConnectionLedgerTests: XCTestCase {
    private let cooldown = Duration.seconds(1)
    private let peer = ConnectionLedger.Peer(host: "10.0.0.1", port: 5727)

    func testAnUntouchedPeerIsFreeAtOnce() {
        let ledger = ConnectionLedger()
        XCTAssertEqual(ledger.delay(before: peer, cooldown: cooldown, now: .now), .zero)
    }

    func testAnOpenStartsTheCooldown() {
        let ledger = ConnectionLedger()
        let t0 = ContinuousClock.Instant.now
        ledger.noteOpen(peer, at: t0)
        XCTAssertEqual(
            ledger.delay(before: peer, cooldown: cooldown, now: t0 + .milliseconds(300)),
            .milliseconds(700))
        XCTAssertEqual(ledger.delay(before: peer, cooldown: cooldown, now: t0 + .seconds(1)), .zero)
        XCTAssertEqual(ledger.delay(before: peer, cooldown: cooldown, now: t0 + .seconds(5)), .zero)
    }

    /// The cooldown runs from whichever of the open and the close came last —
    /// a session held for a minute still forces a full quiet gap after it.
    func testACloseRestartsTheCooldown() {
        let ledger = ConnectionLedger()
        let t0 = ContinuousClock.Instant.now
        ledger.noteOpen(peer, at: t0)
        ledger.noteClose(peer, at: t0 + .seconds(60))
        let justAfterClose = t0 + .seconds(60) + .milliseconds(10)
        XCTAssertEqual(
            ledger.delay(before: peer, cooldown: cooldown, now: justAfterClose), .milliseconds(990))
    }

    /// A stamp that arrives late (a read callback racing a close) must not pull
    /// the deadline back in.
    func testAStampNeverMovesBackwards() {
        let ledger = ConnectionLedger()
        let t0 = ContinuousClock.Instant.now
        ledger.noteClose(peer, at: t0 + .seconds(2))
        ledger.noteOpen(peer, at: t0)
        XCTAssertEqual(
            ledger.delay(before: peer, cooldown: cooldown, now: t0 + .seconds(2)), .seconds(1))
    }

    func testOtherPeersAreNotDelayed() {
        let ledger = ConnectionLedger()
        let t0 = ContinuousClock.Instant.now
        ledger.noteOpen(peer, at: t0)
        let otherPort = ConnectionLedger.Peer(host: "10.0.0.1", port: 5728)
        let otherHost = ConnectionLedger.Peer(host: "10.0.0.2", port: 5727)
        XCTAssertEqual(ledger.delay(before: otherPort, cooldown: cooldown, now: t0), .zero)
        XCTAssertEqual(ledger.delay(before: otherHost, cooldown: cooldown, now: t0), .zero)
        XCTAssertEqual(ledger.delay(before: peer, cooldown: cooldown, now: t0), .seconds(1))
    }

    /// Passing the gate claims the slot: the dial about to happen is already on
    /// the books, so a second open cannot slip through beside it.
    func testWaitingYourTurnClaimsTheSlot() async throws {
        let ledger = ConnectionLedger()
        let before = ContinuousClock.Instant.now
        try await ledger.waitTurn(for: peer, cooldown: cooldown)
        XCTAssertLessThan(before.duration(to: .now), .milliseconds(100))
        XCTAssertGreaterThan(
            ledger.delay(before: peer, cooldown: cooldown, now: .now), .milliseconds(900))
    }

    /// Two opens racing for one peer come out a cooldown apart, not together.
    func testConcurrentWaitersAreSerialised() async throws {
        let ledger = ConnectionLedger()
        let gap = Duration.milliseconds(200)
        let peer = peer
        let admitted = await withTaskGroup(of: ContinuousClock.Instant.self) { group in
            for _ in 0..<3 {
                group.addTask {
                    try? await ledger.waitTurn(for: peer, cooldown: gap)
                    return .now
                }
            }
            var out = [ContinuousClock.Instant]()
            for await instant in group { out.append(instant) }
            return out.sorted()
        }
        XCTAssertEqual(admitted.count, 3)
        XCTAssertGreaterThanOrEqual(admitted[0].duration(to: admitted[1]), gap - .milliseconds(5))
        XCTAssertGreaterThanOrEqual(admitted[1].duration(to: admitted[2]), gap - .milliseconds(5))
    }
}

// MARK: - Session.connect against a loopback listener

/// A TCP listener on an ephemeral loopback port that accepts every connection,
/// records when each arrived, and never says a word — enough to prove where the
/// cooldown is enforced without a device.
///
/// A plain BSD socket rather than `NWListener`, for the same reason
/// `DiscoveryPort` is: `NWListener` fails with `EINVAL` in some sandboxed test
/// runs, and the accept loop here has nothing to do that needs the framework.
private final class LoopbackListener: @unchecked Sendable {
    let port: UInt16
    private let fd: Int32
    private let lock = NSLock()
    private var accepted: [ContinuousClock.Instant] = []
    private var clients: [Int32] = []

    init() throws {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { throw LoopbackListener.Failure(call: "socket") }
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
            throw LoopbackListener.Failure(call: "bind/listen")
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

    struct Failure: Error {
        let call: String
    }

    private func acceptLoop() {
        while true {
            let client = accept(fd, nil, nil)
            guard client >= 0 else { return }  // the listener was shut down
            lock.lock()
            accepted.append(.now)
            clients.append(client)
            lock.unlock()
        }
    }

    /// When each connection was accepted, oldest first. The accept thread runs
    /// a beat behind the kernel completing the handshake, so wait for `count`
    /// entries rather than reading straight after `connect` returns.
    func accepts(count: Int) async -> [ContinuousClock.Instant] {
        let deadline = ContinuousClock.Instant.now + .seconds(2)
        while true {
            let snapshot = acceptedSoFar()
            if snapshot.count >= count || .now > deadline { return snapshot }
            try? await Task.sleep(for: .milliseconds(5))
        }
    }

    private func acceptedSoFar() -> [ContinuousClock.Instant] {
        lock.lock()
        defer { lock.unlock() }
        return accepted
    }

    func stop() {
        lock.lock()
        let open = clients
        clients.removeAll()
        lock.unlock()
        for client in open { close(client) }
        // `shutdown` is what unblocks the thread parked in `accept`.
        shutdown(fd, SHUT_RDWR)
        close(fd)
    }
}

final class SessionCooldownTests: XCTestCase {
    private let cooldown = Duration.seconds(Session.connectionCooldown)
    private let host = "127.0.0.1"

    /// Open, close, open again: the second dial must not leave before the
    /// cooldown from the close has elapsed, and no caller slept to make it so.
    func testReopeningThePeerWaitsOutTheCooldown() async throws {
        let device = try LoopbackListener()
        defer { device.stop() }

        let first = try await Session.connect(host: host, port: device.port, timeout: 2)
        let closedAt = ContinuousClock.Instant.now
        first.close()
        let second = try await Session.connect(host: host, port: device.port, timeout: 2)
        defer { second.close() }

        let accepts = await device.accepts(count: 2)
        XCTAssertEqual(accepts.count, 2)
        XCTAssertGreaterThanOrEqual(closedAt.duration(to: accepts[1]), cooldown - .milliseconds(20))
    }

    /// A second close of one session does not push the cooldown out again:
    /// the model's teardown and a caller's own `close` can both reach it, and
    /// the cooldown runs from the first.
    func testClosingTwiceStampsTheLedgerOnce() async throws {
        let device = try FakeDevice()
        defer { device.stop() }
        let session = try await Session.connect(host: host, port: device.port, timeout: 2)
        session.close()
        try? await Task.sleep(for: .milliseconds(300))
        session.close()
        let peer = ConnectionLedger.Peer(host: host, port: device.port)
        XCTAssertLessThanOrEqual(
            ConnectionLedger.shared.delay(before: peer, cooldown: cooldown, now: .now),
            cooldown - .milliseconds(250))
    }

    /// Nothing listening on the port is a failed dial, reported as such.
    func testConnectToAClosedPortFails() async throws {
        let device = try FakeDevice()
        let port = device.port
        device.stop()
        try? await Task.sleep(for: .milliseconds(50))
        do {
            let session = try await Session.connect(host: host, port: port, timeout: 1)
            session.close()
            XCTFail("a closed port must not connect")
        } catch SessionError.connect(let address, _) {
            XCTAssertEqual(address, "\(host):\(port)")
        }
    }

    /// A peer on another port is a different peer: it opens at once even while
    /// the first is inside its cooldown.
    func testAnotherPortIsNotDelayed() async throws {
        let hot = try LoopbackListener()
        defer { hot.stop() }
        let cold = try LoopbackListener()
        defer { cold.stop() }

        let first = try await Session.connect(host: host, port: hot.port, timeout: 2)
        first.close()
        let started = ContinuousClock.Instant.now
        let second = try await Session.connect(host: host, port: cold.port, timeout: 2)
        defer { second.close() }
        XCTAssertLessThan(started.duration(to: .now), .milliseconds(500))
        let accepts = await cold.accepts(count: 1)
        XCTAssertEqual(accepts.count, 1)
    }

    /// The one-shot snapshot fetch opens its own socket; it passes the ledger
    /// because it goes through `Session.connect`, and this pins that. The fetch
    /// itself fails — the listener never greets — which is beside the point.
    func testStateSnapshotFetchPassesTheLedger() async throws {
        let device = try LoopbackListener()
        defer { device.stop() }

        let warm = try await Session.connect(host: host, port: device.port, timeout: 2)
        let closedAt = ContinuousClock.Instant.now
        warm.close()
        _ = try? await StateSnapshot.fetch(host: host, port: device.port, timeout: 0.5)

        let accepts = await device.accepts(count: 2)
        XCTAssertEqual(accepts.count, 2)
        XCTAssertGreaterThanOrEqual(closedAt.duration(to: accepts[1]), cooldown - .milliseconds(20))
    }

    /// `StateSnapshot.fetch` reads the dump over its own control link: the
    /// position, the morph and the rig name, on one socket, the trigger
    /// written once, nothing left open.
    func testStateSnapshotFetchReadsTheDump() async throws {
        let device = try FakeDevice(offerCbor: true)
        defer { device.stop() }
        let snapshot = try await StateSnapshot.fetch(host: host, port: device.port, timeout: 2)
        XCTAssertTrue(snapshot.isComplete)
        XCTAssertEqual(snapshot.currentBank, 3)
        XCTAssertEqual(snapshot.currentRigSlot, 1)
        XCTAssertEqual(snapshot.morph, 8192)
        XCTAssertEqual(snapshot.string(UInt32(Generated.stringRigName)), "Dump Rig")
        let connections = await device.connections(atLeast: 1)
        XCTAssertEqual(connections.count, 1)
        XCTAssertEqual(connections[0].selected, Generated.protocolCborControl)
        XCTAssertEqual(connections[0].receivedItems, [Cbor.stateDumpRequest()])
        let closed = await connections[0].wait { $0.isClosed }
        XCTAssertTrue(closed, "the fetch closes its socket")
    }

    /// `CborSession` streams the dump's numeric pairs in document order —
    /// held for the first subscriber, who cannot exist before the link is
    /// open — then the values pushed after it.
    func testCborSessionStreamsTheDumpAndLaterPushes() async throws {
        let device = try FakeDevice(offerCbor: true)
        defer { device.stop() }
        let session = try await CborSession.connect(host: host, port: device.port)
        let updates = Recorder<CborUpdate>()
        let stream = await session.updates()
        Task {
            for await update in stream { updates.append(update) }
            updates.finish()
        }
        let expected = Cbor.numericValues(FakeDevice.defaultDump).map {
            CborUpdate(address: $0.address, value: $0.value)
        }
        let dump = await updates.wait { $0.count >= expected.count }
        XCTAssertEqual(dump, expected)
        guard let control = await device.connection(selecting: Generated.protocolCborControl) else {
            return XCTFail("no control connection")
        }
        control.pushItems([Cbor.paramWrite(addr: Generated.morphAddress, value: 0)])
        let all = await updates.wait { $0.count > expected.count }
        XCTAssertEqual(all.last, CborUpdate(address: Generated.morphAddress, value: 0))
        await session.close()
        await updates.wait { _ in updates.finished }
        XCTAssertTrue(updates.finished)
    }

    /// Likewise the live CBOR session.
    func testCborSessionConnectPassesTheLedger() async throws {
        let device = try LoopbackListener()
        defer { device.stop() }

        let warm = try await Session.connect(host: host, port: device.port, timeout: 2)
        let closedAt = ContinuousClock.Instant.now
        warm.close()
        _ = try? await CborSession.connect(host: host, port: device.port)

        let accepts = await device.accepts(count: 2)
        XCTAssertEqual(accepts.count, 2)
        XCTAssertGreaterThanOrEqual(closedAt.duration(to: accepts[1]), cooldown - .milliseconds(20))
    }
}

// MARK: - The handshake's wait for a slow device

/// The greeting and the selection reply are each given ``Session/handshakeTimeout``
/// to begin, not just the inter-segment `idle` gap; these pin that against a
/// fake that dawdles and one that never speaks.
final class SessionHandshakeTests: XCTestCase {
    private let host = "127.0.0.1"
    /// The inter-segment gap the model uses on the stream, well under the
    /// delays below.
    private let idle: TimeInterval = 0.03

    /// A device that takes ten idle gaps to greet — and as long again to
    /// acknowledge the selection — still completes the handshake, because each
    /// line's first byte is waited for on the handshake budget.
    func testASlowGreetingStillHandshakes() async throws {
        let device = try FakeDevice()
        defer { device.stop() }
        device.configure { $0.handshakeDelay = 0.3 }

        let session = try await Session.connect(host: host, port: device.port, timeout: 2)
        defer { session.close() }
        let started = ContinuousClock.Instant.now
        let outcome = try await session.handshake(
            preferred: [Session.protocolMidi3Stream], idle: idle)
        let elapsed = started.duration(to: .now)

        XCTAssertEqual(outcome.selected, Session.protocolMidi3Stream)
        XCTAssertEqual(
            outcome.offered, [Generated.protocolReserved, Session.protocolMidi3Stream])
        XCTAssertEqual(
            outcome.response.first, Array(Generated.handshakeAcceptPrefix.utf8).first)
        XCTAssertEqual(
            String(decoding: outcome.response, as: UTF8.self),
            Generated.handshakeAcceptPrefix + Session.protocolMidi3Stream
                + Generated.handshakeTerminator)
        // Two delayed lines, and the wait was for them rather than a timeout.
        XCTAssertGreaterThanOrEqual(elapsed, .milliseconds(600) - .milliseconds(20))
        XCTAssertLessThan(elapsed, .seconds(Session.handshakeTimeout))
    }

    /// A device that never greets fails with the greeting timeout, after the
    /// budget it was given and reporting that budget — shortened here so the
    /// test does not sit out the real two seconds.
    func testADeviceThatNeverGreetsTimesOut() async throws {
        let device = try FakeDevice()
        defer { device.stop() }
        device.configure { $0.greets = false }

        let session = try await Session.connect(host: host, port: device.port, timeout: 2)
        defer { session.close() }
        let started = ContinuousClock.Instant.now
        do {
            _ = try await session.handshake(
                preferred: [Session.protocolMidi3Stream], idle: idle, greetingTimeout: 0.2)
            XCTFail("a silent device must not complete the handshake")
        } catch SessionError.timeout(let phase, let ms) {
            XCTAssertEqual(phase, "greeting")
            XCTAssertEqual(ms, 200)
        }
        let elapsed = started.duration(to: .now)
        XCTAssertGreaterThanOrEqual(elapsed, .milliseconds(200) - .milliseconds(20))
        XCTAssertLessThan(elapsed, .seconds(1))
    }

    /// A device that greets, reads the selection, and then never answers it
    /// has not opened a session: the handshake fails for the selection phase
    /// after its budget, rather than handing back an empty verdict.
    func testASelectionReplyThatNeverBeginsTimesOut() async throws {
        let device = try FakeDevice()
        defer { device.stop() }
        device.configure { $0.acks = false }

        let session = try await Session.connect(host: host, port: device.port, timeout: 2)
        defer { session.close() }
        let started = ContinuousClock.Instant.now
        do {
            _ = try await session.handshake(
                preferred: [Session.protocolMidi3Stream], idle: idle, greetingTimeout: 0.2)
            XCTFail("an absent verdict must not complete the handshake")
        } catch SessionError.timeout(let phase, let ms) {
            XCTAssertEqual(phase, "protocol selection")
            XCTAssertEqual(ms, 200)
        }
        let elapsed = started.duration(to: .now)
        XCTAssertGreaterThanOrEqual(elapsed, .milliseconds(200) - .milliseconds(20))
        XCTAssertLessThan(elapsed, .seconds(1))
        let connection = await device.connections(atLeast: 1)[0]
        let selected = await connection.wait { $0.selected != nil }
        XCTAssertTrue(selected, "the selection was written before the wait")
    }

    /// A greeting that offers nothing to choose is the greeting failure, the
    /// same as no greeting at all: reported for the `"greeting"` phase with
    /// the budget it was given.
    func testAnEmptyGreetingIsAGreetingTimeout() async throws {
        let device = try FakeDevice()
        defer { device.stop() }
        device.configure { $0.offers = [] }

        let session = try await Session.connect(host: host, port: device.port, timeout: 2)
        defer { session.close() }
        do {
            _ = try await session.handshake(
                preferred: [Session.protocolMidi3Stream], idle: idle, greetingTimeout: 0.2)
            XCTFail("an empty greeting must not complete the handshake")
        } catch SessionError.timeout(let phase, let ms) {
            XCTAssertEqual(phase, "greeting")
            XCTAssertEqual(ms, 200)
        }
    }

    /// With none of the preferred protocols offered, the handshake takes the
    /// device's first — the stream is better than nothing.
    func testHandshakeFallsBackToTheFirstOfferedProtocol() async throws {
        let device = try FakeDevice()
        defer { device.stop() }
        device.configure { $0.offers = [Session.protocolMidi3Stream] }

        let session = try await Session.connect(host: host, port: device.port, timeout: 2)
        defer { session.close() }
        let outcome = try await session.handshake(preferred: ["not-offered"], idle: idle)
        XCTAssertEqual(outcome.selected, Session.protocolMidi3Stream)
        XCTAssertEqual(outcome.offered, [Session.protocolMidi3Stream])
    }

    /// A rejected selection names the protocol and carries the device's own
    /// line.
    func testARejectionCarriesTheDeviceDetail() async throws {
        let device = try FakeDevice()
        defer { device.stop() }

        let session = try await Session.connect(host: host, port: device.port, timeout: 2)
        defer { session.close() }
        do {
            _ = try await session.handshake(preferred: [Generated.protocolReserved], idle: idle)
            XCTFail("the reserved GUID is refused")
        } catch SessionError.protocolRejected(let name, let detail) {
            XCTAssertEqual(name, Generated.protocolReserved)
            XCTAssertEqual(detail, Generated.handshakeRejectPrefix + "NO")
        }
    }

    /// The default budget is the generated constant, so the library and the
    /// spec agree on how patient a connect is.
    func testTheDefaultBudgetIsTheSpecConstant() {
        XCTAssertEqual(Session.handshakeTimeout, 2.0)
        XCTAssertEqual(UInt64(Session.handshakeTimeout * 1000), Generated.handshakeTimeoutMs)
    }
}
