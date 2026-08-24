import Foundation
import XCTest

@testable import LibKP

// MARK: - Helpers

/// Everything one of the model's streams yielded, with a wait-until.
final class Recorder<Element: Sendable>: @unchecked Sendable {
    private let lock = NSLock()
    private var items: [Element] = []
    private var done = false

    func append(_ item: Element) {
        lock.lock()
        items.append(item)
        lock.unlock()
    }

    func finish() {
        lock.lock()
        done = true
        lock.unlock()
    }

    var all: [Element] {
        lock.lock()
        defer { lock.unlock() }
        return items
    }

    /// Whether the stream behind this recorder has finished.
    var finished: Bool {
        lock.lock()
        defer { lock.unlock() }
        return done
    }

    /// The items once `predicate` holds of them, or once `within` has elapsed.
    @discardableResult
    func wait(
        within: Duration = .seconds(4), until predicate: ([Element]) -> Bool
    ) async -> [Element] {
        let deadline = ContinuousClock.Instant.now + within
        while true {
            let snapshot = all
            if predicate(snapshot) || .now > deadline { return snapshot }
            try? await Task.sleep(for: .milliseconds(5))
        }
    }
}

/// Subscribe to both of a model's streams now, so nothing emitted after this
/// returns is missed. The snapshot stream yields the current state first.
func attach(
    _ model: DeviceModel
) async -> (events: Recorder<DeviceEvent>, snapshots: Recorder<DeviceState>) {
    let events = Recorder<DeviceEvent>()
    let snapshots = Recorder<DeviceState>()
    let eventStream = await model.events()
    let snapshotStream = await model.snapshots()
    Task {
        for await event in eventStream { events.append(event) }
        events.finish()
    }
    Task {
        for await snapshot in snapshotStream { snapshots.append(snapshot) }
        snapshots.finish()
    }
    return (events, snapshots)
}

/// The control link's states, in order, out of a snapshot list: consecutive
/// repeats collapsed and the initial `closed` dropped. The snapshot stream
/// yields the current state first, so this is deterministic even when the
/// first transition beat the subscription — the control task starts the
/// moment `connect` returns, and a subscriber cannot exist before that.
func controlStates(_ snapshots: [DeviceState]) -> [ChannelState] {
    var out: [ChannelState] = []
    for state in snapshots.map(\.channels.control) where out.last != state {
        out.append(state)
    }
    return out.first == .closed ? Array(out.dropFirst()) : out
}

private let host = "127.0.0.1"
private let gainAddress = UInt32(Generated.ampPage) * 128 + UInt32(Generated.gainNumber)

// MARK: - The control link

final class ControlLinkTests: XCTestCase {
    /// The default connect: the control link opens after the stream, its dump
    /// folds into the one tree, the run at the end address closes the dump,
    /// and the whole burst costs one snapshot.
    func testConnectOpensTheControlLinkAndFoldsTheDump() async throws {
        let device = try FakeDevice(offerCbor: true)
        defer { device.stop() }
        let model = try await DeviceModel.connect(host: host, port: device.port)
        let (events, snapshots) = await attach(model)

        let seen = await events.wait { $0.contains(.syncCompleted(source: .control)) }
        XCTAssertEqual(controlStates(snapshots.all), [.connecting, .open])
        XCTAssertTrue(seen.contains(.channelChanged(channel: .control, state: .open)))
        // The dump's values are in the tree, and were folded before the sync
        // was declared complete.
        let state = await model.snapshot()
        XCTAssertEqual(state.morph, 8192)
        XCTAssertEqual(state.rig.name, "Dump Rig")
        XCTAssertEqual(state.currentBank, 3)
        XCTAssertEqual(state.currentRigSlot, 1)
        XCTAssertEqual(state.connection, .connected)
        XCTAssertEqual(state.channels, Channels(stream: .open, control: .open))
        let morphAt = seen.firstIndex(of: .morphChanged(8192))
        let syncAt = seen.firstIndex(of: .syncCompleted(source: .control))
        XCTAssertNotNil(morphAt)
        XCTAssertLessThan(morphAt ?? .max, syncAt ?? 0)

        try? await Task.sleep(for: .milliseconds(100))
        XCTAssertEqual(
            snapshots.all.filter { $0.morph != nil }.count, 1,
            "the dump is one chunk, so one snapshot")
        let control = await device.connection(selecting: Generated.protocolCborControl)
        XCTAssertEqual(control?.sawPreamble, true)
        XCTAssertEqual(control?.sawDumpTrigger, true)
        XCTAssertEqual(device.connections.count, 2)
        // The control link is one socket the model never writes to again.
        XCTAssertEqual(control?.receivedItems, [Cbor.stateDumpRequest()])
        await model.close()
    }

