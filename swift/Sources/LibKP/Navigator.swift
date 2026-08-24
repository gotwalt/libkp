import Foundation

// MARK: - The Navigator

/// The only way a rig is loaded through this library.
///
/// A rig load makes the device replay its whole parameter tree on both wires,
/// and two loads that land on top of each other are what wedges it: it answers
/// the first normally, closes the session some twenty seconds later, and stops
/// accepting connections until it is power-cycled. The fuse is delayed, so
/// nothing in the reply to a burst says it did any harm. The Navigator makes
/// the overlap structurally impossible: every load goes through one serializer
/// inside the model, and the direct routes — ``Control/loadSlot(_:)``,
/// ``Control/up``, ``Control/down``, a Program Change — are refused with
/// ``CommandError/rigLoadRequiresNavigator``.
///
/// The serializer is this pure, clock-free state machine. The model drives it
/// with the four inputs — a new aim, the two timers, and every position the
/// device reports — and executes the ``NavAction``s it hands back. Its rules:
///
/// - **A burst of taps costs two loads however long it is.** The aim moves
///   freely while a move is in flight; the pump sends wherever the aim ended
///   up once the flight settles.
/// - **A move is in flight for ``Generated/rigLoadSettleMs``**, a fixed wait
///   right at the measured edge of the device pushing the landed rig on both
///   wires. Nothing shortens it — not even the device confirming the position
///   early — because the rig dump is still streaming after the position is
///   reported.
/// - **An index already sent is never re-sent.** Aim past the last rig and the
///   device stays put and says nothing; re-sending would loop forever. Once
///   its move has settled, such an aim is given ``Generated/pendingWindowMs``
///   to be confirmed and is then dropped, so the device's own position is the
///   truth again.
/// - **A position that matches the aim retires it; one that does not is
///   ignored.** Mid-burst the device is still reporting the moves before the
///   last one, and treating those as the truth would make every tap after the
///   first step from a stale index.
///
/// `spec/vectors/navigation.json` pins every step of this.
public struct NavigatorState: Sendable, Equatable {
    /// Where the client wants to be: a flat 0-based rig index the device has
    /// not yet confirmed, or `nil` when there is nothing outstanding.
    public var aim: UInt16?
    /// The last index actually put on the wire, held until the device
    /// confirms it or the window drops it, so the same index is not sent
    /// twice while the device is ignoring it.
    public var sent: UInt16?
    /// Whether a sent move is still inside its settle time.
    public var inFlight: Bool
    /// Whether the pending window is open on an unconfirmed aim.
    public var awaiting: Bool

    /// A fresh machine: nothing aimed, nothing sent.
    public init(
        aim: UInt16? = nil, sent: UInt16? = nil, inFlight: Bool = false, awaiting: Bool = false
    ) {
        self.aim = aim
        self.sent = sent
        self.inFlight = inFlight
        self.awaiting = awaiting
    }

    /// Aim at `target`. Sends it at once if nothing is in flight; otherwise
    /// only the aim moves, and the settle sends it. A new aim while the
    /// window is open on the last one cancels that window by pumping.
    public mutating func navigate(_ target: UInt16) -> [NavAction] {
        aim = target
        return inFlight ? [] : pump()
    }

    /// The settle timer fired: the move is no longer in flight. If the index
    /// it carried is still the aim and unconfirmed, the pending window opens
    /// on it; either way the pump sends whatever the aim moved on to.
    public mutating func settleElapsed() -> [NavAction] {
        inFlight = false
        var actions: [NavAction] = []
        if let aim, aim == sent {
            awaiting = true
            actions.append(.startWindow)
        }
        actions.append(contentsOf: pump())
        return actions
    }

    /// The window timer fired: an aim the device never confirmed is dropped,
    /// and the sent index is forgotten with it so the same index may be tried
    /// again later. A window that was already cancelled does nothing.
    public mutating func windowElapsed() -> [NavAction] {
        guard awaiting, let aim else { return [] }
        self.aim = nil
        sent = nil
        awaiting = false
        return [.dropped(aim)]
    }

    /// The device reported its position, on either wire. Only a report equal
    /// to the aim retires it: the device may still be on its way, or the aim
    /// may be past the end and never coming.
    public mutating func position(_ index: UInt16) -> [NavAction] {
        guard aim == index else { return [] }
        aim = nil
        sent = nil
        awaiting = false
        return [.settled(index)]
    }

    /// Send the aim if nothing is in flight and it is not the index already
    /// on the wire.
    private mutating func pump() -> [NavAction] {
        guard !inFlight, let aim, aim != sent else { return [] }
        sent = aim
        inFlight = true
        awaiting = false
        return [.send(aim), .startSettle]
    }
}

