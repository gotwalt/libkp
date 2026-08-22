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

/// Where the app is in its connect / retry cycle. Everything except
/// ``connected`` renders as a placeholder instead of the dashboard.
enum Phase: Equatable {
    /// Nothing started yet.
    case idle
    /// Broadcasting the discovery poll.
    case discovering
    /// A session is being opened to `host`.
    case connecting(host: String)
    /// A session is live; `name` is the device's advertised discovery name.
    case connected(host: String, name: String?)
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

/// The app's single source of truth: it owns the ``DeviceModel``, drives the
/// connect / retry cycle, and turns the two library streams into published
/// state the views bind to.
///
/// The high-rate lane is deliberately *not* published. Status events fold into
/// a private ``MeterFrame``; a 33 ms render tick decays the peak-hold markers,
/// recomputes the derived values and publishes the frame once. The coalesced
/// snapshot stream, which already fires at most once per ingested chunk, is
/// published as it arrives.
///
/// Every restart bumps an epoch counter. Tasks captured the epoch they were
/// started under and bail the moment it no longer matches, so a connection
/// that is still unwinding can never write into the state of its successor.
@MainActor
final class DeviceStore: ObservableObject {
    /// Where the app is in its connect / retry cycle.
    @Published private(set) var phase: Phase = .idle {
        didSet {
            guard phase != oldValue else { return }
            Log.conn("phase \(oldValue.label) -> \(phase.label)")
        }
    }
    /// The latest SLOW snapshot: rig, amp, cabinet, effects, tuner, output.
    @Published private(set) var state = DeviceState()
    /// The latest rendered FAST frame.
    @Published private(set) var meters = MeterFrame()
    /// The slot (1–5) this app selected last, for immediate feedback on a tap.
    /// The device does not report its rig index over the network, so this is
    /// app-local; it clears when the rig changes from anywhere else (front panel,
    /// another controller) so ``deviceSlot`` can take over.
    @Published private(set) var selectedSlot: Int? {
        didSet {
            guard selectedSlot != oldValue else { return }
            Log.ui("selectedSlot \(Log.opt(oldValue)) -> \(Log.opt(selectedSlot))")
        }
    }
    /// The bank the device is on, 1-based, or `nil` before the first position
    /// report. Read straight from the state tree: seeded at connect from the
    /// CBOR snapshot, then kept live by the Bank Select / Program Change pair
    /// the device sends on every rig change — including changes made at the
    /// front panel. Never stepped or inferred by this app.
    var bank: Int? { state.currentBank.map { Int($0) + 1 } }

    /// The slot the device has loaded, 1-based, from the same live report.
    var slot: Int? { state.currentRigSlot.map { Int($0) + 1 } }

    /// The device's flat rig index — its own numbering, and the address every
    /// navigation is computed in.
    var rigIndex: UInt16? { state.currentRigIndex }

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

    /// Which slot the rig navigator highlights: this app's optimistic last tap
    /// while it is fresh, then the device's own reported slot while it is still
    /// valid, then the name-matched inference.
    var highlightedSlot: Int? { selectedSlot ?? slot ?? deviceSlot }

    private var model: DeviceModel?
    private var tasks: [Task<Void, Never>] = []
    private var epoch = 0

    // The fast lane, accumulated off the published properties.
    private var frame = MeterFrame()
    private var recent: [Date] = []
    private var lastDecay = Date()
    private var lastPulse: (at: Date, on: Bool)?
    private var phaseHistory: [(at: Date, phase: UInt16)] = []
    /// When this app last navigated rigs, so a rig change arriving well after
    /// it can be recognized as externally driven.
    private var lastNavigation = Date.distantPast