    /// A device that refuses the CBOR protocol leaves the model degraded, with
    /// the stream intact.
    func testARejectedControlLinkIsUnavailableAndTheModelDegraded() async throws {
        let device = try FakeDevice(offerCbor: true)
        defer { device.stop() }
        device.configure {
            $0.accepts = [Generated.protocolMidi3Stream]
            $0.values[gainAddress] = 1234
        }
        let model = try await DeviceModel.connect(
            host: host, port: device.port, options: ConnectOptions(sync: .off))
        let (events, snapshots) = await attach(model)

        let seen = await events.wait { $0.contains(.connectionChanged(.degraded)) }
        XCTAssertEqual(controlStates(snapshots.all), [.connecting, .unavailable])
        XCTAssertTrue(seen.contains(.channelChanged(channel: .control, state: .unavailable)))
        let state = await model.snapshot()
        XCTAssertEqual(state.connection, .degraded)
        XCTAssertEqual(state.channels, Channels(stream: .open, control: .unavailable))
        // The stream keeps working.
        let gain = try await model.requestParam(
            page: Generated.ampPage, number: Generated.gainNumber)
        XCTAssertEqual(gain, 1234)
        XCTAssertFalse(seen.contains(.disconnected))
        await model.close()
    }

    /// A greeting without the CBOR protocol is the same failure, without a
    /// selection ever being sent.
    func testAControlProtocolNotOfferedIsUnavailable() async throws {
        let device = try FakeDevice(offerCbor: false)
        defer { device.stop() }
        let model = try await DeviceModel.connect(
            host: host, port: device.port, options: ConnectOptions(sync: .off))
        let (events, snapshots) = await attach(model)

        await events.wait { $0.contains(.connectionChanged(.degraded)) }
        XCTAssertEqual(controlStates(snapshots.all), [.connecting, .unavailable])
        let control = await device.connections(atLeast: 2).last
        XCTAssertNil(control?.selected)
        await model.close()
    }

    /// `off` means one socket, ever.
    func testControlOffNeverOpensASecondConnection() async throws {
        let device = try FakeDevice(offerCbor: true)
        defer { device.stop() }
        let model = try await DeviceModel.connect(
            host: host, port: device.port, options: ConnectOptions(control: .off, sync: .off))
        let (events, snapshots) = await attach(model)

        try? await Task.sleep(for: .milliseconds(1500))
        XCTAssertEqual(device.connections.count, 1)
        let state = await model.snapshot()
        XCTAssertEqual(state.connection, .connected)
        XCTAssertEqual(state.channels, Channels(stream: .open, control: .closed))
        XCTAssertEqual(controlStates(snapshots.all), [])
        XCTAssertFalse(
            events.all.contains {
                if case .channelChanged(.control, _) = $0 { true } else { false }
            })
        do {
            try await model.reopenControl()
            XCTFail("reopenControl must be refused with the policy off")
        } catch let error as ChannelError {
            guard case .off = error else { return XCTFail("expected .off, got \(error)") }
        }
        await model.close()
    }

    /// `required` fails the connect, and closes the stream it had opened.
    func testRequiredControlFailsConnectAndLeavesNothingOpen() async throws {
        let device = try FakeDevice(offerCbor: true)
        defer { device.stop() }
        device.configure { $0.accepts = [Generated.protocolMidi3Stream] }
        do {
            _ = try await DeviceModel.connect(
                host: host, port: device.port,
                options: ConnectOptions(control: .required, sync: .off))
            XCTFail("connect must fail when a required control link is refused")
        } catch let error as SessionError {
            guard case .protocolRejected = error else {
                return XCTFail("expected protocolRejected, got \(error)")
            }
        }
        let connections = await device.connections(atLeast: 2)
        XCTAssertEqual(connections.count, 2)
        for connection in connections {
            let closed = await connection.wait { $0.isClosed }
            XCTAssertTrue(closed, "connection \(connection.selected ?? "?") is still open")
        }
    }