/// What the model does on the state machine's behalf, in the order given.
public enum NavAction: Sendable, Equatable {
    /// Put the load for this flat rig index on the wire: the documented pair,
    /// bank preselect (CC47) then slot load (CC50–54).
    case send(UInt16)
    /// Arm the ``Generated/rigLoadSettleMs`` timer, whose expiry is
    /// ``NavigatorState/settleElapsed()``.
    case startSettle
    /// Arm the ``Generated/pendingWindowMs`` timer, whose expiry is
    /// ``NavigatorState/windowElapsed()``.
    case startWindow
    /// The device confirmed the aim: raise ``DeviceEvent/navigationSettled(index:)``.
    case settled(UInt16)
    /// The aim was given up: raise ``DeviceEvent/navigationDropped(index:reason:)``.
    case dropped(UInt16)
}

/// What the snapshot shows of the Navigator: the outstanding aim and whether
/// a move is in flight. ``NavigatorState/sent`` and the window are the
/// machine's own business.
public struct Navigation: Sendable, Equatable {
    /// The flat rig index the client last aimed at and the device has not yet
    /// confirmed, or `nil` when the device's position is the whole truth.
    public var aim: UInt16?
    /// Whether a load is inside its settle time.
    public var inFlight: Bool

    public init(aim: UInt16? = nil, inFlight: Bool = false) {
        self.aim = aim
        self.inFlight = inFlight
    }
}

/// Why an aim was dropped.
public enum NavDrop: Sendable, Equatable {
    /// The device never reported the aimed index inside
    /// ``Generated/pendingWindowMs`` of its move settling — the usual cause
    /// being an index past the last rig, which the device ignores.
    case unconfirmed
}

// MARK: - The Navigator in the model

extension DeviceModel {
    /// Aim at a flat, 0-based rig index — the device's own numbering, and the
    /// only address that reaches a rig outside the current bank — and load it,
    /// one move at a time.
    ///
    /// Returns as soon as the aim is recorded; the load itself goes out now if
    /// nothing is in flight, else once the previous load has settled. Follow
    /// ``DeviceState/navigation`` for the aim and
    /// ``DeviceEvent/navigationSettled(index:)`` /
    /// ``DeviceEvent/navigationDropped(index:reason:)`` for how it ended.
    ///
    /// Nothing here assumes how many banks a device has. Aim past the end and
    /// the device simply stays where it is — and says so in the `$06` position
    /// push that follows, so ``DeviceState/currentRigIndex`` always reflects
    /// where it actually landed; the aim is dropped after the window.
    public func navigateTo(_ index: UInt16) {
        drive(navigator.navigate(index))
        publishNavigation()
    }

    /// Step `delta` rigs from ``DeviceState/aimedRigIndex`` — the outstanding
    /// aim, or the device's reported position — floored at 0, and load the
    /// result. Ignored while no position is known: there is nothing to step
    /// *from*, and guessing would jump somewhere arbitrary. A step that lands
    /// where the aim already is — Previous at rig 0 — sends nothing, since a
    /// reload of the same rig is a load the device pays for and nobody asked
    /// for.
    ///
    /// Stepping from the aim rather than the position is what makes two taps
    /// inside the device's reporting delay compose instead of both stepping
    /// from the same stale index.
    public func stepRig(by delta: Int) {
        guard let current = state.aimedRigIndex else { return }
        let target = UInt16(clamping: max(Int(current) + delta, 0))
        guard target != current else { return }
        navigateTo(target)
    }

    /// Step one bank, keeping the slot — ``Params/bankSlots`` rigs at a time,
    /// floored at 0. Ignored while no position is known.
    public func stepBank(forward: Bool) {
        stepRig(by: forward ? Params.bankSlots : -Params.bankSlots)
    }

    /// Load `slot` (1…``Params/bankSlots``, clamped) of the aimed bank — the
    /// bank of the outstanding aim, else the device's own. Ignored while no
    /// position is known, since there is no bank to name.
    ///
    /// Addressed as a flat index like every other move, so the bank preselect
    /// is re-armed with the slot load: a bare slot load drops into whatever
    /// bank is still loaded, silently undoing a Bank Up tapped a moment
    /// earlier.
    public func selectSlot(_ slot: UInt8) {
        guard let current = state.aimedRigIndex else { return }
        let slots = UInt16(Params.bankSlots)
        let clamped = UInt16(min(max(Int(slot), 1), Params.bankSlots))
        navigateTo(current / slots * slots + clamped - 1)
    }

