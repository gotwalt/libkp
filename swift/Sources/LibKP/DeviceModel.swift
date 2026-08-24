import Foundation

/// A live store over a Profiler: a typed ``DeviceState`` snapshot the UI binds
/// to, plus commands for writes and requests for reads.
///
/// Two layers:
///
/// - ``DeviceState`` is the **pure, network-free core**: a plain-data tree that
///   folds one value at a time, whichever wire carried it, via
///   ``DeviceState/applyUpdate(_:)``. It does no IO, so tests drive it with
///   synthesized messages.
/// - `DeviceModel` is the **async handle**: an actor that owns every socket to
///   the device, keeps the shared snapshot, and publishes both a coalesced
///   snapshot stream and a granular event stream.
///
/// The model is the only thing in this library that holds a socket to the
/// device. It owns two links: the MIDI3 **stream**, which is required and is
/// what ``Connection/connected`` means, and — by default — the CBOR
/// **control** link, which asks for the device's state dump when it opens and
/// then carries the values the stream never does, the morph position above
/// all. Both feed one funnel, the single writer of the state tree, so a client
/// sees one handle, one tree and one event stream, and never a channel name
/// except in ``DeviceEvent/channelChanged(channel:state:)``. What the control
/// link may and may not do is decided at connect time by ``ConnectOptions``.
///
/// State is classified FAST vs SLOW. **FAST** = the meter ``RealtimeStatus``
/// block, the beat pulse and tuner deviance — high-rate, poll them via
/// ``status()``. **SLOW** = everything else (rig / amp / cab / effect / output /
/// tempo / morph / tuner-note / connection); those drive the coalesced
/// ``snapshots()`` stream, at most one snapshot per ingested chunk from either
/// link.
///
/// Commands split into three groups:
///
/// - **Parameters** (`set*`) — settable values the device stores. They go out as
///   14-bit NRPN `$01` Single Parameter Changes; the device applies the write
///   silently and does *not* echo it back on the stream, so follow a set with
///   ``requestParam(page:number:)`` when the snapshot should confirm the new
///   value.
/// - **Requests** (`request*`, `refresh*`) — read-only. Each one travels the
///   request lane: it is sent, waits for a value at its address, and returns
///   that value or ``RequestError/timeout``. The reply folds into the tree on
///   the way through.
/// - **Actions** (verbs) — momentary presses and live expression that carry no
///   stored value. They go out as 7-bit Control Change messages via ``Control``
///   and are *not* reflected in state.
/// - **Navigation** (``navigateTo(_:)``, ``stepRig(by:)``, ``stepBank(forward:)``,
///   ``selectSlot(_:)``) — the one way to load a rig. Every load goes through
///   the model's Navigator (see Navigator.swift), which serialises them so two
///   can never overlap on the wire; the direct routes — ``Control/loadSlot(_:)``,
///   ``Control/up``, ``Control/down``, a Program Change — are refused with
///   ``CommandError/rigLoadRequiresNavigator``. The outstanding aim is
///   ``DeviceState/navigation``.
public actor DeviceModel {
    /// Product byte addressed in outbound SysEx (0x00 = Profiler).
    public static let product: UInt8 = Generated.productProfiler
    /// Device byte addressed in outbound SysEx (0x7F = omni).
    public static let device: UInt8 = Generated.deviceOmni
    /// MIDI channel used for Control Change commands (0 = channel 1).
    public static let ccChannel: UInt8 = 0
    /// The maximum 14-bit NRPN value.
    public static let nrpnMax: UInt16 = Generated.fullScale

    /// Read idle gap driving the stream's ingest loop; short so it reacts per
    /// packet.
    static let readIdle: TimeInterval = 0.03

    /// The address this model is connected to, as `host:port`.
    public nonisolated let peer: String
    /// The options this model was connected with. They cannot change: a
    /// different policy is a different model.
    public nonisolated let options: ConnectOptions
    nonisolated let host: String
    nonisolated let port: UInt16

    var state = DeviceState()
    var eventContinuations: [UUID: AsyncStream<DeviceEvent>.Continuation] = [:]
    var snapshotContinuations: [UUID: AsyncStream<DeviceState>.Continuation] = [:]

    /// Set by ``close()``; nothing reopens after it.
    var closed = false
    /// Bumped whenever a life starts or ends, so a task from a previous life
    /// cannot touch the state of its successor. Every task captures the epoch
    /// it was started under and is refused if it no longer matches.
    var epoch = 0

    /// The stream link of the current life, or `nil` between lives.
    var stream: StreamLink?
    /// The control link, while it is open.
    var control: ControlLink?
    var controlIngestTask: Task<Void, Never>?
    var syncTask: Task<Void, Never>?
    var settleTask: Task<Void, Never>?
    var reopenTask: Task<Void, Never>?
    var reconnectTask: Task<Void, Never>?
    /// When the control link was last dialled — the moment the device saw the
    /// attempt, whether or not it succeeded — which is what the reopen gap
    /// is measured from.
    var lastControlAttempt: ContinuousClock.Instant?
    /// How many reconnect attempts the current outage has cost; 0 while up.
    var reconnectAttempt: UInt32 = 0
    /// Between the dump trigger and the dump's end (or its settle time).
    var dumpActive = false

    // The request lane (see Lane.swift).
    var pending: [UInt32: [PendingRequest]] = [:]
    var pendingRenders: [RenderKey: [PendingRequest]] = [:]
    var nextRequestID: UInt64 = 0
    var inFlight = 0
    var laneWaiters: [CheckedContinuation<Bool, Never>] = []

    // The Navigator (see Navigator.swift): the machine, and its two timers,
    // each armed under a serial so a superseded one cannot call back in.
    var navigator = NavigatorState()
    var navSettleTask: Task<Void, Never>?
    var navSettleSerial = 0
    var navWindowTask: Task<Void, Never>?
    var navWindowSerial = 0

    init(host: String, port: UInt16, options: ConnectOptions) {
        self.host = host
        self.port = port
        self.options = options
        peer = "\(host):\(port)"
    }

    /// Dropping the last handle closes both sockets and finishes the streams —
    /// the same as ``close()`` minus the events, which nobody is left to hear.
    /// The ingest loops hold this actor weakly, so a model nobody references
    /// does reach here even while the device is still sending.
    deinit {
        stream?.close()
        control?.close()
        for task in [
            controlIngestTask, syncTask, settleTask, reopenTask, reconnectTask, navSettleTask,
            navWindowTask,
        ] {
            task?.cancel()
        }
        for (_, continuation) in eventContinuations { continuation.finish() }
        for (_, continuation) in snapshotContinuations { continuation.finish() }
    }

    // MARK: - Store access

    /// The current state snapshot.
    public func snapshot() -> DeviceState { state }

    /// The latest FAST meter/tuner frame — poll this per animation frame.
    /// Equals `snapshot().status`, but skips copying the whole tree.
    public func status() -> RealtimeStatus { state.status }

    /// The **store**: the current state first, then a fresh snapshot each time
    /// *slow* state changes, coalesced to at most one per ingested chunk.
    ///
    /// The stream finishes only on ``close()``. Losing the device does not end
    /// it: the snapshot then shows ``Connection/disconnected`` — or
    /// ``Connection/reconnecting(attempt:)`` and, in time, ``Connection/connected``
    /// again, on this same stream — so a subscriber never has to resubscribe.
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
    /// Finishes only on ``close()``, like ``snapshots()``.
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

    func emit(_ event: DeviceEvent) {
        for (_, continuation) in eventContinuations { continuation.yield(event) }
    }

    func publishSnapshot() {
        for (_, continuation) in snapshotContinuations { continuation.yield(state) }
    }

    /// Fold one value from the CBOR channel into this model's tree, as a live
    /// push off the control channel.
    ///
    /// The model's own control link does this for every value it reads; this
    /// remains only for a client still holding its own ``CborSession`` beside
    /// a model, and goes with the next step.
    @available(*, deprecated, message: "the model's control link folds the CBOR channel itself")
    public func applyCbor(address: UInt32, value: Int64) {
        let outcome = state.applyCbor(address: address, value: value)
        for event in outcome.events { emit(event) }
        if outcome.slowChanged { publishSnapshot() }
    }

    // MARK: - Parameters (NRPN `$01`, 14-bit, state-tracked)

    /// Set an arbitrary numeric parameter — the escape hatch behind every
    /// parameter setter below.
    public func setParam(page: UInt8, number: UInt8, value: UInt16) async throws {
        try await write(
            Nrpn.setSingle(
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
        try await setParam(
            page: Generated.pageRigSettings, number: Generated.rigVolumeNumber, value: value)
    }

    /// Set the Main Output Volume, 0–16383 (NRPN `0x7F/0`).
    public func setMainVolume(_ value: UInt16) async throws {
        try await setParam(
            page: Generated.systemPage, number: Generated.mainVolumeNumber, value: value)
    }

    /// Set the Monitor Output Volume, 0–16383 (NRPN `0x7F/2`).
    public func setMonitorVolume(_ value: UInt16) async throws {
        try await setParam(
            page: Generated.systemPage, number: Generated.monitorVolumeNumber, value: value)
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
        try await setParam(
            page: Generated.pageRigSettings, number: Generated.tempoNumber, value: value)
    }

    /// Send the bidirectional beacon so the device starts streaming its selected
    /// parameter set. Re-send within half the lease to keep it alive.
    public func sendBeacon(
        init isInit: Bool = true, tuner: Bool = true, leaseSecs: UInt8 = 30
    ) async throws {
        try await write(
            Nrpn.beacon(
                init: isInit, tuner: tuner, leaseSecs: leaseSecs, product: DeviceModel.product
            ))
    }

    // MARK: - Actions (CC, momentary, NOT stored in state)

    /// Send an arbitrary ``Control`` on the command channel.
    ///
    /// The ones that load a rig — ``Control/loadSlot(_:)``, ``Control/up``,
    /// ``Control/down``, ``Control/programChange(_:)`` and
    /// ``Control/bankSelect(msb:lsb:)`` — are refused with
    /// ``CommandError/rigLoadRequiresNavigator`` before anything is written:
    /// a load that overlaps another wedges the device, and only the
    /// Navigator (``navigateTo(_:)`` and its steppers) can keep them apart.
    public func send(control: Control) async throws {
        switch control {
        case .loadSlot, .up, .down, .programChange, .bankSelect:
            throw CommandError.rigLoadRequiresNavigator
        default:
            try await write(control.message(channel: DeviceModel.ccChannel))
        }
    }

    /// Frame and write raw (pre-framing) MIDI bytes to the device, exactly as
    /// given — the escape hatch under every command here, with one refusal: a
    /// Program Change (status `0xC0`–`0xCF`), or a Control Change whose
    /// controller is one of ``Generated/rigLoadControllers``, is a rig load
    /// and throws ``CommandError/rigLoadRequiresNavigator`` before any byte
    /// is written. The bank preselect (CC47) loads nothing and passes.
    public func sendRaw(_ midi: [UInt8]) async throws {
        guard !DeviceModel.loadsARig(midi) else { throw CommandError.rigLoadRequiresNavigator }
        try await write(midi)
    }

    /// Whether raw MIDI carries a rig load anywhere in it. Data bytes are
    /// below `0x80`, so every byte at or above it is a status byte, and a
    /// Control Change's controller is the byte after its status.
    static func loadsARig(_ midi: [UInt8]) -> Bool {
        for (i, byte) in midi.enumerated() where byte >= 0x80 {
            switch byte & 0xF0 {
            case Generated.programChangeStatus:
                return true
            case Generated.controlChangeStatus:
                if i + 1 < midi.count, Generated.rigLoadControllers.contains(midi[i + 1]) {
                    return true
                }
            default:
                continue
            }
        }
        return false
    }

    /// Preselect bank `n` (1-based; CC47). A preselect alone loads nothing —
    /// the device waits for the slot load that commits it, which only the
    /// Navigator sends — so this is the one bank control that passes.
    public func bank(_ n: UInt16) async throws {
        try await send(control: .bankPreselect(UInt8(truncatingIfNeeded: max(n, 1) - 1)))
    }
    /// Tap the tempo (CC30).
    public func tapTempo() async throws { try await send(control: .tapTempo) }
    /// Open (`true`) or close (`false`) the tuner (CC31).
    public func tunerMode(_ open: Bool) async throws { try await send(control: .tunerMode(open)) }
    /// Morph button (CC80): `rise` climbs to the morph target, else falls back.
    public func morphButton(_ rise: Bool) async throws {
        try await send(control: .morphButton(rise))
    }
    /// Set the morph pedal position 0–127 (CC11).
    public func morphPedal(_ value: UInt8) async throws {
        try await send(control: .morphPedal(value))
    }
    /// Delay + Reverb Freeze (CC35).
    public func freeze(_ on: Bool) async throws { try await send(control: .freeze(on)) }
    /// Rotary speaker speed (CC33): `fast` or slow.
    public func rotaryFast(_ fast: Bool) async throws { try await send(control: .rotaryFast(fast)) }
    /// Delay Infinity (CC34).
    public func delayInfinity(_ on: Bool) async throws {
        try await send(control: .delayInfinity(on))
    }
    /// Toggle every module A–REV on/off (CC16).
    public func toggleAllModules() async throws { try await send(control: .toggleAllModules) }
    /// Press Effect Button `n` (I–IIII, clamped to 1...4; CC75–78).
    public func effectButton(_ n: UInt8) async throws { try await send(control: .effectButton(n)) }
    /// Set the wah pedal position 0–127 (CC1).
    public func wahPedal(_ value: UInt8) async throws { try await send(control: .wahPedal(value)) }
    /// Set the pitch pedal position 0–127 (CC4).
    public func pitchPedal(_ value: UInt8) async throws {
        try await send(control: .pitchPedal(value))
    }
    /// Set the volume pedal position 0–127 (CC7).
    public func volumePedal(_ value: UInt8) async throws {
        try await send(control: .volumePedal(value))
    }
    /// Set the panorama 0–127 (CC10).
    public func panorama(_ value: UInt8) async throws { try await send(control: .panorama(value)) }

    // MARK: - Wire

    /// Frame and write raw (pre-framing) MIDI bytes on the stream link.
    ///
    /// A failed write is the stream ending: it is handed to the supervisor,
    /// which closes both links and either reports the loss or starts the
    /// reconnect, exactly as a read error would.
    func write(_ bytes: [UInt8]) async throws {
        guard let stream, state.channels.stream == .open else { throw CommandError.disconnected }
        let epoch = self.epoch
        do {
            try await stream.session.writeAll(Midi3.frame(bytes))
        } catch {
            streamEnded(epoch: epoch)
            throw CommandError.disconnected
        }
    }
}
