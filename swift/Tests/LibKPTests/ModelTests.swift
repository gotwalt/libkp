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
private let tempoAddress = UInt32(Generated.pageRigSettings) * 128 + UInt32(Generated.tempoNumber)
private let rigNameAddress = UInt32(Generated.pageStrings) * 128 + UInt32(Generated.stringRigName)

/// A `$01` Single Parameter Change as the device pushes one (product 0,
/// device 0).
private func pushed(page: UInt8, number: UInt8, value: UInt16) -> [UInt8] {
    Nrpn.setSingle(product: 0, device: 0, page: page, number: number, value: value)
}

/// A `$01` as the model writes one (product 0, device omni).
private func written(page: UInt8, number: UInt8, value: UInt16) -> [UInt8] {
    Nrpn.setSingle(
        product: DeviceModel.product, device: DeviceModel.device, page: page, number: number,
        value: value)
}

/// The tempo at 120 BPM, as a live push on the stream.
private let tempo120 = pushed(
    page: Generated.pageRigSettings, number: Generated.tempoNumber,
    value: 120 * Generated.tempoBpmScale)

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

    /// The fake's default dump is a real dump's two-section shape: two runs
    /// based at the end address, and the second is what ends the dump. One
    /// run alone ends nothing.
    func testTheDumpHasTwoEndRunsTheSecondOfWhichEndsIt() async throws {
        XCTAssertEqual(
            FakeDevice.defaultDump.filter { $0 == FakeDevice.dumpEndRun }.count,
            Generated.dumpEndRuns)
        XCTAssertEqual(FakeDevice.defaultDump.last, FakeDevice.dumpEndRun)

        let device = try FakeDevice(offerCbor: true)
        defer { device.stop() }
        device.configure { $0.dumpItems = [] }
        let model = try await DeviceModel.connect(
            host: host, port: device.port, options: ConnectOptions(sync: .off))
        let (events, _) = await attach(model)
        await events.wait { $0.contains(.channelChanged(channel: .control, state: .open)) }
        guard let control = await device.connection(selecting: Generated.protocolCborControl) else {
            return XCTFail("no control connection")
        }
        // The first run closes the system section; the dump is still open.
        control.pushItems([
            Cbor.paramWrite(addr: Generated.morphAddress, value: 100), FakeDevice.dumpEndRun,
        ])
        let seen = await events.wait(within: .milliseconds(300)) {
            $0.contains(.syncCompleted(source: .control))
        }
        XCTAssertTrue(seen.contains(.morphChanged(100)), "the item before the run folded")
        XCTAssertFalse(seen.contains(.syncCompleted(source: .control)), "one run ends nothing")
        // The second closes the rig section, and the dump with it.
        control.pushItems([FakeDevice.dumpEndRun])
        let done = await events.wait { $0.contains(.syncCompleted(source: .control)) }
        XCTAssertTrue(done.contains(.syncCompleted(source: .control)))
        await model.close()
    }

    /// A value pushed live on the stream while the dump streams outranks the
    /// dump's stale copy of that address: the dump is a copy taken when it
    /// was asked for, and the push is newer.
    func testALivePushDuringTheDumpOutranksTheDump() async throws {
        // The fake serves nothing on the trigger, so the dump phase stays open
        // and the test drives it.
        let device = try FakeDevice(offerCbor: true)
        defer { device.stop() }
        device.configure { $0.dumpItems = [] }
        let model = try await DeviceModel.connect(
            host: host, port: device.port, options: ConnectOptions(sync: .off))
        let (events, _) = await attach(model)
        await events.wait { $0.contains(.channelChanged(channel: .control, state: .open)) }
        guard let stream = await device.connection(selecting: Generated.protocolMidi3Stream),
            let control = await device.connection(selecting: Generated.protocolCborControl)
        else { return XCTFail("missing a connection") }

        stream.push(tempo120)
        await events.wait { $0.contains(.tempoBpm(120)) }
        // The dump's stale copy of the same address, then the two runs that
        // close the phase.
        control.pushItems([
            Cbor.paramWrite(addr: tempoAddress, value: Int64(100 * Generated.tempoBpmScale)),
            FakeDevice.dumpEndRun, FakeDevice.dumpEndRun,
        ])
        let seen = await events.wait { $0.contains(.syncCompleted(source: .control)) }
        XCTAssertTrue(seen.contains(.syncCompleted(source: .control)))
        let state = await model.snapshot()
        XCTAssertEqual(state.rig.tempoBpm, 120, "the live tempo held; the dump's copy was refused")
        await model.close()
    }

    /// A dump that never sends its end run still ends, at the settle time,
    /// with everything it did send in the tree.
    func testADumpWithoutItsEndMarkerSettles() async throws {
        let device = try FakeDevice(offerCbor: true)
        defer { device.stop() }
        device.configure {
            $0.dumpItems = [Cbor.paramWrite(addr: Generated.morphAddress, value: 100)]
        }
        let model = try await DeviceModel.connect(
            host: host, port: device.port, options: ConnectOptions(sync: .off))
        let (events, _) = await attach(model)
        await events.wait { $0.contains(.channelChanged(channel: .control, state: .open)) }
        let opened = ContinuousClock.Instant.now
        let seen = await events.wait(within: .seconds(3)) {
            $0.contains(.syncCompleted(source: .control))
        }
        let elapsed = opened.duration(to: .now)
        XCTAssertTrue(seen.contains(.syncCompleted(source: .control)))
        XCTAssertGreaterThanOrEqual(
            elapsed, .milliseconds(Generated.dumpSettleMs) - .milliseconds(50))
        let state = await model.snapshot()
        XCTAssertEqual(state.morph, 100)
        await model.close()
    }

    /// `reopenControl()` opening the link and returning the connection to
    /// `connected` — the success path the refusal tests do not cover.
    func testReopenControlRecoversADegradedConnection() async throws {
        // The greeting offers CBOR, but the device refuses the selection at
        // first, so the connection degrades.
        let device = try FakeDevice(offerCbor: true)
        defer { device.stop() }
        device.configure { $0.accepts = [Generated.protocolMidi3Stream] }
        let model = try await DeviceModel.connect(
            host: host, port: device.port, options: ConnectOptions(sync: .off))
        let (events, _) = await attach(model)
        await events.wait { $0.contains(.connectionChanged(.degraded)) }

        // The device starts accepting the control protocol. The reopen gap is
        // thirty seconds by spec; forget the last attempt so the reopen is
        // allowed now (the ledger still spaces the socket).
        device.configure { $0.accepts.insert(Generated.protocolCborControl) }
        await model.clearControlAttemptForTests()
        try await model.reopenControl()
        let seen = await events.wait { $0.contains(.syncCompleted(source: .control)) }
        XCTAssertTrue(seen.contains(.syncCompleted(source: .control)))
        let state = await model.snapshot()
        XCTAssertEqual(state.connection, .connected)
        XCTAssertEqual(state.channels, Channels(stream: .open, control: .open))
        XCTAssertEqual(state.morph, 8192, "the dump folded")
        XCTAssertEqual(device.connections.count, 3)
        await model.close()
    }

    /// `close()` while the default-policy control open is still parked in the
    /// ledger returns promptly and leaves no control socket behind.
    func testCloseCancelsAControlLinkStillWaitingItsTurn() async throws {
        let device = try FakeDevice(offerCbor: true)
        defer { device.stop() }
        let model = try await DeviceModel.connect(
            host: host, port: device.port, options: ConnectOptions(sync: .off))
        // The control open is queued in the ledger, a cooldown behind the
        // stream.
        let started = ContinuousClock.Instant.now
        await model.close()
        let cooldown = Duration.seconds(Session.connectionCooldown)
        XCTAssertLessThan(
            started.duration(to: .now), cooldown / 4, "close waited on the parked open")
        // Well past when the control open would have landed: it never did.
        try? await Task.sleep(for: cooldown + .milliseconds(300))
        XCTAssertEqual(device.connections.count, 1)
    }

    /// A message that rode in on the handshake acceptance tail is folded
    /// before the first read of the stream.
    func testTheHandshakeTailIsDecodedBeforeTheFirstRead() async throws {
        let device = try FakeDevice()
        defer { device.stop() }
        device.configure {
            $0.tailMessages = [
                Nrpn.sysex(
                    product: 0, device: 0, function: Generated.fnStringParam,
                    page: Generated.pageStrings, number: Generated.stringRigName,
                    values: Array("Tail Rig".utf8) + [0])
            ]
        }
        let model = try await DeviceModel.connect(
            host: host, port: device.port, options: ConnectOptions(control: .off, sync: .off))
        let state = await model.snapshot()
        XCTAssertEqual(state.rig.name, "Tail Rig", "folded by the time connect returned")
        await model.close()
    }

    /// A stream loss with the control link open closes the control link too:
    /// to `closed`, not `lost`, and both sockets drop together.
    func testAStreamLossClosesTheControlLinkToo() async throws {
        let device = try FakeDevice(offerCbor: true)
        defer { device.stop() }
        let model = try await DeviceModel.connect(
            host: host, port: device.port, options: ConnectOptions(sync: .off))
        let (events, _) = await attach(model)
        await events.wait { $0.contains(.channelChanged(channel: .control, state: .open)) }
        guard let stream = await device.connection(selecting: Generated.protocolMidi3Stream),
            let control = await device.connection(selecting: Generated.protocolCborControl)
        else { return XCTFail("missing a connection") }

        stream.hangUp()
        let seen = await events.wait { $0.contains(.disconnected) }
        XCTAssertTrue(seen.contains(.channelChanged(channel: .control, state: .closed)))
        XCTAssertFalse(seen.contains(.channelChanged(channel: .control, state: .lost)))
        XCTAssertTrue(seen.contains(.channelChanged(channel: .stream, state: .lost)))
        // The control link goes first: it is closed because the stream went.
        XCTAssertLessThan(
            seen.firstIndex(of: .channelChanged(channel: .control, state: .closed)) ?? .max,
            seen.firstIndex(of: .channelChanged(channel: .stream, state: .lost)) ?? 0)
        let state = await model.snapshot()
        XCTAssertEqual(state.channels, Channels(stream: .lost, control: .closed))
        let closed = await control.wait { $0.isClosed }
        XCTAssertTrue(closed, "the control socket really dropped")
        await model.close()
    }

    /// The deprecated `applyCbor` shim folds through the same funnel as the
    /// control link: a request waiting at the address is answered, a position
    /// reaches the Navigator, and the tree moves.
    func testApplyCborShimStillFolds() async throws {
        let device = try FakeDevice()
        defer { device.stop() }
        device.configure { $0.silent = [gainAddress] }
        let model = try await DeviceModel.connect(
            host: host, port: device.port, options: ConnectOptions(control: .off, sync: .off))
        let (events, _) = await attach(model)
        let stream = await device.connections(atLeast: 1)[0]

        let request = Task {
            try await model.requestParam(page: Generated.ampPage, number: Generated.gainNumber)
        }
        _ = await stream.received(atLeast: 1)
        await model.navigateTo(14)
        _ = await stream.received(atLeast: 3)
        await model.applyCbor(address: gainAddress, value: 42)
        await model.applyCbor(address: Generated.currentBankAddress, value: 2)
        await model.applyCbor(address: Generated.currentRigSlotAddress, value: 4)
        let gain = try await request.value
        XCTAssertEqual(gain, 42, "the shim answered the pending request")
        let seen = await events.wait { $0.contains(.navigationSettled(index: 14)) }
        XCTAssertTrue(
            seen.contains(.navigationSettled(index: 14)),
            "the shim's position reached the Navigator")
        let state = await model.snapshot()
        XCTAssertEqual(state.amp.gain, 42)
        XCTAssertEqual(state.currentRigIndex, 14)
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
        // A rendered string of it is refused the same way: a `$7C` for the
        // morph draws the same silence as a `$41`.
        do {
            _ = try await model.requestRender(
                page: Generated.pageMorph, number: Generated.morphNumber, value: 0)
            XCTFail("a render of the morph must be refused")
        } catch let error as RequestError {
            XCTAssertEqual(error, .unreadable)
        }
        try? await Task.sleep(for: .milliseconds(100))
        let stream = await device.connections(atLeast: 1)[0]
        XCTAssertEqual(stream.received, [])
        await model.close()
    }

    /// A reply wider than the 14 bits a `$01` carries — only the control wire
    /// can put one at the address — is not the stream's answer.
    func testAReplyWiderThanFourteenBitsIsUnreadable() async throws {
        let device = try FakeDevice()
        defer { device.stop() }
        device.configure { $0.silent = [gainAddress] }
        let model = try await connectWithoutControl(device)
        let stream = await device.connections(atLeast: 1)[0]
        let request = Task {
            try await model.requestParam(page: Generated.ampPage, number: Generated.gainNumber)
        }
        _ = await stream.received(atLeast: 1)
        await model.applyCbor(address: gainAddress, value: 20000)
        do {
            _ = try await request.value
            XCTFail("a reply past 14 bits must be refused")
        } catch let error as RequestError {
            XCTAssertEqual(error, .unreadable)
        }
        await model.close()
    }

    /// An unsolicited push at the requested address is the reply: the value
    /// is no less current for not having been asked for.
    func testAnUnsolicitedPushAtTheAddressResolvesARequest() async throws {
        let device = try FakeDevice()
        defer { device.stop() }
        device.configure { $0.silent = [gainAddress] }
        let model = try await connectWithoutControl(device)
        let stream = await device.connections(atLeast: 1)[0]
        let request = Task {
            try await model.requestParam(page: Generated.ampPage, number: Generated.gainNumber)
        }
        let received = await stream.received(atLeast: 1)
        XCTAssertEqual(received.count, 1)
        stream.push(pushed(page: Generated.ampPage, number: Generated.gainNumber, value: 42))
        let gain = try await request.value
        XCTAssertEqual(gain, 42)
        await model.close()
    }

    /// Three slow changes in one write are one read, and one snapshot.
    func testAStreamReadChunkRepublishesTheSnapshotOnce() async throws {
        let device = try FakeDevice()
        defer { device.stop() }
        let model = try await connectWithoutControl(device)
        let (events, snapshots) = await attach(model)
        let stream = await device.connections(atLeast: 1)[0]

        var burst: [UInt8] = []
        burst += Midi3.frame(
            pushed(page: Generated.ampPage, number: Generated.gainNumber, value: 1))
        burst += Midi3.frame(tempo120)
        burst += Midi3.frame(pushed(page: 0x3D, number: 0, value: 179))
        stream.pushRaw(burst)
        await events.wait { $0.contains(.effectChanged(slot: 7)) }
        try? await Task.sleep(for: .milliseconds(100))
        // The current state at subscription, then the one the chunk raised.
        let published = snapshots.all
        XCTAssertEqual(published.count, 2)
        XCTAssertEqual(published.last?.amp.gain, 1)
        XCTAssertEqual(published.last?.rig.tempoBpm, 120)
        XCTAssertEqual(published.last?.effects[7].kind, 179)
        await model.close()
    }

    /// The connect-time burst against a device that answers nothing: every
    /// one of the 46 times out, and then the burst reports itself done —
    /// `syncCompleted` last.
    func testTheBurstCompletesEvenWhenNothingAnswers() async throws {
        let device = try FakeDevice()
        defer { device.stop() }
        device.configure { $0.answers = false }
        let model = try await connectWithoutControl(device, sync: .streamBurst)
        let (events, _) = await attach(model)
        let seen = await events.wait { $0.contains(.syncCompleted(source: .stream)) }
        let timedOut = seen.filter { if case .requestTimedOut = $0 { true } else { false } }
        XCTAssertEqual(timedOut.count, 46)
        XCTAssertEqual(seen.last, .syncCompleted(source: .stream))
        await model.close()
    }

    /// `refresh()` with one row unanswered: `timeout`, while every answered
    /// row still folded into the tree.
    func testRefreshReportsATimeoutAfterTheRestLanded() async throws {
        let device = try FakeDevice()
        defer { device.stop() }
        device.configure { $0.silent = [rigNameAddress] }
        let model = try await connectWithoutControl(device)
        do {
            try await model.refresh()
            XCTFail("a silent row must time the refresh out")
        } catch let error as RequestError {
            XCTAssertEqual(error, .timeout)
        }
        let state = await model.snapshot()
        XCTAssertNil(state.rig.name, "the one silent row never landed")
        XCTAssertEqual(state.amp.name, "X", "the rest did")
        XCTAssertEqual(state.effects[7].on, false)
        await model.close()
    }

    /// Requests are refused once the stream is gone, and one still waiting
    /// when the model closes fails the same way.
    func testRequestsAreRefusedOnceDisconnected() async throws {
        let device = try FakeDevice()
        defer { device.stop() }
        device.configure { $0.closeAfterHandshake = true }
        let model = try await connectWithoutControl(device)
        let (events, _) = await attach(model)
        await events.wait { $0.contains(.disconnected) }
        do {
            _ = try await model.requestParam(page: Generated.ampPage, number: Generated.gainNumber)
            XCTFail("a request on a lost stream must be refused")
        } catch let error as RequestError {
            XCTAssertEqual(error, .disconnected)
        }
        await model.close()
    }

    func testCloseFailsAPendingRequest() async throws {
        let device = try FakeDevice()
        defer { device.stop() }
        device.configure { $0.silent = [gainAddress] }
        let model = try await connectWithoutControl(device)
        let stream = await device.connections(atLeast: 1)[0]
        let request = Task {
            try await model.requestParam(page: Generated.ampPage, number: Generated.gainNumber)
        }
        _ = await stream.received(atLeast: 1)
        await model.close()
        do {
            _ = try await request.value
            XCTFail("a request pending at close must fail")
        } catch let error as RequestError {
            XCTAssertEqual(error, .disconnected)
        }
    }

    /// The snapshot stream yields the current state before anything fresh.
    func testSnapshotsYieldTheCurrentStateFirst() async throws {
        let device = try FakeDevice()
        defer { device.stop() }
        let model = try await connectWithoutControl(device)
        let (events, _) = await attach(model)
        let stream = await device.connections(atLeast: 1)[0]
        stream.push(tempo120)
        await events.wait { $0.contains(.tempoBpm(120)) }
        var iterator = await model.snapshots().makeAsyncIterator()
        let first = await iterator.next()
        let current = await model.snapshot()
        XCTAssertEqual(first, current)
        XCTAssertEqual(first?.rig.tempoBpm, 120)
        await model.close()
    }

    /// The parameter setters put the exact Single Parameter Changes on the
    /// wire, in the order called.
    func testParameterSettersEmitSingleParameterChanges() async throws {
        let device = try FakeDevice()
        defer { device.stop() }
        let model = try await connectWithoutControl(device)
        let stream = await device.connections(atLeast: 1)[0]
        try await model.setGain(8192)
        try await model.setEffectEnabled("rev", true)
        try await model.setEffectMix("DLY", 4096)
        try await model.setTempoBpm(120)
        try await model.setMainVolume(9000)
        try await model.setMonitorVolume(3000)
        try await model.setRigVolume(4096)
        let received = await stream.received(atLeast: 7)
        XCTAssertEqual(
            received,
            [
                written(page: Generated.ampPage, number: Generated.gainNumber, value: 8192),
                written(page: 0x3D, number: 3, value: 1),
                written(page: 0x3C, number: 4, value: 4096),
                written(page: Generated.pageRigSettings, number: 0, value: 7680),
                written(page: Generated.systemPage, number: 0, value: 9000),
                written(page: Generated.systemPage, number: 2, value: 3000),
                written(page: Generated.pageRigSettings, number: 1, value: 4096),
            ])
        await model.close()
    }

    func testSetTempoClampsToTheFourteenBitMaximum() async throws {
        let device = try FakeDevice()
        defer { device.stop() }
        let model = try await connectWithoutControl(device)
        let stream = await device.connections(atLeast: 1)[0]
        try await model.setTempoBpm(9999)
        let received = await stream.received(atLeast: 1)
        XCTAssertEqual(
            received,
            [written(page: Generated.pageRigSettings, number: 0, value: Generated.fullScale)])
        await model.close()
    }

    func testActionsEmitControlChanges() async throws {
        let device = try FakeDevice()
        defer { device.stop() }
        let model = try await connectWithoutControl(device)
        let stream = await device.connections(atLeast: 1)[0]
        try await model.tapTempo()
        try await model.bank(4)
        try await model.tunerMode(true)
        try await model.freeze(false)
        try await model.sendRaw([0xB0, 30, 1])
        let received = await stream.received(atLeast: 5)
        XCTAssertEqual(
            received,
            [
                Control.tapTempo.message(), Control.bankPreselect(3).message(),
                Control.tunerMode(true).message(), Control.freeze(false).message(), [0xB0, 30, 1],
            ])
        await model.close()
    }

    func testUnknownEffectSlotIsRejected() async throws {
        let device = try FakeDevice()
        defer { device.stop() }
        let model = try await connectWithoutControl(device)
        let stream = await device.connections(atLeast: 1)[0]
        do {
            try await model.setEffectEnabled("nope", true)
            XCTFail("an unknown slot must be refused")
        } catch let error as CommandError {
            XCTAssertEqual(error, .unknownSlot("nope"))
        }
        try? await Task.sleep(for: .milliseconds(50))
        XCTAssertEqual(stream.received, [])
        await model.close()
    }

    /// The stream link's command queue holds sixty-four commands and no
    /// more; the Navigator's pair is queued as one unit or not at all.
    func testTheCommandQueueIsBounded() async throws {
        let device = try FakeDevice()
        defer { device.stop() }
        let session = try await Session.connect(host: host, port: device.port, timeout: 2)
        let link = StreamLink(session: session)
        defer { link.close() }
        for _ in 0..<(StreamLink.commandQueueDepth - 1) {
            XCTAssertTrue(link.enqueue(Control.tapTempo.message()))
        }
        XCTAssertFalse(link.enqueue(pair: ([1], [2])), "no room for two")
        XCTAssertTrue(link.enqueue(Control.tapTempo.message()), "room for one")
        XCTAssertFalse(link.enqueue(Control.tapTempo.message()), "full")
        XCTAssertEqual(link.commands.count, StreamLink.commandQueueDepth)
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

    /// `refresh()` is every `request = true` row, sent in the table's
    /// (address-sorted) order, and the connect-time burst is `refresh()`.
    func testRefreshIssuesTheFortySixRequests() async throws {
        let device = try FakeDevice()
        defer { device.stop() }
        let model = try await connectWithoutControl(device)
        let stream = await device.connections(atLeast: 1)[0]
        try await model.refresh()
        let received = stream.received
        XCTAssertEqual(received.count, 46)
        // The first request is the Rig Name string tag, then the functions run
        // in blocks — the wire order is the table order in every language.
        XCTAssertEqual(
            received.first,
            Nrpn.requestString(
                product: DeviceModel.product, device: DeviceModel.device,
                page: Generated.pageStrings, number: Generated.stringRigName))
        let functions = received.map { $0[6] }
        XCTAssertEqual(
            functions,
            Array(repeating: Generated.fnRequestString, count: 6)
                + Array(repeating: Generated.fnRequestSingle, count: 23)
                + Array(repeating: Generated.fnRequestExtString, count: 15)
                + Array(repeating: Generated.fnRequestExtParam, count: 2))
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

    /// A reconnect keeps the tree, never says `disconnected`, and counts on
    /// with a doubling backoff when a redial is refused.
    func testAReconnectKeepsTheTreeAndRetriesUntilItLands() async throws {
        let device = try FakeDevice()
        defer { device.stop() }
        let backoff = Backoff(initial: .milliseconds(50), max: .milliseconds(200))
        let model = try await DeviceModel.connect(
            host: host, port: device.port,
            options: options(reconnect: ReconnectPolicy(stream: backoff)))
        let (events, _) = await attach(model)
        let stream = await device.connections(atLeast: 1)[0]

        // A value in the tree before the loss; it must survive the reconnect.
        stream.push(tempo120)
        await events.wait { $0.contains(.tempoBpm(120)) }

        // Refuse the first redial, then let the next one through.
        device.pauseAccepting()
        stream.hangUp()
        let second = await events.wait(within: .seconds(6)) {
            $0.contains(.connectionChanged(.reconnecting(attempt: 2)))
        }
        XCTAssertTrue(second.contains(.connectionChanged(.reconnecting(attempt: 2))))
        XCTAssertEqual(device.refused, 1)
        device.resumeAccepting()
        let seen = await events.wait(within: .seconds(6)) { $0.contains(.connected) }
        XCTAssertTrue(seen.contains(.connected))
        XCTAssertFalse(seen.contains(.disconnected))
        XCTAssertFalse(seen.contains(.connectionChanged(.disconnected)))
        let state = await model.snapshot()
        XCTAssertEqual(state.connection, .connected)
        XCTAssertEqual(state.rig.tempoBpm, 120, "the tree carried across the reconnect")
        await model.close()
    }

    /// Under `required`, a redial whose stream opens but whose control link
    /// is refused is the next failure: the attempt count goes on, and with
    /// it the backoff, rather than starting over at one for every such life.
    func testARequiredControlRefusalKeepsCountingAttempts() async throws {
        let device = try FakeDevice(offerCbor: true)
        defer { device.stop() }
        let backoff = Backoff(initial: .milliseconds(50), max: .milliseconds(200))
        let model = try await DeviceModel.connect(
            host: host, port: device.port,
            options: ConnectOptions(
                control: .required, sync: .off, reconnect: ReconnectPolicy(stream: backoff)))
        let (events, _) = await attach(model)

        device.configure { $0.accepts = [Generated.protocolMidi3Stream] }
        device.hangUpAll()
        let refused = await events.wait(within: .seconds(6)) {
            $0.contains(.connectionChanged(.reconnecting(attempt: 2)))
        }
        XCTAssertTrue(refused.contains(.connectionChanged(.reconnecting(attempt: 1))))
        XCTAssertTrue(
            refused.contains(.connectionChanged(.reconnecting(attempt: 2))),
            "the refused control link counts as an attempt")
        device.configure { $0.accepts.insert(Generated.protocolCborControl) }
        // The next life comes up whole: connected after the second attempt,
        // with a dump of its own.
        let seen = await events.wait(within: .seconds(6)) {
            ($0.lastIndex(of: .connected) ?? -1)
                > ($0.firstIndex(of: .connectionChanged(.reconnecting(attempt: 2))) ?? .max)
        }
        XCTAssertFalse(seen.contains(.disconnected))
        await events.wait {
            ($0.lastIndex(of: .channelChanged(channel: .control, state: .open)) ?? -1)
                > ($0.lastIndex(of: .connected) ?? .max)
        }
        let state = await model.snapshot()
        XCTAssertEqual(state.connection, .connected)
        XCTAssertEqual(state.channels, Channels(stream: .open, control: .open))
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
        // The teardown, in order: the stream closes, then the compatibility
        // event, then the transition it belongs to. (With the control link
        // open, its `closed` precedes the stream's.)
        XCTAssertEqual(
            Array(events.all.suffix(3)),
            [
                .channelChanged(channel: .stream, state: .closed), .disconnected,
                .connectionChanged(.disconnected),
            ])
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
        // Out of range, or no step at all: nothing to load.
        await model.selectSlot(0)
        await model.selectSlot(UInt8(Params.bankSlots + 1))
        await model.stepRig(by: 0)
        try? await Task.sleep(for: .milliseconds(100))
        XCTAssertEqual(stream.received, [], "an out-of-range slot or a zero step is ignored")
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
        // Past the settle and the window: the cancelled timers stay quiet.
        try? await Task.sleep(
            for: .milliseconds(Generated.rigLoadSettleMs + Generated.pendingWindowMs + 100))
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

    /// `close()` cancels the Navigator's timers and clears the aim without an
    /// event: no late settle or drop after the timers would have fired.
    func testCloseCancelsTheNavigatorTimers() async throws {
        let device = try FakeDevice()
        defer { device.stop() }
        let model = try await connect(device)
        let (events, _) = await attach(model)
        let stream = await device.connections(atLeast: 1)[0]

        await model.navigateTo(14)
        _ = await stream.received(atLeast: 2)
        await model.close()
        let state = await model.snapshot()
        XCTAssertEqual(state.navigation, Navigation())
        try? await Task.sleep(
            for: .milliseconds(Generated.rigLoadSettleMs + Generated.pendingWindowMs + 100))
        XCTAssertFalse(
            events.all.contains {
                switch $0 {
                case .navigationDropped, .navigationSettled: return true
                default: return false
                }
            })
    }

    /// A position carried by the control link settles the aim as readily as
    /// one on the stream.
    func testAPositionFromTheControlLinkSettlesTheAim() async throws {
        let device = try FakeDevice(offerCbor: true)
        defer { device.stop() }
        let model = try await DeviceModel.connect(
            host: host, port: device.port, options: ConnectOptions(sync: .off))
        let (events, _) = await attach(model)
        await events.wait { $0.contains(.syncCompleted(source: .control)) }
        guard let stream = await device.connection(selecting: Generated.protocolMidi3Stream),
            let control = await device.connection(selecting: Generated.protocolCborControl)
        else { return XCTFail("missing a connection") }

        await model.navigateTo(14)
        let received = await stream.received(atLeast: 2)
        XCTAssertEqual(received, loadPair(14))
        // Bank 2, slot 4: index 14. The bank alone (with the dump's slot 1)
        // is index 11, which is not the aim and is ignored.
        control.pushItems([
            Cbor.paramWrite(addr: Generated.currentBankAddress, value: 2),
            Cbor.paramWrite(addr: Generated.currentRigSlotAddress, value: 4),
        ])
        let seen = await events.wait { $0.contains(.navigationSettled(index: 14)) }
        XCTAssertTrue(seen.contains(.navigationSettled(index: 14)))
        let state = await model.snapshot()
        XCTAssertNil(state.navigation.aim)
        XCTAssertEqual(state.currentRigIndex, 14)
        await model.close()
    }
}
