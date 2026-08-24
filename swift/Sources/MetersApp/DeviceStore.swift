import Foundation
import LibKP

// MARK: - Settings

/// How the app finds a Profiler: broadcast discovery, or a typed-in address.
enum ConnectionMode: String {
    /// Broadcast the UDP discovery poll and take the first device that answers.
    case automatic
    /// Connect straight to the address stored in ``SettingsKeys/manualHost``.
    case manual
}

/// The `UserDefaults` keys the app and its Settings scene share.
enum SettingsKeys {
    /// A ``ConnectionMode`` raw value.
    static let mode = "connectionMode"
    /// The IPv4 address used in ``ConnectionMode/manual``.
    static let manualHost = "manualHost"
    /// Whether the level list shows all eleven raw meter fields.
    static let showAllMeters = "showAllMeters"
}

// MARK: - Connection phase

/// Where the app is in its connect cycle. Everything except ``connected``
/// renders as a placeholder instead of the dashboard.
///
/// Once a session is up the phase follows the model's own
/// ``LibKP/Connection``: the app opts into the library's reconnect policy, so
/// a lost stream is ``reconnecting(host:attempt:)`` until the model has it
/// back, and the dashboard returns on its own.
enum Phase: Equatable {
    /// Nothing started yet.
    case idle
    /// Broadcasting the discovery poll.
    case discovering
    /// A session is being opened to `host`.
    case connecting(host: String)
    /// A session is live; `name` is the device's advertised discovery name.
    case connected(host: String, name: String?)
    /// The device closed the stream and the model is dialling `host` again;
    /// `attempt` counts from 1.
    case reconnecting(host: String, attempt: UInt32)
    /// The last attempt failed; `message` is shown to the user.
    case failed(message: String)
}

// MARK: - Tuner verdict

/// The tuner readout distilled from the strobe phase drift rate.
enum StrobeVerdict: Equatable {
    /// The tuner block is not producing data.
    case idle
    /// Active, but not enough phase samples yet to call it.
    case measuring
    /// The strobe is (near enough) stationary.
    case inTune
    /// The phase is falling — the note is above pitch.
    case sharp
    /// The phase is rising — the note is below pitch.
    case flat
}

// MARK: - Meter frame

/// One rendered frame of the FAST lane: the eleven meter values plus everything
/// derived from their history — peak-hold markers, the observed range, the
/// message rate, the strobe position and verdict, and the beat pulse.
///
/// The store accumulates into a private copy and publishes it once per render
/// tick, so a 300 Hz stream never drives 300 view updates a second.
struct MeterFrame: Equatable {
    /// The eleven raw 14-bit values, in NRPN order.
    var values = [UInt16](repeating: 0, count: Generated.meterCount)
    /// Peak-hold markers, in raw units, decayed on every tick.
    var peaks = [Double](repeating: 0, count: Generated.meterCount)
    /// The lowest value seen per field since the last connect.
    var mins = [UInt16](repeating: .max, count: Generated.meterCount)
    /// The highest value seen per field since the last connect.
    var maxs = [UInt16](repeating: 0, count: Generated.meterCount)
    /// How many status frames have arrived since the last connect.
    var frames: UInt64 = 0
    /// Status frames per second over the trailing window.
    var rate: Double = 0
    /// True while the tuner block is producing data.
    var strobeActive = false
    /// The strobe phase as a 0…1 position along the track.
    var strobePosition: Double = 0
    /// The in-tune / sharp / flat call derived from the phase drift.
    var verdict: StrobeVerdict = .idle
    /// True during the brief window after an "on" beat pulse.
    var beatActive = false

    /// The 14-bit full-scale value, as a `Double` for the bar math.
    static let fullScale = Double(Generated.fullScale)

    /// A field's current value as a 0…1 fraction of full scale.
    func fraction(_ index: Int) -> Double { Double(values[index]) / MeterFrame.fullScale }

    /// A field's peak-hold marker as a 0…1 fraction of full scale.
    func peakFraction(_ index: Int) -> Double { peaks[index] / MeterFrame.fullScale }

