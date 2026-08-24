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
    /// The slot (1–5) this app selected last, for immediate feedback on a tap —
    /// the device's own report follows a beat later. App-local; it clears when
    /// the rig changes from anywhere else (front panel, another controller) so
    /// the device's report takes over.
    @Published private(set) var selectedSlot: Int? {
        didSet {
            guard selectedSlot != oldValue else { return }
            Log.ui("selectedSlot \(Log.opt(oldValue)) -> \(Log.opt(selectedSlot))")
        }
    }
    /// The bank the device is on, 1-based, or `nil` before the first position
    /// report. Read straight from the state tree: asked for at connect
    /// (``LibKP/DeviceModel/refreshPosition()``) and kept live by the `$06` the
    /// device pushes on every rig change — including changes made at the front
    /// panel. Never stepped or inferred by this app.
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
    /// until the device reports where it actually landed, then the device's own
    /// slot, then the name-matched inference.
    var highlightedSlot: Int? { selectedSlot ?? slot ?? deviceSlot }

    /// The flat rig index navigation steps *from*: this app's own un-confirmed
    /// aim while it is fresh, otherwise the device's reported position.
    ///
    /// The device takes a moment to report a move, so two taps inside that
    /// window would both step from the same stale index and the second would
    /// re-send the first one's target — a second press of Bank Up that does
    /// nothing. Stepping from the aim instead makes them compose.
    ///
    /// The aim expires rather than being trusted indefinitely: aim past the last
    /// rig and the device stays put and reports nothing, so there is no
    /// confirmation to wait for. After ``pendingWindow`` the device's own
    /// position is the truth again.
    private var navigationIndex: UInt16? {
        if let pending, Date().timeIntervalSince(pending.at) < DeviceStore.pendingWindow {
            return pending.index
        }
        return rigIndex
    }

    private var model: DeviceModel?
    private var tasks: [Task<Void, Never>] = []
    private var epoch = 0
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
    /// When this app last navigated rigs, so a rig change arriving well after
    /// it can be recognized as externally driven.
    private var lastNavigation = Date.distantPast
    /// Where this app last aimed, and when. Cleared by the device's next
    /// position report — see ``navigationIndex``.
    private var pending: (index: UInt16, at: Date)?
    /// The move currently on the wire, if any.
    private var navigationTask: Task<Void, Never>?
    /// Whether a move is on the wire and not yet settled. While it is, taps only
    /// move the aim — see ``navigate(to:why:)``.
    private var moveInFlight = false
    /// The last aim actually sent, so a settled aim is not sent twice.
    private var sentIndex: UInt16?

    /// How long a rig load is left alone before the read-back follows it. The
    /// device reports its new position about 200 ms in; this is comfortably past
    /// that. See ``navigate(to:why:)``.
    private static let loadSettle: TimeInterval = 0.5
    /// How long the read-back's replies are left to drain before another move
    /// may be sent.
    private static let readBackSettle: TimeInterval = 0.5

    /// How long an un-confirmed aim stands in for the device's own position.
    /// Comfortably longer than the ~150 ms the device takes to report a move,
    /// short enough that an aim past the last rig — which is never confirmed,
    /// because nothing moved — stops mattering quickly.
    private static let pendingWindow: TimeInterval = 1.5
    /// How long to wait before another attempt after a failure.
    private static let retryDelay: TimeInterval = 4
    /// The render tick — about 30 frames a second.
    private static let tickInterval: TimeInterval = 0.033
    /// The trailing window the message rate is averaged over.
    private static let rateWindow: TimeInterval = 2
    /// How long discovery listens for replies.
    private static let discoveryWindow: TimeInterval = 3

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
        navigationTask?.cancel()
        navigationTask = nil
        moveInFlight = false
        sentIndex = nil
        pending = nil
        selectedSlot = nil
        // Hand the outgoing model to the connect loop so it is closed *before*
        // the next connection is dialled; the library's connection ledger then
        // spaces the dial from that close.
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
    /// `closing` is the previous model, if any. The device refuses a new
    /// session that follows a close too closely; the library's connection
    /// ledger waits that out inside the next connect, so the close only has to
    /// come first.
    private func runConnectLoop(epoch: Int, closing: DeviceModel?) async {
        if let closing {
            await closing.close()
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
                    var options = DiscoveryOptions()
                    options.listenFor = DeviceStore.discoveryWindow
                    let reply = try await heldDiscoveryPort().poll(options).first
                    guard epoch == self.epoch else { return }
                    guard let reply else {
                        phase = .failed(message: "No Profiler found on the network.")
                        await waitBeforeRetry()
                        continue
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
                    await waitBeforeRetry()
                    continue
                } catch {
                    guard epoch == self.epoch else { return }
                    phase = .failed(message: "Discovery failed: \(error)")
                    await waitBeforeRetry()
                    continue
                }
            }

            phase = .connecting(host: host)

            do {
                let model = try await DeviceModel.connect(host: host)
                guard epoch == self.epoch else {
                    await model.close()
                    return
                }
                self.model = model
                phase = .connected(host: host, name: name)

                // `DeviceModel.connect` already queued the read-only sync burst
                // — every header value included — and is opening the control
                // link that carries the morph position in the background.
                attachStreams(to: model, epoch: epoch)

                // The burst asked the device where it is; the `$06` replies
                // land on the stream we just subscribed to. Ask again, so a
                // reply that beat the subscription is not the only one.
                try? await model.refreshPosition()
                return
            } catch {
                guard epoch == self.epoch else { return }
                Log.conn("connect to \(host) threw: \(error)")
                phase = .failed(message: "Could not connect to \(host): \(error)")
                await waitBeforeRetry()
            }
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
                    // The model does not dial again on its own (this app keeps
                    // its own retry loop for now), so this is the device
                    // hanging up; the stream itself finishes only on close.
                    if case .disconnected = event { self.handleStreamEnd(epoch: epoch) }
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

    /// The device closed the connection: say so, then start over.
    private func handleStreamEnd(epoch: Int) {
        guard epoch == self.epoch else { return }
        Log.conn("the device closed the connection")
        phase = .failed(message: "The device closed the connection. Retrying…")
        tasks.append(
            Task {
                await self.waitBeforeRetry()
                guard epoch == self.epoch else { return }
                self.restart()
            })
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
        // Fire and forget: a failed write means the session is going down, and
        // the connect loop surfaces that on its own.
        Log.cmd("toggleEffect \(slot): \(on ? "on -> off" : "off -> on")")
        Task {
            do {
                try await model.setEffectEnabled(slot, !on)
                _ = try await model.requestParam(page: effect.page, number: Params.effectParamState)
            } catch {
                Log.cmd("toggleEffect \(slot) failed: \(error)")
            }
        }
    }

    /// Load slot 1–5 of the bank the device is on.
    ///
    /// Addressed as a flat index like every other move, so the bank preselect
    /// (CC47) is re-armed immediately before the slot load: the device's
    /// preselect does not persist across the gap between a bank tap and a later
    /// slot tap, so a bare slot load would drop into whatever bank is still
    /// loaded. Re-arming the already-current bank is harmless. Before the first
    /// position report there is no bank to name, so the slot load goes on its
    /// own.
    func selectSlot(_ slot: Int) {
        guard let model, (1...Params.bankSlots).contains(slot) else { return }
        // The bank this app is aiming at, which is the device's own only once a
        // bank step has settled. Reading `state.currentBank` here instead would
        // address the slot to the bank the device has *left*, silently undoing a
        // Bank Up tapped a moment earlier — the stale-index bug that
        // ``navigationIndex`` exists to prevent.
        guard let bank = navigationIndex.map({ $0 / UInt16(Params.bankSlots) }) else {
            // No position yet, so there is no bank to name and the slot load
            // goes bare. Still takes the in-flight gate: a rig load that lands
            // on top of another is what kills the device.
            guard !moveInFlight else {
                Log.cmd("selectSlot \(slot) dropped — a move is in flight and there is no bank yet")
                return
            }
            selectedSlot = slot
            lastNavigation = Date()
            moveInFlight = true
            Log.cmd("selectSlot \(slot) (CC\(49 + slot)) — no bank reported yet, sending bare")
            navigationTask = Task { [weak self] in
                defer {
                    self?.moveInFlight = false
                    self?.pump()
                }
                do {
                    try await model.selectRig(UInt8(slot))
                    await self?.sleep(DeviceStore.loadSettle)
                    try await model.refreshRig()
                    await self?.sleep(DeviceStore.readBackSettle)
                } catch {
                    Log.cmd("selectSlot \(slot) failed: \(error)")
                }
            }
            return
        }
        navigate(to: bank * UInt16(Params.bankSlots) + UInt16(slot - 1), why: "selectSlot \(slot)")
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
        guard let current = navigationIndex else {
            Log.cmd("stepRig \(delta) ignored — no position reported yet")
            return
        }
        let target = UInt16(max(Int(current) + delta, 0))
        guard target != current else { return }
        navigate(to: target, why: "stepRig \(delta > 0 ? "+" : "")\(delta) from \(current)")
    }

    /// Aim at a flat rig index, and load it — one move at a time, never
    /// overlapping the last one.
    ///
    /// The aim lands immediately, so the buttons and the readout answer every
    /// tap; only the *sending* is rationed. A tap while the device is idle goes
    /// out at once. A tap while a move is still in flight just moves the aim,
    /// and ``pump()`` sends wherever the aim ended up once the previous move has
    /// settled — so a burst of taps costs two rig loads however long it is.
    ///
    /// This is not tidiness. A rig load makes the device replay its whole
    /// parameter tree, and each one here is followed by a read-back of a couple
    /// of dozen requests. Two of those issued 8 ms apart is enough to kill the
    /// device: it answers the first normally, then closes the session about
    /// twenty seconds later and stops accepting connections until it is power
    /// cycled. The fuse is delayed, so nothing about the reply to a burst says
    /// it did any harm.
    private func navigate(to target: UInt16, why: String) {
        guard model != nil else { return }
        let slots = UInt16(Params.bankSlots)
        let now = Date()
        selectedSlot = Int(target % slots) + 1
        lastNavigation = now
        pending = (target, now)
        Log.cmd(
            "\(why): aim index \(target) (bank \(target / slots + 1), slot \(target % slots + 1))"
                + (moveInFlight ? " — holding, a move is in flight" : ""))
        pump()
    }

    /// Send the current aim, if the device is idle and is not already there.
    private func pump() {
        guard !moveInFlight, model != nil, let pending else { return }
        // Nothing to send if the device is already there, or if this same aim
        // has been sent and the device did not move — which is what aiming past
        // the last rig looks like, and re-sending it would loop forever.
        guard pending.index != rigIndex, pending.index != sentIndex else { return }
        moveInFlight = true
        sentIndex = pending.index
        navigationTask = Task { [weak self] in
            await self?.performMove(pending.index)
        }
    }

    /// One move, start to finish: the absolute bank preselect plus slot load,
    /// a pause for the device to land it and say so, then the read-back that
    /// refreshes the header, amp, cabinet and effects — and another pause before
    /// anything else is allowed on the wire.
    ///
    /// The pauses are what keep the load and the read-back from arriving on top
    /// of each other. They are generous on purpose: the failure they avoid costs
    /// a power cycle, and the cost of being wrong the other way is a burst of
    /// taps taking an extra second to land.
    private func performMove(_ target: UInt16) async {
        defer {
            moveInFlight = false
            pump()
        }
        guard let model else { return }
        do {
            try await model.selectRigIndex(target)
            Log.cmd("navigate to \(target) sent")
        } catch {
            Log.cmd("navigate to \(target) failed: \(error)")
            return
        }
        await sleep(DeviceStore.loadSettle)
        guard !Task.isCancelled else { return }
        // The device does not volunteer the landing rig's amp, cabinet or effect
        // state, so read them back — now that the load itself has gone quiet.
        try? await model.refreshRig()
        Log.cmd("navigate to \(target): rig read-back requested")
        await sleep(DeviceStore.readBackSettle)
    }

    private func sleep(_ seconds: TimeInterval) async {
        try? await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
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
        case .currentPosition:
            // The device has said where it is. That retires this app's guesses —
            // but only once it agrees with them: mid-burst the device is still
            // reporting the moves before the last one, and treating those as the
            // truth would make every tap after the first step from a stale
            // index. An aim the device never confirms (one past the last rig, so
            // nothing moved and nothing is reported) expires instead — see
            // ``navigationIndex``.
            if let pending, pending.index == state.currentRigIndex {
                self.pending = nil
            }
            if let slot, slot == selectedSlot {
                selectedSlot = nil
            }
            // The send-once guard exists to stop an aim the device *ignored*
            // from being re-sent forever (aiming past the last rig moves nothing
            // and reports nothing). A report means the device did move, so the
            // guard has done its job — holding it would refuse a later, genuine
            // move back to the same index, say after a front-panel change.
            sentIndex = nil
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