    /// How long to wait before another attempt after a failure.
    private static let retryDelay: TimeInterval = 4
    /// Breathing room between one connection closing and the next opening — the
    /// device resets, or silently refuses to greet, a session opened immediately
    /// after the previous socket closed. The value is the shared protocol
    /// constant, so all implementations space connections the same way.
    private static let connectionSpacing = Session.connectionCooldown
    /// The render tick — about 30 frames a second.
    private static let tickInterval: TimeInterval = 0.033
    /// The trailing window the message rate is averaged over.
    private static let rateWindow: TimeInterval = 2
    /// How long discovery listens for replies.
    private static let discoveryWindow: TimeInterval = 3

    /// The handful of values the header shows that the initial rig sync does
    /// not cover. These are requests, so nothing on the device changes.
    private static let extraParams: [(page: UInt8, number: UInt8)] = [
        (Generated.ampPage, Generated.ampOnNumber),
        (Generated.ampPage, Generated.gainNumber),
        (Generated.pageRigSettings, Generated.tempoNumber),
        (Generated.pageRigSettings, Generated.rigVolumeNumber),
        (Generated.pageMorph, Generated.morphNumber),
        (Generated.systemPage, Generated.mainVolumeNumber),
        // Headphone + Monitor together are the physical Master Volume knob.
        (Generated.systemPage, Generated.headphoneVolumeNumber),
        (Generated.systemPage, Generated.monitorVolumeNumber),
    ]

    // MARK: - Lifecycle

    /// Start the connect cycle, once. Safe to call from every `onAppear`.
    func start() {
        guard phase == .idle else { return }
        Log.conn("start")
        restart()
    }

    /// Tear down whatever is running and begin a fresh connect cycle. Also the
    /// hook behind the Reconnect button and every Settings change.
    func restart() {
        epoch += 1
        let epoch = self.epoch
        Log.conn("restart (epoch \(epoch))")

        for task in tasks { task.cancel() }
        tasks.removeAll()
        // Hand the outgoing model to the connect loop so it can close it *before*
        // — and spaced from — the next connection, rather than racing a
        // fire-and-forget close against the new CBOR fetch.
        let closing = model
        model = nil

        frame = MeterFrame()
        meters = frame
        state = DeviceState()
        selectedSlot = nil
        recent.removeAll()
        lastDecay = Date()
        lastPulse = nil
        phaseHistory.removeAll()

        tasks.append(Task { await self.runConnectLoop(epoch: epoch, closing: closing) })
    }