    /// The low/high range observed for a field, or `(0, 0)` if nothing has been
    /// seen yet (the seeded minimum is still above the seeded maximum).
    func range(_ index: Int) -> (low: UInt16, high: UInt16) {
        mins[index] <= maxs[index] ? (mins[index], maxs[index]) : (0, 0)
    }
}

// MARK: - The store

/// The app's single source of truth: it owns the ``DeviceModel``, finds the
/// device and opens the session, and turns the two library streams into
/// published state the views bind to.
///
/// Everything that used to be this store's own — the retry loop, the rig
/// navigator's serializer and its pending aim — is the model's now. The store
/// connects with ``LibKP/ReconnectPolicy/stream`` set, so a dropped stream is
/// dialled again by the library, and forwards every navigation tap to the
/// model's Navigator, which is the only thing that loads a rig. What the
/// buttons highlight is read straight back out of the snapshot.
///
/// The high-rate lane is deliberately *not* published. Status events fold into
/// a private ``MeterFrame``; a 33 ms render tick decays the peak-hold markers,
/// recomputes the derived values and publishes the frame once. The coalesced
/// snapshot stream, which already fires at most once per ingested chunk, is
/// published as it arrives.
///
/// Every restart bumps an epoch counter. Tasks captured the epoch they were
/// started under and bail the moment it no longer matches, so a session that
/// is still unwinding can never write into the state of its successor.
@MainActor
final class DeviceStore: ObservableObject {
    /// Where the app is in its connect cycle.
    @Published private(set) var phase: Phase = .idle {
        didSet {
            guard phase != oldValue else { return }
            Log.conn("phase \(oldValue.label) -> \(phase.label)")
        }
    }
    /// The latest SLOW snapshot: rig, amp, cabinet, effects, tuner, output,
    /// connection, and the Navigator's outstanding aim.
    @Published private(set) var state = DeviceState() {
        didSet {
            guard state.navigation != oldValue.navigation else { return }
            Log.ui(
                "navigation aim \(Log.opt(state.navigation.aim)) "
                    + (state.navigation.inFlight ? "in flight" : "idle"))
        }
    }
    /// The latest rendered FAST frame.
    @Published private(set) var meters = MeterFrame()

    /// The bank the device is on, 1-based, or `nil` before the first position
    /// report. Read straight from the state tree: asked for at connect by the
    /// sync burst and kept live by the `$06` the device pushes on every rig
    /// change — including changes made at the front panel. Never stepped or
    /// inferred by this app.
    var bank: Int? { state.currentBank.map { Int($0) + 1 } }

    /// The slot the device has loaded, 1-based, from the same live report.
    var slot: Int? { state.currentRigSlot.map { Int($0) + 1 } }

    /// The device's flat rig index — its own numbering, and the address every
    /// navigation is computed in.
    var rigIndex: UInt16? { state.currentRigIndex }

    /// The slot this app is aiming at, 1-based, while the model's Navigator
    /// has an aim the device has not yet confirmed — the optimistic highlight
    /// for a tap, gone the moment the device reports it landed (or the
    /// Navigator gives up on it).
    var aimedSlot: Int? { state.navigation.aim.map { Int($0 % UInt16(Params.bankSlots)) + 1 } }

    /// The bank slot the device has loaded, *inferred* by matching the loaded
    /// rig name against the bank preview's five rig names. This is the live
    /// fallback until the device's own ``slot`` is known: the device pushes a
    /// fresh preview on every bank change, so the match follows navigation that
    /// the boot snapshot cannot. `nil` when the current rig name is not one the
    /// bank preview carries (e.g. two slots share a name).
    var deviceSlot: Int? {
        guard let name = state.rig.name?.trimmed.nonEmpty else { return nil }
        for (index, slot) in state.bank.slots.enumerated()
        where slot.rigName?.trimmed.nonEmpty == name {
            return index + 1
        }
        return nil
    }

    /// Which slot the rig navigator highlights: the Navigator's outstanding
    /// aim until the device reports where it actually landed, then the
    /// device's own slot, then the name-matched inference.
    var highlightedSlot: Int? { aimedSlot ?? slot ?? deviceSlot }

