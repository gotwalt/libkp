import Foundation

/// A live store over a Profiler's stream: a typed ``DeviceState`` snapshot the
/// UI binds to, plus commands for writes.
///
/// Two layers:
///
/// - ``DeviceState`` is the **pure, network-free core**: a plain-data tree that
///   decodes one already-unframed MIDI message at a time via
///   ``DeviceState/apply(_:)``. It does no IO, so tests drive it with
///   synthesized messages.
/// - `DeviceModel` is the **async handle**: an actor that owns the ``Session``,
///   runs an ingest loop, keeps the shared snapshot, and publishes both a
///   coalesced snapshot stream and a granular event stream.
///
/// State is classified FAST vs SLOW. **FAST** = the meter ``RealtimeStatus``
/// block, the beat pulse and tuner deviance — high-rate, poll them via
/// ``status()``. **SLOW** = everything else (rig / amp / cab / effect / output /
/// tempo / morph / tuner-note / connection); those drive the coalesced
/// ``snapshots()`` stream.
///
/// Commands split into two groups:
///
/// - **Parameters** (`set*`) — settable values the device also reports back.
///   They go out as 14-bit NRPN `$01` Single Parameter Changes, so the device
///   echoes the change on the same stream the model ingests and the snapshot
///   stays consistent.
/// - **Actions** (verbs) — momentary presses and live expression that carry no
///   stored value. They go out as 7-bit Control Change messages via ``Control``
///   and are *not* reflected in state.
public actor DeviceModel {
    /// Product byte addressed in outbound SysEx (0x00 = Profiler).
    public static let product: UInt8 = Generated.productProfiler
    /// Device byte addressed in outbound SysEx (0x7F = omni).
    public static let device: UInt8 = Generated.deviceOmni
    /// MIDI channel used for Control Change commands (0 = channel 1).
    public static let ccChannel: UInt8 = 0
    /// The maximum 14-bit NRPN value.
    public static let nrpnMax: UInt16 = Generated.fullScale

    /// Read idle gap driving the ingest loop; short so it reacts per packet.
    private static let readIdle: TimeInterval = 0.03

    private let session: Session
    private var state = DeviceState()
    private var unframer = Midi3.Unframer()
    private var ingestTask: Task<Void, Never>?
    private var eventContinuations: [UUID: AsyncStream<DeviceEvent>.Continuation] = [:]
    private var snapshotContinuations: [UUID: AsyncStream<DeviceState>.Continuation] = [:]

    /// The address this model is connected to.
    public nonisolated let peer: String

    private init(session: Session) {
        self.session = session
        peer = session.peer
    }

    // MARK: - Lifecycle

    /// Connect to `host:5727`, open the streaming protocol, start the ingest
    /// loop, and kick off a read-only initial sync (rig strings + effect slots).
    ///
    /// Returns once the session is established; the state fills in as the
    /// device's replies stream back. Subscribe *before* awaiting fresh events.
    public static func connect(
        host: String,
        port: UInt16 = Generated.port,
        preferredProtocols: [String] = [Generated.protocolMidi3Stream]
    ) async throws -> DeviceModel {
        let session = try await Session.connect(host: host, port: port)
        let outcome = try await session.handshake(preferred: preferredProtocols, idle: readIdle)
        try await session.writeSessionPreamble()

        let model = DeviceModel(session: session)
        await model.start(tail: outcome.responseTail)
        try? await model.refreshRig()
        return model
    }

    /// Mark the session live and start ingesting, priming with any burst that
    /// rode in on the handshake acceptance.
    private func start(tail: [UInt8]) {
        state.connection = .connected
        emit(.connected)
        publishSnapshot()
        applyChunk(unframer.push(tail))
        ingestTask = Task { [weak self] in
            await self?.ingestLoop()
        }
    }

    /// Read, unframe and apply, until the device closes the connection.
    private func ingestLoop() async {
        while !Task.isCancelled {
            do {
                let chunk = try await session.readOnce(wait: DeviceModel.readIdle)
                if chunk.isEmpty { continue }
                applyChunk(unframer.push(chunk))
            } catch {
                markDisconnected()
                return
            }
        }
    }

    /// Close the session and finish both streams.
    public func close() {
        ingestTask?.cancel()
        ingestTask = nil
        session.close()
        markDisconnected()
    }

    private func markDisconnected() {
        guard state.connection == .connected else { return }
        state.connection = .disconnected
        emit(.disconnected)
        publishSnapshot()
        for (_, continuation) in eventContinuations { continuation.finish() }
        for (_, continuation) in snapshotContinuations { continuation.finish() }
        eventContinuations.removeAll()
        snapshotContinuations.removeAll()
    }

    // MARK: - Store access

    /// The current state snapshot.
    public func snapshot() -> DeviceState { state }

    /// The latest FAST meter/tuner frame — poll this per animation frame.
    /// Equals `snapshot().status`, but skips copying the whole tree.
    public func status() -> RealtimeStatus { state.status }

    /// The **store**: a fresh snapshot each time *slow* state changes, coalesced
    /// to at most one per ingested stream chunk.
    public func snapshots() -> AsyncStream<DeviceState> {
        let id = UUID()
        let (stream, continuation) = AsyncStream<DeviceState>.makeStream(
            bufferingPolicy: .bufferingNewest(64)
        )
        snapshotContinuations[id] = continuation
        continuation.onTermination = { [weak self] _ in
            Task { await self?.dropSnapshotContinuation(id) }
        }
        continuation.yield(state)
        return stream
    }

    /// The granular delta stream — every ``DeviceEvent``, including the FAST
    /// ones. For callers that want per-message deltas rather than snapshots.
    public func events() -> AsyncStream<DeviceEvent> {
        let id = UUID()
        let (stream, continuation) = AsyncStream<DeviceEvent>.makeStream(
            bufferingPolicy: .bufferingNewest(1024)
        )
        eventContinuations[id] = continuation
        continuation.onTermination = { [weak self] _ in
            Task { await self?.dropEventContinuation(id) }
        }
        return stream
    }

    private func dropEventContinuation(_ id: UUID) { eventContinuations[id] = nil }
    private func dropSnapshotContinuation(_ id: UUID) { snapshotContinuations[id] = nil }

    private func emit(_ event: DeviceEvent) {
        for (_, continuation) in eventContinuations { continuation.yield(event) }
    }

    private func publishSnapshot() {
        for (_, continuation) in snapshotContinuations { continuation.yield(state) }
    }

    /// Apply every message in one ingested chunk: publish each granular event,
    /// and — if any message changed a slow field — emit exactly one snapshot.
    private func applyChunk(_ messages: [[UInt8]]) {
        var slowChanged = false
        for message in messages {
            let outcome = state.apply(message)
            for event in outcome.events { emit(event) }
            slowChanged = slowChanged || outcome.slowChanged
        }
        if slowChanged { publishSnapshot() }
    }

    // MARK: - Parameters (NRPN `$01`, 14-bit, state-tracked)

    /// Set an arbitrary numeric parameter — the escape hatch behind every
    /// parameter setter below.
    public func setParam(page: UInt8, number: UInt8, value: UInt16) async throws {
        try await send(Nrpn.setSingle(
            product: DeviceModel.product, device: DeviceModel.device,
            page: page, number: number, value: value
        ))
    }

    /// Set the amp Gain, 0–16383 (NRPN `0x0A/4`).
    public func setGain(_ value: UInt16) async throws {
        try await setParam(page: Generated.ampPage, number: Generated.gainNumber, value: value)
    }

    /// Set the Rig Volume, 0–16383 (NRPN `0x04/1`).
    public func setRigVolume(_ value: UInt16) async throws {
        try await setParam(page: Generated.pageRigSettings, number: Generated.rigVolumeNumber, value: value)
    }

    /// Set the Main Output Volume, 0–16383 (NRPN `0x7F/0`).
    public func setMainVolume(_ value: UInt16) async throws {
        try await setParam(page: Generated.systemPage, number: Generated.mainVolumeNumber, value: value)
    }

    /// Set the Monitor Output Volume, 0–16383 (NRPN `0x7F/2`).
    public func setMonitorVolume(_ value: UInt16) async throws {
        try await setParam(page: Generated.systemPage, number: Generated.monitorVolumeNumber, value: value)
    }

    /// Turn an effect slot on or off via a `$01` write to number 3.
    public func setEffectEnabled(_ slot: String, _ on: Bool) async throws {
        guard let page = Params.effectSlotPage(slot) else { throw CommandError.unknownSlot(slot) }
        try await setParam(page: page, number: Generated.effectParamState, value: on ? 1 : 0)
    }

    /// Set an effect slot's dry/wet Mix, 0–16383.
    public func setEffectMix(_ slot: String, _ value: UInt16) async throws {
        guard let page = Params.effectSlotPage(slot) else { throw CommandError.unknownSlot(slot) }
        try await setParam(page: page, number: Generated.effectParamMix, value: value)
    }

    /// Set the Tempo in whole beats per minute (NRPN `0x04/0`). The wire value
    /// is `bpm × 64`, clamped to the 14-bit maximum (≈255 BPM).
    public func setTempoBpm(_ bpm: UInt16) async throws {
        let scaled = UInt32(bpm) * UInt32(Generated.tempoBpmScale)
        let value = UInt16(min(scaled, UInt32(DeviceModel.nrpnMax)))
        try await setParam(page: Generated.pageRigSettings, number: Generated.tempoNumber, value: value)
    }

    // MARK: - Read-only requests

    /// Re-request the rig strings and every effect slot's Type/State. Also the
    /// initial sync run at connect time: it only issues value *requests* and
    /// changes nothing on the device.
    public func refreshRig() async throws {
        // Rig Name, Author, Comment, Date, Amp Name, Cabinet Name.
        for tag in [1, 2, 4, 3, 10, 32] as [UInt8] {
            try await send(Nrpn.requestString(
                product: DeviceModel.product, device: DeviceModel.device,
                page: Generated.pageStrings, number: tag
            ))
        }
        for slot in Params.effectSlots {
            try await send(Nrpn.requestSingle(
                product: DeviceModel.product, device: DeviceModel.device,
                page: slot.page, number: Generated.effectParamType
            ))
            try await send(Nrpn.requestSingle(
                product: DeviceModel.product, device: DeviceModel.device,
                page: slot.page, number: Generated.effectParamState
            ))
        }
    }

    /// Request one numeric parameter's current value (function `$41`). The
    /// device answers with a `$01` message on the same stream, which the ingest
    /// loop folds into the snapshot. Read-only.
    public func requestParam(page: UInt8, number: UInt8) async throws {
        try await send(Nrpn.requestSingle(
            product: DeviceModel.product, device: DeviceModel.device, page: page, number: number
        ))
    }

    /// Request one string parameter (function `$43`), e.g. a page-0 string tag.
    /// Read-only.
    public func requestString(page: UInt8 = Generated.pageStrings, number: UInt8) async throws {
        try await send(Nrpn.requestString(
            product: DeviceModel.product, device: DeviceModel.device, page: page, number: number
        ))
    }

    /// Request a parameter value rendered to its exact display text (function
    /// `$7C`) — e.g. `"5.2"`, `"120 BPM"`, `"<0.0>"` instead of a generic
    /// percentage. Read-only.
    ///
    /// This is a streaming request/response: it enqueues the request and returns
    /// immediately. Watch ``events()`` for the matching
    /// ``DeviceEvent/renderedString(page:number:value:text:)``.
    public func requestRender(page: UInt8, number: UInt8, value: UInt16) async throws {
        try await send(Nrpn.requestRenderedString(
            product: DeviceModel.product, device: DeviceModel.device,
            page: page, number: number, value: value
        ))
    }

    /// Send the bidirectional beacon so the device starts streaming its selected
    /// parameter set. Re-send within half the lease to keep it alive.
    public func sendBeacon(init isInit: Bool = true, tuner: Bool = true, leaseSecs: UInt8 = 30) async throws {
        try await send(Nrpn.beacon(
            init: isInit, tuner: tuner, leaseSecs: leaseSecs, product: DeviceModel.product
        ))
    }

    // MARK: - Actions (CC, momentary, NOT stored in state)

    /// Send an arbitrary ``Control`` on the command channel.
    public func send(control: Control) async throws {
        try await send(control.message(channel: DeviceModel.ccChannel))
    }

    /// Select rig slot 1–5 in the current bank (CC50–54).
    public func selectRig(_ slot: UInt8) async throws { try await send(control: .loadSlot(slot)) }
    /// Step to the next rig (CC48).
    public func rigUp() async throws { try await send(control: .up) }
    /// Step to the previous rig (CC49).
    public func rigDown() async throws { try await send(control: .down) }
    /// Preselect bank `n` (1-based; CC47).
    public func bank(_ n: UInt16) async throws {
        try await send(control: .bankPreselect(UInt8(truncatingIfNeeded: max(n, 1) - 1)))
    }
    /// Tap the tempo (CC30).
    public func tapTempo() async throws { try await send(control: .tapTempo) }
    /// Open (`true`) or close (`false`) the tuner (CC31).
    public func tunerMode(_ open: Bool) async throws { try await send(control: .tunerMode(open)) }
    /// Morph button (CC80): `rise` climbs to the morph target, else falls back.
    public func morphButton(_ rise: Bool) async throws { try await send(control: .morphButton(rise)) }
    /// Set the morph pedal position 0–127 (CC11).
    public func morphPedal(_ value: UInt8) async throws { try await send(control: .morphPedal(value)) }
    /// Delay + Reverb Freeze (CC35).
    public func freeze(_ on: Bool) async throws { try await send(control: .freeze(on)) }
    /// Rotary speaker speed (CC33): `fast` or slow.
    public func rotaryFast(_ fast: Bool) async throws { try await send(control: .rotaryFast(fast)) }
    /// Delay Infinity (CC34).
    public func delayInfinity(_ on: Bool) async throws { try await send(control: .delayInfinity(on)) }
    /// Toggle every module A–REV on/off (CC16).
    public func toggleAllModules() async throws { try await send(control: .toggleAllModules) }
    /// Press Effect Button `n` (I–IIII, clamped to 1...4; CC75–78).
    public func effectButton(_ n: UInt8) async throws { try await send(control: .effectButton(n)) }
    /// Set the wah pedal position 0–127 (CC1).
    public func wahPedal(_ value: UInt8) async throws { try await send(control: .wahPedal(value)) }
    /// Set the pitch pedal position 0–127 (CC4).
    public func pitchPedal(_ value: UInt8) async throws { try await send(control: .pitchPedal(value)) }
    /// Set the volume pedal position 0–127 (CC7).
    public func volumePedal(_ value: UInt8) async throws { try await send(control: .volumePedal(value)) }
    /// Set the panorama 0–127 (CC10).
    public func panorama(_ value: UInt8) async throws { try await send(control: .panorama(value)) }

    // MARK: - Wire

    /// Frame and write raw (pre-framing) MIDI bytes to the device.
    private func send(_ bytes: [UInt8]) async throws {
        guard state.connection == .connected else { throw CommandError.disconnected }
        do {
            try await session.writeAll(Midi3.frame(bytes))
        } catch {
            markDisconnected()
            throw CommandError.disconnected
        }
    }
}