    /// Resolve an address, connect, and attach the streams — retrying on a
    /// timer until it works or the epoch moves on.
    ///
    /// `closing` is the previous session, if any. The device refuses a new
    /// session that follows a close too closely, so it is closed and spaced
    /// before the first connection attempt — which starts with the CBOR
    /// snapshot.
    private func runConnectLoop(epoch: Int, closing: DeviceModel?) async {
        if let closing {
            await closing.close()
            try? await Task.sleep(
                nanoseconds: UInt64(DeviceStore.connectionSpacing * 1_000_000_000))
            guard epoch == self.epoch else { return }
        }
        while !Task.isCancelled && epoch == self.epoch {
            let defaults = UserDefaults.standard
            let mode =
                ConnectionMode(rawValue: defaults.string(forKey: SettingsKeys.mode) ?? "")
                ?? .automatic
            var host = (defaults.string(forKey: SettingsKeys.manualHost) ?? "").trimmed
            var name: String?

            if mode == .manual {
                // Retrying would poll an address that does not exist, so stop
                // and wait for the user to fill one in.
                guard !host.isEmpty else {
                    phase = .failed(message: "No device address set. Choose one in Settings.")
                    return
                }
            } else {
                phase = .discovering
                do {
                    let reply = try await Discovery.findFirst(
                        listenFor: DeviceStore.discoveryWindow)
                    guard epoch == self.epoch else { return }
                    guard let reply else {
                        phase = .failed(message: "No Profiler found on the network.")
                        await waitBeforeRetry()
                        continue
                    }
                    host = reply.host
                    name = reply.name
                    Log.conn("discovered \(reply.host) \(Log.opt(reply.name))")
                } catch {
                    guard epoch == self.epoch else { return }
                    phase = .failed(message: "Discovery failed: \(error)")
                    await waitBeforeRetry()
                    continue
                }
            }

            phase = .connecting(host: host)

            // Learn the device's real current bank/rig first, over the CBOR
            // channel, on its own short-lived connection. The device is fragile
            // under concurrent sockets, so this completes and closes *before* the
            // streaming session opens; a failure is non-fatal — the name-matched
            // fallback still runs — so it never blocks the connect.
            Log.conn("fetching CBOR state snapshot from \(host)")
            let snapshot = try? await StateSnapshot.fetch(host: host)
            guard epoch == self.epoch else { return }
            if let snapshot {
                Log.conn(
                    """
                    snapshot: bank \(Log.opt(snapshot.currentBank)) \
                    slot \(Log.opt(snapshot.currentRigSlot)) (both 0-based)
                    """)
            } else {
                Log.conn("snapshot unavailable — falling back to name matching")
            }
            // Let that socket settle before opening the streaming session — the
            // device will not greet one that follows too closely.
            if snapshot != nil {
                try? await Task.sleep(
                    nanoseconds: UInt64(DeviceStore.connectionSpacing * 1_000_000_000))
                guard epoch == self.epoch else { return }
            }

            do {
                let model = try await DeviceModel.connect(host: host)
                guard epoch == self.epoch else {
                    await model.close()
                    return
                }
                self.model = model
                phase = .connected(host: host, name: name)

                // `DeviceModel.connect` already ran the read-only rig sync.
                Log.conn("requesting \(DeviceStore.extraParams.count) extra header params")
                for (page, number) in DeviceStore.extraParams {
                    try? await model.requestParam(page: page, number: number)
                }
                guard epoch == self.epoch else { return }

                attachStreams(to: model, epoch: epoch)

                // Seed the current position from the snapshot, now that the store
                // is subscribed: fold the indices into the model's state (which
                // publishes a snapshot the views pick up) and show the real bank
                // on the stepper.
                if let snapshot {
                    Log.conn("seeding position from snapshot")
                    await model.setCurrentPosition(
                        bank: snapshot.currentBank, slot: snapshot.currentRigSlot)
                }
                return
            } catch {
                guard epoch == self.epoch else { return }
                Log.conn("connect to \(host) threw: \(error)")
                phase = .failed(message: "Could not connect to \(host): \(error)")
                await waitBeforeRetry()
            }
        }
    }

    /// Sleep out the retry delay.
    private func waitBeforeRetry() async {
        try? await Task.sleep(nanoseconds: UInt64(DeviceStore.retryDelay * 1_000_000_000))
    }