    private var model: DeviceModel?
    private var tasks: [Task<Void, Never>] = []
    private var epoch = 0
    /// The host of the current session, for the phase the reconnect shows.
    private var host: String?
    /// The UDP discovery port, taken on first use and held for as long as this
    /// store lives — deliberately not released between connect attempts. See
    /// ``heldDiscoveryPort()``.
    private var discoveryPort: DiscoveryPort?

    // The fast lane, accumulated off the published properties.
    private var frame = MeterFrame()
    private var recent: [Date] = []
    private var lastDecay = Date()
    private var lastPulse: (at: Date, on: Bool)?
    private var phaseHistory: [(at: Date, phase: UInt16)] = []

    /// The render tick — about 30 frames a second.
    private static let tickInterval: TimeInterval = 0.033
    /// The trailing window the message rate is averaged over.
    private static let rateWindow: TimeInterval = 2
    /// How long discovery listens for replies.
    private static let discoveryWindow: TimeInterval = 3

    /// How the model is connected: the library's own stream backoff, so a
    /// dropped session is dialled again without this app keeping a loop.
    private static let options = ConnectOptions(
        reconnect: ReconnectPolicy(stream: .defaultStream()))

    // MARK: - Lifecycle

    /// Start the connect cycle, once. Safe to call from every `onAppear`.
    func start() {
        guard phase == .idle else { return }
        Log.conn("start")
        restart()
    }

    /// Close whatever is running and connect afresh. Also the hook behind the
    /// Reconnect button and every Settings change.
    func restart() {
        epoch += 1
        let epoch = self.epoch
        Log.conn("restart (epoch \(epoch))")

        for task in tasks { task.cancel() }
        tasks.removeAll()
        // Hand the outgoing model to the connect task so it is closed *before*
        // the next connection is dialled; the library's connection ledger then
        // spaces the dial from that close.
        let closing = model
        model = nil
        host = nil

        frame = MeterFrame()
        meters = frame
        state = DeviceState()
        recent.removeAll()
        lastDecay = Date()
        lastPulse = nil
        phaseHistory.removeAll()

        tasks.append(Task { await self.connect(epoch: epoch, closing: closing) })
    }

    /// Resolve an address, connect once, and attach the streams.
    ///
    /// `closing` is the previous model, if any. The device refuses a new
    /// session that follows a close too closely; the library's connection
    /// ledger waits that out inside the next connect, so the close only has
    /// to come first. A failure here stays on screen with a Try Again button:
    /// once a session is up, losing it is the model's reconnect policy's to
    /// handle, not this app's.
    private func connect(epoch: Int, closing: DeviceModel?) async {
        if let closing {
            await closing.close()
        }
        guard epoch == self.epoch else { return }
        let defaults = UserDefaults.standard
        let mode =
            ConnectionMode(rawValue: defaults.string(forKey: SettingsKeys.mode) ?? "")
            ?? .automatic
        var host = (defaults.string(forKey: SettingsKeys.manualHost) ?? "").trimmed
        var name: String?

        if mode == .manual {
            guard !host.isEmpty else {
                phase = .failed(message: "No device address set. Choose one in Settings.")
                return
            }
        } else {
            phase = .discovering
            do {
                var options = DiscoveryOptions()
                options.listenFor = DeviceStore.discoveryWindow
                let reply = try await heldDiscoveryPort().poll(options).first
                guard epoch == self.epoch else { return }
                guard let reply else {
                    phase = .failed(message: "No Profiler found on the network.")
                    return
                }
                host = reply.host
                name = reply.name
                Log.conn("discovered \(reply.host) \(Log.opt(reply.name))")
            } catch let error as DiscoverError {
                guard epoch == self.epoch else { return }
                // `DiscoverError` states the remedy itself — in particular
                // ``DiscoverError/portUnavailable(port:)`` names the conflict
                // rather than leaving it to look like an absent device.
                Log.conn("discovery failed: \(error)")
                phase = .failed(message: "\(error)")
                return
            } catch {
                guard epoch == self.epoch else { return }
                phase = .failed(message: "Discovery failed: \(error)")
                return
            }
        }

        phase = .connecting(host: host)
        self.host = host

        do {
            let model = try await DeviceModel.connect(host: host, options: DeviceStore.options)
            guard epoch == self.epoch else {
                await model.close()
                return
            }
            self.model = model
            phase = .connected(host: host, name: name)
            // `DeviceModel.connect` already queued the read-only sync burst —
            // the position included — and is opening the control link that
            // carries the morph in the background. The snapshot stream yields
            // the current state first, so nothing that landed before this
            // subscription is missed.
            attachStreams(to: model, name: name, epoch: epoch)
        } catch {
            guard epoch == self.epoch else { return }
            Log.conn("connect to \(host) threw: \(error)")
            phase = .failed(message: "Could not connect to \(host): \(error)")
        }
    }

