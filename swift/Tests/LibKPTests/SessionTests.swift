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