    /// Drain the granular event stream, the coalesced snapshot stream, and run
    /// the render tick — one task each, all inheriting the main actor.
    private func attachStreams(to model: DeviceModel, epoch: Int) {
        tasks.append(
            Task {
                for await event in await model.events() {
                    guard epoch == self.epoch else { return }
                    self.handle(event)
                }
                // The stream only finishes when the device hangs up.
                self.handleStreamEnd(epoch: epoch)
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

    /// The device closed the connection: say so, then start over.
    private func handleStreamEnd(epoch: Int) {
        guard epoch == self.epoch else { return }
        Log.conn("event stream finished — the device closed the connection")
        phase = .failed(message: "The device closed the connection. Retrying…")
        tasks.append(
            Task {
                await self.waitBeforeRetry()
                guard epoch == self.epoch else { return }
                self.restart()
            })
    }

    // MARK: - Commands

    /// Ask the device to flip an effect slot on or off, then read the state
    /// back. The device applies a `$01` write without echoing it, so the
    /// read-back is what updates the card — the UI always shows the device's
    /// actual state, never an assumed one.
    func toggleEffect(_ slot: String) {
        guard let model, let effect = state.effect(slot), !effect.isEmpty, let on = effect.on
        else { return }
        // Fire and forget: a failed write means the session is going down, and
        // the connect loop surfaces that on its own.
        Log.cmd("toggleEffect \(slot): \(on ? "on -> off" : "off -> on")")
        Task {
            do {
                try await model.setEffectEnabled(slot, !on)
                try await model.requestParam(page: effect.page, number: Params.effectParamState)
            } catch {
                Log.cmd("toggleEffect \(slot) failed: \(error)")
            }
        }
    }

    /// Load slot 1–5 (CC50–54) and remember the choice as the selected-slot hint.
    ///
    /// When a bank has been preselected, re-arm it (CC47) immediately before the
    /// rig-select, in one ordered burst — the device's preselect does not persist
    /// across the gap between a bank tap and a later slot tap, so a stale
    /// preselect would drop the load into whatever bank is still loaded. Sending
    /// the pair back-to-back is the sequence the device is documented to honour;
    /// re-arming the already-current bank is harmless.
    func selectSlot(_ slot: Int) {
        guard let model, (1...5).contains(slot) else { return }
        selectedSlot = slot
        lastNavigation = Date()
        Log.cmd("selectSlot \(slot) (CC\(49 + slot) press/release)")
        Task {
            do {
                try await model.selectRig(UInt8(slot))
                // The device does not volunteer the landing rig's strings after a CC
                // rig-select, so read them back to refresh the header/amp/cab/effects.
                try await model.refreshRig()
                Log.cmd("selectSlot \(slot) sent, rig read-back requested")
            } catch {
                Log.cmd("selectSlot \(slot) failed: \(error)")
            }
        }
    }

    /// Step `delta` rigs from where the device says it is, and load the result.
    ///
    /// Everything navigational goes through the flat rig index, because it is
    /// the only address that crosses a bank boundary: ±1 is the next or previous
    /// rig, ±``Params/bankSlots`` is the next or previous bank at the same slot.
    /// The move is a no-op until the device has reported a position, since there
    /// is nothing to step *from* — better to do nothing than to guess and jump
    /// somewhere arbitrary.
    ///
    /// The lower bound is 0; there is no upper bound, because how many rigs a
    /// device holds varies and nothing here knows it. Aiming past the end leaves
    /// the device where it is, and its position report says so.
    func stepRig(by delta: Int) {
        guard let model, let current = rigIndex else {
            Log.cmd("stepRig \(delta) ignored — no position reported yet")
            return
        }
        let target = UInt16(max(Int(current) + delta, 0))
        guard target != current else { return }
        let slots = UInt16(Params.bankSlots)
        selectedSlot = Int(target % slots) + 1
        lastNavigation = Date()
        Log.cmd(
            "stepRig \(delta > 0 ? "+" : "")\(delta): index \(current) -> \(target) "
                + "(bank \(target / slots + 1), slot \(target % slots + 1))")
        Task {
            do {
                try await model.selectRigIndex(target)
                // The device does not volunteer the landing rig's strings, so
                // read them back for the header, amp, cab and effects.
                try await model.refreshRig()
                Log.cmd("stepRig sent, rig read-back requested")
            } catch {
                Log.cmd("stepRig failed: \(error)")
            }
        }
    }

    /// Step one bank, keeping the slot — ``Params/bankSlots`` rigs at a time.
    func stepBank(forward: Bool) {
        stepRig(by: forward ? Params.bankSlots : -Params.bankSlots)
    }

    // MARK: - Ingest

    /// Fold one granular event into the fast lane. Slow changes arrive through
    /// the snapshot stream instead, so everything else is ignored here.
    private func handle(_ event: DeviceEvent) {
        if let line = Log.describe(event) { Log.evt(line) }
        switch event {
        case let .status(status):
            ingest(status)
        case let .beatPulse(on):
            lastPulse = (Date(), on)
        case .rigChanged:
            // A rig change well after our own navigation came from somewhere
            // else (front panel, another controller) — the hint is stale.
            let sinceNavigation = Date().timeIntervalSince(lastNavigation)
            Log.evt(String(format: "rigChanged %.2fs after our last navigation", sinceNavigation))
            if sinceNavigation > 3 {
                selectedSlot = nil
            }
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