    /// The discovery port, acquired once and then held.
    ///
    /// LibKP requires sole ownership of UDP 5727: the device answers a poll only
    /// on that port, and the kernel gives each reply to exactly one bound socket,
    /// so a second listener on the machine takes replies instead of duplicating
    /// them. Holding the port for the whole session means no other process can
    /// take it between a dropped connection and the reconnect, and acquiring it
    /// up front turns a conflict into a stated error rather than a device that
    /// appears to be missing.
    ///
    /// Only ``ConnectionMode/automatic`` needs it. Manual mode connects straight
    /// to a known address over TCP and never polls, so it does not claim the port.
    private func heldDiscoveryPort() throws -> DiscoveryPort {
        if let discoveryPort { return discoveryPort }
        let port = try DiscoveryPort()
        Log.conn("acquired UDP \(port.port) exclusively for this session")
        discoveryPort = port
        return port
    }

    /// Drain the granular event stream, the coalesced snapshot stream, and run
    /// the render tick — one task each, all inheriting the main actor.
    private func attachStreams(to model: DeviceModel, name: String?, epoch: Int) {
        tasks.append(
            Task {
                for await event in await model.events() {
                    guard epoch == self.epoch else { return }
                    self.handle(event, name: name)
                }
            })

        tasks.append(
            Task {
                for await snapshot in await model.snapshots() {
                    guard epoch == self.epoch else { return }
                    self.logSnapshotDelta(from: self.state, to: snapshot)
                    self.state = snapshot
                }
            })

        tasks.append(
            Task {
                while !Task.isCancelled && epoch == self.epoch {
                    self.tick()
                    try? await Task.sleep(
                        nanoseconds: UInt64(DeviceStore.tickInterval * 1_000_000_000))
                }
            })
    }

    /// Map the model's connection onto this app's phase. The model dials again
    /// on its own after a loss, so ``Phase/reconnecting(host:attempt:)`` is a
    /// wait, not a failure; `.disconnected` only ever follows this app's own
    /// `close()`, which the next phase already covers.
    private func apply(_ connection: Connection, name: String?) {
        guard let host else { return }
        switch connection {
        case .connected, .degraded:
            phase = .connected(host: host, name: name)
        case let .reconnecting(attempt):
            Log.conn("the device closed the connection; reconnecting (attempt \(attempt))")
            phase = .reconnecting(host: host, attempt: attempt)
        case .disconnected:
            break
        }
    }

    // MARK: - Commands

    /// Whether the rig is morphed — its position is past halfway.
    ///
    /// `nil` while the position is unavailable: the model's control link is
    /// down (``LibKP/Connection/degraded``), or it has not yet delivered the
    /// dump that carries the position. The streaming session never reports it.
    var isMorphed: Bool? {
        guard state.connection != .degraded else { return nil }
        return state.morph.map { $0 > Generated.fullScale / 2 }
    }

    /// Morph to the rig's morphed sound, or back to its base.
    ///
    /// Sent as the morph pedal (CC 11), which sets an absolute position, rather
    /// than the morph button (CC 80), which ramps over about two seconds and
    /// alternates direction per press. A toggle wants a destination, not a
    /// direction, and the pedal is the control that names one.
    ///
    /// The device does not echo the write; the position comes back on the
    /// model's control link, so the readout follows the device rather than
    /// this call.
    func setMorphed(_ morphed: Bool) {
        guard let model else { return }
        Log.cmd("setMorphed \(morphed) (CC11 -> \(morphed ? 127 : 0))")
        Task {
            do {
                try await model.morphPedal(morphed ? 127 : 0)
            } catch {
                Log.cmd("setMorphed failed: \(error)")
            }
        }
    }