    /// The control socket ending on its own is `lost`, never reopened unless
    /// asked, and an ask inside the gap is refused.
    func testControlEofIsLostAndNotReopened() async throws {
        let device = try FakeDevice(offerCbor: true)
        defer { device.stop() }
        let model = try await DeviceModel.connect(
            host: host, port: device.port, options: ConnectOptions(sync: .off))
        let (events, snapshots) = await attach(model)

        await events.wait { $0.contains(.channelChanged(channel: .control, state: .open)) }
        guard let control = await device.connection(selecting: Generated.protocolCborControl) else {
            return XCTFail("no control connection")
        }
        control.hangUp()

        let seen = await events.wait {
            $0.contains(.channelChanged(channel: .control, state: .lost))
        }
        XCTAssertEqual(controlStates(snapshots.all), [.connecting, .open, .lost])
        XCTAssertTrue(seen.contains(.connectionChanged(.degraded)))
        let state = await model.snapshot()
        XCTAssertEqual(state.channels, Channels(stream: .open, control: .lost))
        XCTAssertEqual(state.morph, 8192, "the last known morph stays")

        try? await Task.sleep(for: .milliseconds(1500))
        XCTAssertEqual(device.connections.count, 2, "the control link must not reopen on its own")
        do {
            try await model.reopenControl()
            XCTFail("a reopen inside the gap must be refused")
        } catch let error as ChannelError {
            guard case .tooSoon = error else { return XCTFail("expected .tooSoon, got \(error)") }
        }
        XCTAssertEqual(device.connections.count, 2)
        await model.close()
    }
}

// MARK: - The request lane

final class RequestLaneTests: XCTestCase {
    private func connectWithoutControl(
        _ device: FakeDevice, sync: SyncStrategy = .off
    ) async throws -> DeviceModel {
        try await DeviceModel.connect(
            host: host, port: device.port, options: ConnectOptions(control: .off, sync: sync))
    }

    func testRequestsResolveWithTheirValuesAndFoldThem() async throws {
        let device = try FakeDevice()
        defer { device.stop() }
        let slotName = Params.bankPreviewAddress(.rigName, slot: 1)
        device.configure {
            $0.values[gainAddress] = 1234
            $0.strings[UInt32(Generated.stringRigName)] = "Maz 18"
            $0.values[Generated.currentBankAddress] = 7
            $0.strings[slotName] = "Slot One"
            $0.renders[
                FakeDevice.RenderKey(
                    page: Generated.ampPage, number: Generated.gainNumber, value: 1234)] =
                "5.2"
        }
        let model = try await connectWithoutControl(device)

        let gain = try await model.requestParam(
            page: Generated.ampPage, number: Generated.gainNumber)
        XCTAssertEqual(gain, 1234)
        let name = try await model.requestString(number: Generated.stringRigName)
        XCTAssertEqual(name, "Maz 18")
        let bank = try await model.requestExtParam(address: Generated.currentBankAddress)
        XCTAssertEqual(bank, 7)
        let slot = try await model.requestExtString(address: slotName)
        XCTAssertEqual(slot, "Slot One")
        let rendered = try await model.requestRender(
            page: Generated.ampPage, number: Generated.gainNumber, value: 1234)
        XCTAssertEqual(rendered, "5.2")

        let state = await model.snapshot()
        XCTAssertEqual(state.amp.gain, 1234)
        XCTAssertEqual(state.rig.name, "Maz 18")
        XCTAssertEqual(state.currentBank, 7)
        XCTAssertEqual(state.bank.slots[0].rigName, "Slot One")
        await model.close()
    }

    func testAnUnansweredRequestTimesOutAndIsNeverResent() async throws {
        let device = try FakeDevice()
        defer { device.stop() }
        device.configure { $0.silent = [gainAddress] }
        let model = try await connectWithoutControl(device)
        let (events, _) = await attach(model)

        let started = ContinuousClock.Instant.now
        do {
            _ = try await model.requestParam(page: Generated.ampPage, number: Generated.gainNumber)
            XCTFail("a silent address must time out")
        } catch let error as RequestError {
            XCTAssertEqual(error, .timeout)
        }
        let elapsed = started.duration(to: .now)
        let timeout = Duration.milliseconds(Generated.requestTimeoutMs)
        XCTAssertGreaterThanOrEqual(elapsed, timeout - .milliseconds(20))
        XCTAssertLessThan(elapsed, timeout * 3)
        await events.wait { $0.contains(.requestTimedOut(address: gainAddress)) }
        XCTAssertTrue(events.all.contains(.requestTimedOut(address: gainAddress)))

        try? await Task.sleep(for: .milliseconds(200))
        let stream = await device.connections(atLeast: 1)[0]
        XCTAssertEqual(stream.received.count, 1, "a timed-out request is not retried")
        await model.close()
    }