    /// Execute the actions the state machine handed back, in order.
    ///
    /// A `send` while the stream is not open cannot be queued for later — the
    /// aim would be sent into a session that no longer exists — so it is
    /// dropped on the spot and the machine reset. The timers are armed under a
    /// serial each, so one that was superseded or cancelled cannot call back
    /// into a machine that has moved on.
    func drive(_ actions: [NavAction]) {
        for action in actions {
            switch action {
            case .send(let index):
                guard stream != nil, state.channels.stream == .open else {
                    cancelNavigationTimers()
                    navigator = NavigatorState()
                    emit(.navigationDropped(index: index, reason: .unconfirmed))
                    return
                }
                let epoch = self.epoch
                Task { [weak self] in
                    guard let self else { return }
                    await self.sendRigLoad(index, epoch: epoch)
                }
            case .startSettle:
                navSettleTask?.cancel()
                navSettleSerial &+= 1
                let serial = navSettleSerial
                navSettleTask = Task { [weak self] in
                    try? await Task.sleep(for: .milliseconds(Generated.rigLoadSettleMs))
                    guard !Task.isCancelled else { return }
                    await self?.navigationSettleElapsed(serial: serial)
                }
            case .startWindow:
                navWindowTask?.cancel()
                navWindowSerial &+= 1
                let serial = navWindowSerial
                navWindowTask = Task { [weak self] in
                    try? await Task.sleep(for: .milliseconds(Generated.pendingWindowMs))
                    guard !Task.isCancelled else { return }
                    await self?.navigationWindowElapsed(serial: serial)
                }
            case .settled(let index):
                emit(.navigationSettled(index: index))
            case .dropped(let index):
                emit(.navigationDropped(index: index, reason: .unconfirmed))
            }
        }
        // A window the machine has walked away from — a new aim pumped, or
        // the position confirmed — must not fire into the next move.
        if !navigator.awaiting, let task = navWindowTask {
            task.cancel()
            navWindowTask = nil
            navWindowSerial &+= 1
        }
    }

    /// The documented pair for a flat index: the absolute bank preselect
    /// (CC47) followed by the slot load (CC50–54) that commits it. The index
    /// divides by ``Params/bankSlots``, so index 123 is bank 25, slot 4.
    ///
    /// A failed write is the stream ending, which the supervisor handles;
    /// the aim goes with the life.
    private func sendRigLoad(_ index: UInt16, epoch: Int) async {
        guard epoch == self.epoch else { return }
        let slots = UInt16(Params.bankSlots)
        let bank = UInt8(truncatingIfNeeded: index / slots)
        let slot = UInt8(index % slots) + 1
        do {
            try await write(Control.bankPreselect(bank).message(channel: DeviceModel.ccChannel))
            try await write(Control.loadSlot(slot).message(channel: DeviceModel.ccChannel))
        } catch {
            // The supervisor has already torn the life down.
        }
    }

    /// The settle timer fired. A serial that no longer matches is a timer
    /// this life has already cancelled or re-armed, and is ignored.
    private func navigationSettleElapsed(serial: Int) {
        guard serial == navSettleSerial, !closed else { return }
        navSettleTask = nil
        drive(navigator.settleElapsed())
        publishNavigation()
    }

    /// The window timer fired, under the same rule.
    private func navigationWindowElapsed(serial: Int) {
        guard serial == navWindowSerial, !closed else { return }
        navWindowTask = nil
        drive(navigator.windowElapsed())
        publishNavigation()
    }

    /// A position landed in the tree, off either wire: hand it to the
    /// machine. Returns whether the snapshot's navigation changed, so the
    /// ingest can fold it into its one publish per chunk.
    func navigationPosition(_ index: UInt16) -> Bool {
        drive(navigator.position(index))
        return mirrorNavigation()
    }

    /// Copy `{aim, inFlight}` into the snapshot; `true` if it moved.
    @discardableResult
    func mirrorNavigation() -> Bool {
        let mirror = Navigation(aim: navigator.aim, inFlight: navigator.inFlight)
        guard state.navigation != mirror else { return false }
        state.navigation = mirror
        return true
    }

    private func publishNavigation() {
        if mirrorNavigation() { publishSnapshot() }
    }

    /// Forget the aim and stop both timers, without an event: the stream is
    /// gone, and the aim went with the life it was made in.
    func resetNavigation() {
        cancelNavigationTimers()
        navigator = NavigatorState()
        mirrorNavigation()
    }

    /// Stop both timers. The serials move on too, so a timer whose `cancel`
    /// arrived after it had already woken cannot call back in.
    private func cancelNavigationTimers() {
        navSettleTask?.cancel()
        navSettleTask = nil
        navSettleSerial &+= 1
        navWindowTask?.cancel()
        navWindowTask = nil
        navWindowSerial &+= 1
    }
}