    /// Ask the device to flip an effect slot on or off, then read the state
    /// back. The device applies a `$01` write without echoing it, so the
    /// read-back is what updates the card — the UI always shows the device's
    /// actual state, never an assumed one.
    func toggleEffect(_ slot: String) {
        guard let model, let effect = state.effect(slot), !effect.isEmpty, let on = effect.on
        else { return }
        Log.cmd("toggleEffect \(slot): \(on ? "on -> off" : "off -> on")")
        Task {
            do {
                try await model.setEffectEnabled(slot, !on)
                // The reply folds into the snapshot on its way here; the value
                // is logged so a card that did not flip can be told apart from
                // a request that timed out.
                let value = try await model.requestParam(
                    page: effect.page, number: Params.effectParamState)
                Log.cmd("toggleEffect \(slot): device reports \(value == 0 ? "off" : "on")")
            } catch {
                Log.cmd("toggleEffect \(slot) failed: \(error)")
            }
        }
    }

    /// Load slot 1–5 of the bank this app is aiming at — the model's
    /// outstanding aim, else the bank the device is on. The Navigator
    /// re-arms the bank preselect with every slot load, so a slot tapped
    /// right after Bank Up lands in the new bank, not the one the device has
    /// yet to leave.
    func selectSlot(_ slot: Int) {
        guard let model, (1...Params.bankSlots).contains(slot) else { return }
        Log.cmd("selectSlot \(slot) from \(Log.opt(state.aimedRigIndex))")
        Task { await model.selectSlot(UInt8(slot)) }
    }

    /// Step `delta` rigs from where this app is aiming, and load the result.
    ///
    /// Everything navigational goes through the flat rig index, because it is
    /// the only address that crosses a bank boundary: ±1 is the next or previous
    /// rig, ±``Params/bankSlots`` is the next or previous bank at the same slot.
    /// The Navigator ignores the tap until the device has reported a position,
    /// since there is nothing to step *from*, and floors the result at 0; there
    /// is no upper bound, because how many rigs a device holds varies and
    /// nothing here knows it.
    func stepRig(by delta: Int) {
        guard let model else { return }
        Log.cmd("stepRig \(delta > 0 ? "+" : "")\(delta) from \(Log.opt(state.aimedRigIndex))")
        Task { await model.stepRig(by: delta) }
    }

    /// Step one bank, keeping the slot — ``Params/bankSlots`` rigs at a time.
    func stepBank(forward: Bool) {
        guard let model else { return }
        Log.cmd("stepBank \(forward ? "up" : "down") from \(Log.opt(state.aimedRigIndex))")
        Task { await model.stepBank(forward: forward) }
    }

    // MARK: - Ingest

    /// Fold one granular event into the fast lane, and the connection ones
    /// into the phase. Slow changes arrive through the snapshot stream
    /// instead, so everything else is ignored here.
    private func handle(_ event: DeviceEvent, name: String?) {
        if let line = Log.describe(event) { Log.evt(line) }
        switch event {
        case let .status(status):
            ingest(status)
        case let .beatPulse(on):
            lastPulse = (Date(), on)
        case let .connectionChanged(connection):
            apply(connection, name: name)
        case .rigChanged:
            // With an aim outstanding this is our own move landing; without
            // one it came from somewhere else (front panel, another
            // controller).
            Log.evt("rigChanged (aim \(Log.opt(state.navigation.aim)))")
        default:
            break
        }
    }