    func testTheMorphIsUnreadableWithoutAByteOnTheWire() async throws {
        let device = try FakeDevice()
        defer { device.stop() }
        let model = try await connectWithoutControl(device)
        do {
            _ = try await model.requestParam(
                page: Generated.pageMorph, number: Generated.morphNumber)
            XCTFail("the morph must be refused")
        } catch let error as RequestError {
            XCTAssertEqual(error, .unreadable)
        }
        try? await Task.sleep(for: .milliseconds(100))
        let stream = await device.connections(atLeast: 1)[0]
        XCTAssertEqual(stream.received, [])
        await model.close()
    }

    /// The lane never puts more than the cap on the wire; the rest wait for a
    /// slot, which here frees only when a request times out.
    func testAtMostTheCapIsOnTheWireAtOnce() async throws {
        let device = try FakeDevice()
        defer { device.stop() }
        device.configure { $0.answers = false }
        let model = try await connectWithoutControl(device)
        let stream = await device.connections(atLeast: 1)[0]

        let total = 40
        let burst = Task {
            await withTaskGroup(of: Void.self) { group in
                for number in 0..<total {
                    group.addTask {
                        _ = try? await model.requestParam(
                            page: Generated.ampPage, number: UInt8(number))
                    }
                }
            }
        }
        try? await Task.sleep(for: .milliseconds(150))
        XCTAssertEqual(stream.received.count, Generated.maxInFlightRequests)
        await burst.value
        let received = await stream.received(atLeast: total)
        XCTAssertEqual(received.count, total, "queued requests are not dropped")
        let times = stream.receivedAt
        let timeout = Duration.milliseconds(Generated.requestTimeoutMs)
        XCTAssertGreaterThanOrEqual(
            times[0].duration(to: times[Generated.maxInFlightRequests]),
            timeout - .milliseconds(30),
            "the 17th request waits for a slot, which the first timeout frees")
        await model.close()
    }

    /// `refresh()` is every `request = true` row, and the connect-time burst
    /// is `refresh()`.
    func testRefreshIssuesTheFortySixRequests() async throws {
        let device = try FakeDevice()
        defer { device.stop() }
        let model = try await connectWithoutControl(device)
        let stream = await device.connections(atLeast: 1)[0]
        try await model.refresh()
        let received = stream.received
        XCTAssertEqual(received.count, 46)
        let functions = Dictionary(grouping: received) { $0[6] }.mapValues(\.count)
        XCTAssertEqual(functions[Generated.fnRequestSingle], 23)
        XCTAssertEqual(functions[Generated.fnRequestString], 6)
        XCTAssertEqual(functions[Generated.fnRequestExtString], 15)
        XCTAssertEqual(functions[Generated.fnRequestExtParam], 2)
        await model.close()

        let second = try FakeDevice()
        defer { second.stop() }
        let synced = try await connectWithoutControl(second, sync: .streamBurst)
        let burst = await second.connections(atLeast: 1)[0]
        let seen = await burst.received(atLeast: 46)
        try? await Task.sleep(for: .milliseconds(100))
        XCTAssertEqual(seen.count, 46)
        XCTAssertEqual(burst.received.count, 46)
        let state = await synced.snapshot()
        XCTAssertEqual(state.rig.name, "X", "the burst's replies fold into the tree")
        XCTAssertEqual(state.amp.gain, 0)
        await synced.close()
    }
}

// MARK: - Lives

final class SupervisorTests: XCTestCase {
    private func options(reconnect: ReconnectPolicy = ReconnectPolicy()) -> ConnectOptions {
        ConnectOptions(control: .off, sync: .off, reconnect: reconnect)
    }

    /// Today's contract: the device hanging up is `disconnected`, and the
    /// streams stay open for a client that wants to keep watching.
    func testStreamHangUpIsDisconnectedByDefault() async throws {
        let device = try FakeDevice()
        defer { device.stop() }
        let model = try await DeviceModel.connect(host: host, port: device.port, options: options())
        let (events, snapshots) = await attach(model)

        device.hangUpAll()
        let seen = await events.wait { $0.contains(.disconnected) }
        XCTAssertTrue(seen.contains(.connectionChanged(.disconnected)))
        XCTAssertTrue(seen.contains(.channelChanged(channel: .stream, state: .lost)))
        let state = await model.snapshot()
        XCTAssertEqual(state.connection, .disconnected)
        XCTAssertEqual(state.channels, Channels(stream: .lost, control: .closed))
        do {
            try await model.tapTempo()
            XCTFail("a command on a closed stream must be refused")
        } catch CommandError.disconnected {
        }

        try? await Task.sleep(for: .milliseconds(100))
        XCTAssertFalse(events.finished, "only close() finishes the streams")
        XCTAssertFalse(snapshots.finished)
        await model.close()
        // The two streams finish on their own tasks; wait for each.
        await events.wait { _ in events.finished }
        await snapshots.wait { _ in snapshots.finished }
        XCTAssertTrue(events.finished)
        XCTAssertTrue(snapshots.finished)
    }

    /// With a backoff, the same handle dials again — spaced by the ledger, on
    /// the same streams, without a `disconnected` in between.
    func testStreamHangUpReconnectsOnTheSameStreams() async throws {
        let device = try FakeDevice()
        defer { device.stop() }
        let backoff = Backoff(initial: .milliseconds(50), max: .seconds(1))
        let model = try await DeviceModel.connect(
            host: host, port: device.port,
            options: options(reconnect: ReconnectPolicy(stream: backoff)))
        let (events, snapshots) = await attach(model)

        let first = await device.connections(atLeast: 1)[0]
        let hungUpAt = ContinuousClock.Instant.now
        first.hangUp()
        let seen = await events.wait { $0.contains(.connected) }
        XCTAssertTrue(seen.contains(.connectionChanged(.reconnecting(attempt: 1))))
        XCTAssertTrue(seen.contains(.connectionChanged(.connected)))
        XCTAssertFalse(seen.contains(.disconnected))
        XCTAssertLessThan(
            seen.firstIndex(of: .connectionChanged(.reconnecting(attempt: 1))) ?? .max,
            seen.firstIndex(of: .connected) ?? 0)

        let connections = await device.connections(atLeast: 2)
        XCTAssertEqual(connections.count, 2)
        XCTAssertGreaterThanOrEqual(
            hungUpAt.duration(to: connections[1].acceptedAt),
            .seconds(Session.connectionCooldown) - .milliseconds(30),
            "the second life is spaced by the ledger, not the backoff")
        let state = await model.snapshot()
        XCTAssertEqual(state.connection, .connected)
        XCTAssertEqual(state.channels.stream, .open)
        XCTAssertTrue(snapshots.all.contains { $0.connection == .reconnecting(attempt: 1) })
        XCTAssertFalse(events.finished)
        // The new life answers commands.
        try await model.tapTempo()
        await model.close()
    }

    func testCloseTwiceIsHarmless() async throws {
        let device = try FakeDevice()
        defer { device.stop() }
        let model = try await DeviceModel.connect(host: host, port: device.port, options: options())
        let (events, snapshots) = await attach(model)
        await model.close()
        await model.close()
        await events.wait { _ in events.finished }
        XCTAssertEqual(events.all.filter { $0 == .disconnected }.count, 1)
        XCTAssertEqual(events.all.filter { $0 == .connectionChanged(.disconnected) }.count, 1)
        XCTAssertTrue(snapshots.finished)
        let state = await model.snapshot()
        XCTAssertEqual(state.connection, .disconnected)
        XCTAssertEqual(state.channels, Channels(stream: .closed, control: .closed))
        let stream = await device.connections(atLeast: 1)[0]
        let closed = await stream.wait { $0.isClosed }
        XCTAssertTrue(closed)
    }
}

// MARK: - The Navigator

final class NavigatorTests: XCTestCase {
    private func connect(_ device: FakeDevice) async throws -> DeviceModel {
        try await DeviceModel.connect(
            host: host, port: device.port, options: ConnectOptions(control: .off, sync: .off))
    }