    /// Log the snapshot fields the rig navigator depends on, whenever they move:
    /// the loaded rig, the bank preview's five slot names, and the device's own
    /// reported position. An entry here with no preceding `cmd` line is a change
    /// this app did not ask for.
    private func logSnapshotDelta(from old: DeviceState, to new: DeviceState) {
        if old.rig.name != new.rig.name || old.rig.author != new.rig.author {
            Log.evt(
                "rig \"\(Log.opt(new.rig.name?.trimmed))\" by \(Log.opt(new.rig.author?.trimmed))")
        }
        let previous = old.bank.slots.map { $0.rigName?.trimmed ?? "" }
        let current = new.bank.slots.map { $0.rigName?.trimmed ?? "" }
        if previous != current {
            Log.evt("bank preview [\(current.joined(separator: " | "))]")
        }
        if old.currentBank != new.currentBank || old.currentRigSlot != new.currentRigSlot {
            Log.evt(
                "device position bank \(Log.opt(new.currentBank)) "
                    + "slot \(Log.opt(new.currentRigSlot))")
        }
    }

    /// Accumulate one meter frame: values, peak-hold, observed range, the
    /// message-rate window and the strobe phase history.
    private func ingest(_ status: RealtimeStatus) {
        frame.frames += 1
        recent.append(Date())
        for (i, value) in status.raw.enumerated() {
            frame.values[i] = value
            frame.peaks[i] = max(frame.peaks[i], Double(value))
            frame.mins[i] = min(frame.mins[i], value)
            frame.maxs[i] = max(frame.maxs[i], value)
        }
        frame.strobeActive = status.strobeActive
        if status.strobeActive {
            phaseHistory.append((Date(), status.strobePhase))
        } else {
            phaseHistory.removeAll(keepingCapacity: true)
        }
    }

    // MARK: - Render tick

    /// Decay the peaks, refresh the derived values, and publish the frame.
    private func tick() {
        let now = Date()

        // Frame-rate independent peak decay, never below the current value.
        let dt = min(now.timeIntervalSince(lastDecay), 0.5)
        lastDecay = now
        let drop = MeterFrame.fullScale * 0.8 * dt  // full-scale peak falls in ~1.25 s
        for i in frame.peaks.indices {
            frame.peaks[i] = max(frame.peaks[i] - drop, Double(frame.values[i]))
        }

        let cutoff = now.addingTimeInterval(-DeviceStore.rateWindow)
        recent.removeAll { $0 < cutoff }
        frame.rate = Double(recent.count) / DeviceStore.rateWindow

        frame.strobePosition =
            Double(frame.values[Generated.strobePhaseIndex]) / MeterFrame.fullScale

        switch (frame.strobeActive, strobeRate()) {
        case (false, _):
            frame.verdict = .idle
        case (true, .none):
            frame.verdict = .measuring
        case let (true, .some(rate)) where abs(rate) < 600:
            frame.verdict = .inTune
        case let (true, .some(rate)) where rate < 0:
            frame.verdict = .sharp
        case (true, .some):
            frame.verdict = .flat
        }

        if let pulse = lastPulse {
            frame.beatActive = pulse.on && now.timeIntervalSince(pulse.at) < 0.15
        } else {
            frame.beatActive = false
        }

        meters = frame
    }

    /// Wrap-aware strobe drift rate in counts/sec over the last ~0.4 s.
    /// Positive = phase rising (flat), negative = falling (sharp).
    private func strobeRate() -> Double? {
        let cutoff = Date().addingTimeInterval(-0.4)
        phaseHistory.removeAll { $0.at < cutoff }
        guard phaseHistory.count >= 3,
            let first = phaseHistory.first,
            let last = phaseHistory.last
        else { return nil }
        var total = 0.0
        for i in 1..<phaseHistory.count {
            let a = Int(phaseHistory[i - 1].phase)
            let b = Int(phaseHistory[i].phase)
            // Shortest wrapped step from a to b on a 0..16384 circle.
            let delta = euclideanRemainder(b - a + 8192, 16384) - 8192
            total += Double(delta)
        }
        let dt = last.at.timeIntervalSince(first.at)
        return dt > 0.05 ? total / dt : nil
    }
}

/// Euclidean remainder, so a negative dividend still wraps forward.
private func euclideanRemainder(_ lhs: Int, _ rhs: Int) -> Int {
    let remainder = lhs % rhs
    return remainder < 0 ? remainder + rhs : remainder
}