    /// The two messages one load puts on the wire for a flat index: the bank
    /// preselect, then the slot load's press and release.
    private func loadPair(_ index: UInt16) -> [[UInt8]] {
        let slots = UInt16(Params.bankSlots)
        return [
            Control.bankPreselect(UInt8(index / slots)).message(),
            Control.loadSlot(UInt8(index % slots) + 1).message(),
        ]
    }

    /// The `$06` pair a device pushes when it lands on a flat index.
    private func report(_ index: UInt16, on stream: FakeConnection) {
        let slots = UInt16(Params.bankSlots)
        stream.push(
            FakeDevice.extParam(address: Generated.currentBankAddress, value: UInt64(index / slots))
        )
        stream.push(
            FakeDevice.extParam(
                address: Generated.currentRigSlotAddress, value: UInt64(index % slots)))
    }

    /// A burst of taps is exactly two loads — the first tap and the final aim
    /// — spaced by at least the settle time, and the position report for the
    /// final aim retires it.
    func testABurstCostsTwoLoadsSpacedByTheSettle() async throws {
        let device = try FakeDevice()
        defer { device.stop() }
        let model = try await connect(device)
        let (events, snapshots) = await attach(model)
        let stream = await device.connections(atLeast: 1)[0]

        await model.navigateTo(14)
        await model.navigateTo(15)
        await model.navigateTo(16)
        var state = await model.snapshot()
        XCTAssertEqual(state.navigation, Navigation(aim: 16, inFlight: true))
        XCTAssertEqual(state.aimedRigIndex, 16)

        let received = await stream.received(atLeast: 4)
        XCTAssertEqual(received, loadPair(14) + loadPair(16))
        let times = stream.receivedAt
        XCTAssertGreaterThanOrEqual(
            times[0].duration(to: times[2]),
            .milliseconds(Generated.rigLoadSettleMs) - .milliseconds(20),
            "the second load waits for the first to settle")
        XCTAssertLessThan(times[0].duration(to: times[1]), .milliseconds(100))

        // The device lands on the final aim: settled, and nothing more sent.
        report(16, on: stream)
        let seen = await events.wait { $0.contains(.navigationSettled(index: 16)) }
        XCTAssertTrue(seen.contains(.navigationSettled(index: 16)))
        XCTAssertFalse(seen.contains { if case .navigationDropped = $0 { true } else { false } })
        state = await model.snapshot()
        XCTAssertNil(state.navigation.aim)
        XCTAssertEqual(state.currentRigIndex, 16)
        XCTAssertEqual(state.aimedRigIndex, 16)
        try? await Task.sleep(for: .milliseconds(Generated.rigLoadSettleMs + 200))
        XCTAssertEqual(stream.received.count, 4, "a burst is two loads, however long")
        let aims = snapshots.all.map(\.navigation.aim)
        XCTAssertTrue(aims.contains(16))
        XCTAssertEqual(aims.last ?? 0, nil, "the aim is gone from the snapshot")
        await model.close()
    }

    /// The direct routes are refused before a byte is written.
    func testRigLoadsOutsideTheNavigatorAreRefused() async throws {
        let device = try FakeDevice()
        defer { device.stop() }
        let model = try await connect(device)
        let stream = await device.connections(atLeast: 1)[0]

        for control in [
            Control.loadSlot(1), .up, .down, .programChange(3), .bankSelect(msb: 0, lsb: 1),
        ] {
            do {
                try await model.send(control: control)
                XCTFail("\(control) must be refused")
            } catch let error as CommandError {
                XCTAssertEqual(error, .rigLoadRequiresNavigator, "\(control)")
            }
        }
        for raw in [[0xC0, 5], Control.loadSlot(2).message(), Control.up.message()] {
            do {
                try await model.sendRaw(raw)
                XCTFail("\(raw) must be refused")
            } catch let error as CommandError {
                XCTAssertEqual(error, .rigLoadRequiresNavigator)
            }
        }
        try? await Task.sleep(for: .milliseconds(100))
        XCTAssertEqual(stream.received, [], "nothing reached the wire")

        // The preselect alone loads nothing, and passes.
        try await model.bank(3)
        try await model.sendRaw(Control.bankPreselect(1).message())
        let received = await stream.received(atLeast: 2)
        XCTAssertEqual(
            received, [Control.bankPreselect(2).message(), Control.bankPreselect(1).message()])
        await model.close()
    }

    /// An aim the device never confirms — past the last rig — is dropped after
    /// the window, and the same index may be tried again afterwards.
    func testAnUnconfirmedAimIsDroppedAfterTheWindow() async throws {
        let device = try FakeDevice()
        defer { device.stop() }
        let model = try await connect(device)
        let (events, _) = await attach(model)
        let stream = await device.connections(atLeast: 1)[0]

        let started = ContinuousClock.Instant.now
        await model.navigateTo(999)
        // The device stays put and says so; that is not the aim.
        report(13, on: stream)
        let seen = await events.wait(within: .seconds(4)) {
            $0.contains(.navigationDropped(index: 999, reason: .unconfirmed))
        }
        let elapsed = started.duration(to: .now)
        XCTAssertTrue(seen.contains(.navigationDropped(index: 999, reason: .unconfirmed)))
        XCTAssertGreaterThanOrEqual(
            elapsed,
            .milliseconds(Generated.rigLoadSettleMs + Generated.pendingWindowMs) - .milliseconds(30)
        )
        let state = await model.snapshot()
        XCTAssertEqual(state.navigation, Navigation())
        XCTAssertEqual(state.aimedRigIndex, 13, "the device's position is the truth again")
        XCTAssertEqual(stream.received, loadPair(999), "an index already sent is never re-sent")

        await model.navigateTo(999)
        let received = await stream.received(atLeast: 4)
        XCTAssertEqual(received, loadPair(999) + loadPair(999), "a drop forgets the sent index")
        await model.close()
    }

    /// The steppers compute in the flat index from the aim, and do nothing
    /// before a position is known.
    func testSteppersAimFromTheAimAndNeedAPosition() async throws {
        let device = try FakeDevice()
        defer { device.stop() }
        let model = try await connect(device)
        let (events, _) = await attach(model)
        let stream = await device.connections(atLeast: 1)[0]

        await model.stepRig(by: 1)
        await model.stepBank(forward: true)
        await model.selectSlot(3)
        try? await Task.sleep(for: .milliseconds(100))
        XCTAssertEqual(stream.received, [], "nothing to step from yet")
        let idle = await model.snapshot()
        XCTAssertEqual(idle.navigation, Navigation())

        report(16, on: stream)  // bank 3, slot 2
        await events.wait { $0.contains(.currentPosition(bank: 3, slot: 1)) }
        await model.stepBank(forward: true)  // 21, sent at once
        await model.stepRig(by: -1)  // 20, from the aim, not the position
        await model.selectSlot(5)  // slot 5 of the aimed bank 4: 24
        let state = await model.snapshot()
        XCTAssertEqual(state.navigation, Navigation(aim: 24, inFlight: true))
        let received = await stream.received(atLeast: 4)
        XCTAssertEqual(received, loadPair(21) + loadPair(24))
        // Stepping below zero floors at zero.
        report(24, on: stream)
        await events.wait { $0.contains(.navigationSettled(index: 24)) }
        await model.stepRig(by: -100)
        let floored = await model.snapshot()
        XCTAssertEqual(floored.navigation.aim, 0)
        await model.close()
    }

    /// Losing the stream forgets the aim without a drop; a load with no
    /// stream is dropped at once.
    func testAStreamLossClearsTheAimAndALoadWithNoStreamIsDropped() async throws {
        let device = try FakeDevice()
        defer { device.stop() }
        let model = try await connect(device)
        let (events, _) = await attach(model)

        await model.navigateTo(7)
        let aimed = await model.snapshot()
        XCTAssertEqual(aimed.navigation, Navigation(aim: 7, inFlight: true))
        device.hangUpAll()
        await events.wait { $0.contains(.disconnected) }
        let state = await model.snapshot()
        XCTAssertEqual(state.navigation, Navigation())
        XCTAssertFalse(
            events.all.contains { if case .navigationDropped = $0 { true } else { false } })

        await model.navigateTo(8)
        let seen = await events.wait {
            $0.contains(.navigationDropped(index: 8, reason: .unconfirmed))
        }
        XCTAssertTrue(seen.contains(.navigationDropped(index: 8, reason: .unconfirmed)))
        let after = await model.snapshot()
        XCTAssertEqual(after.navigation, Navigation())
        await model.close()
    }
}
