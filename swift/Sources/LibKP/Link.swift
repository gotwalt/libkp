import Foundation

// MARK: - Connect options

/// Whether ``DeviceModel`` opens the CBOR control link beside the stream, and
/// what its absence means.
public enum ControlPolicy: Sendable, Equatable {
    /// Never open it. The model reports ``Connection/connected`` on the stream
    /// alone, and the morph position stays unknown. For tooling and tests
    /// that must not touch the device a second time.
    case off
    /// Open it after the stream, in the background; if that fails, or the
    /// link later drops, carry on without it as ``Connection/degraded``. The
    /// default: a missing morph readout is not worth failing a session that
    /// carries everything else.
    case bestEffort
    /// The link must open, or ``DeviceModel/connect(host:port:options:)``
    /// fails — the stream is closed first, so nothing is left half-up.
    case required
}

/// What the model asks the device for as soon as the stream is up.
public enum SyncStrategy: Sendable, Equatable {
    /// Ask for nothing. The tree fills only from what the device pushes and
    /// what the control link's dump carries.
    case off
    /// Send the connect-time burst through the request lane: every row of the
    /// routing table marked `request = true` — the string tags, every effect
    /// slot's type and state, the bank preview, the position, and the
    /// numeric header values. The default: the burst is answered in about
    /// 50 ms, and it is what makes the snapshot whole before the device has
    /// had a reason to push anything.
    case streamBurst
}

/// How the model retries the stream and the control link. Both default to
/// never: a device that drops a session is telling the client something, and
/// dialling it again is a decision the client makes.
public struct ReconnectPolicy: Sendable, Equatable {
    /// Dial the stream again after it is lost, waiting out this backoff first
    /// — the whole connect sequence again on the same handle, same streams,
    /// same tree, until it works or ``DeviceModel/close()`` is called. `nil`
    /// (the default) reports ``Connection/disconnected`` and stops there.
    public var stream: Backoff?
    /// Reopen the control link this long after it becomes
    /// ``ChannelState/unavailable`` or ``ChannelState/lost``, while the stream
    /// is up — never closer than ``Generated/controlReopenMinGapMs`` to the
    /// last attempt, whatever this says. `nil` (the default) never reopens it;
    /// ``DeviceModel/reopenControl()`` is the explicit way.
    public var controlReopen: Duration?

    public init(stream: Backoff? = nil, controlReopen: Duration? = nil) {
        self.stream = stream
        self.controlReopen = controlReopen
    }
}

/// A doubling delay between reconnect attempts.
public struct Backoff: Sendable, Equatable {
    /// The wait before the first attempt.
    public var initial: Duration
    /// The wait never grows past this.
    public var max: Duration

    public init(
        initial: Duration = .milliseconds(Generated.reconnectDelayMs),
        max: Duration = .milliseconds(Generated.reconnectMaxDelayMs)
    ) {
        self.initial = initial
        self.max = max
    }

    /// The spec's stream backoff: ``Generated/reconnectDelayMs`` doubling to
    /// ``Generated/reconnectMaxDelayMs`` — what MetersApp used for a year
    /// without wedging a device.
    public static func defaultStream() -> Backoff { Backoff() }

    /// The wait before attempt `attempt` (counted from 1): `initial`, doubled
    /// once per attempt already made, capped at `max`.
    func delay(attempt: UInt32) -> Duration {
        var delay = initial
        for _ in 1..<Swift.max(attempt, 1) {
            delay = delay * 2
            if delay >= max { return max }
        }
        return Swift.min(delay, max)
    }
}

/// Everything ``DeviceModel/connect(host:port:options:)`` can be told. The
/// defaults are the recommended session: the control link best-effort, the
/// stream burst, no reconnects.
public struct ConnectOptions: Sendable, Equatable {
    public var control: ControlPolicy
    public var sync: SyncStrategy
    public var reconnect: ReconnectPolicy

    public init(
        control: ControlPolicy = .bestEffort,
        sync: SyncStrategy = .streamBurst,
        reconnect: ReconnectPolicy = ReconnectPolicy()
    ) {
        self.control = control
        self.sync = sync
        self.reconnect = reconnect
    }
}

// MARK: - The stream link

/// The MIDI3 socket of one life of the model: the session, the unframer that
/// turns its bytes into messages, and the task that reads it. The request lane
/// rides on it (see Lane.swift); the model writes to it directly, since the
/// session serialises writes itself.
///
/// Owned by the actor and mutated only on it; `Sendable` is claimed so the
/// actor's `deinit` may close it.
final class StreamLink: @unchecked Sendable {
    let session: Session
    var unframer = Midi3.Unframer()
    var ingestTask: Task<Void, Never>?

    init(session: Session) {
        self.session = session
    }

    func close() {
        ingestTask?.cancel()
        ingestTask = nil
        session.close()
    }
}

// MARK: - Supervisor

extension DeviceModel {
    /// Connect to `host:port`, open the stream, start ingesting, and — per
    /// `options` — start the connect-time sync and open the control link.
    ///
    /// Returns once the stream's handshake and preamble are done and the sync
    /// burst has been queued; the state fills in as the replies land. The
    /// control link opens in the background, spaced from the stream by the
    /// ``ConnectionLedger``, unless it is ``ControlPolicy/required``, in which
    /// case this waits for it. Subscribe *before* awaiting fresh events.
    public static func connect(
        host: String,
        port: UInt16 = Generated.port,
        options: ConnectOptions = ConnectOptions()
    ) async throws -> DeviceModel {
        let model = DeviceModel(host: host, port: port, options: options)
        try await model.openFirstLife()
        return model
    }

    /// Dial, handshake and preamble on the stream. No actor state is touched,
    /// so a caller can decide what to do with the session once it has it —
    /// including closing it, if the model moved on while the dial was out.
    static func dialStream(host: String, port: UInt16) async throws -> (Session, tail: [UInt8]) {
        let session = try await Session.connect(host: host, port: port)
        do {
            let outcome = try await session.handshake(
                preferred: [Generated.protocolMidi3Stream], idle: readIdle)
            try await session.writeSessionPreamble()
            return (session, outcome.responseTail)
        } catch {
            session.close()
            throw error
        }
    }

    private func openFirstLife() async throws {
        setChannel(.stream, .connecting)
        let session: Session
        let tail: [UInt8]
        do {
            (session, tail) = try await DeviceModel.dialStream(host: host, port: port)
        } catch {
            setChannel(.stream, .unavailable)
            throw error
        }
        beginLife(session: session, tail: tail)
        do {
            try await bringUpControl()
        } catch {
            // Required and refused: nothing is left half-up.
            close()
            throw error
        }
    }

    /// Install a freshly opened stream as the current life: mark it up, ingest
    /// the handshake tail, start the read loop, and queue the sync burst.
    private func beginLife(session: Session, tail: [UInt8]) {
        epoch += 1
        let epoch = self.epoch
        let link = StreamLink(session: session)
        stream = link
        reconnectAttempt = 0
        setChannel(.stream, .open)
        setConnection(.connected)
        publishSnapshot()
        ingestStream(tail, epoch: epoch)

        // The loop holds the model weakly and only while folding a chunk, so a
        // handle nobody references can still be deallocated mid-stream.
        link.ingestTask = Task { [weak self] in
            while !Task.isCancelled {
                do {
                    let chunk = try await session.readOnce(wait: DeviceModel.readIdle)
                    if !chunk.isEmpty { await self?.ingestStream(chunk, epoch: epoch) }
                } catch {
                    await self?.streamEnded(epoch: epoch)
                    return
                }
            }
        }

        if options.sync == .streamBurst {
            syncTask = Task { [weak self] in
                guard let self else { return }
                _ = try? await self.refresh()
                await self.syncFinished(epoch: epoch)
            }
        }
    }

    /// Open the control link per policy, once the stream is up. Only
    /// ``ControlPolicy/required`` can throw: the other two either do nothing
    /// or fail quietly into ``ChannelState/unavailable``.
    private func bringUpControl() async throws {
        switch options.control {
        case .off:
            return
        case .bestEffort:
            let epoch = self.epoch
            Task { [weak self] in
                _ = try? await self?.openControl(epoch: epoch)
            }
        case .required:
            try await openControl(epoch: epoch)
        }
    }

    /// The sync burst has drained — every reply landed, or timed out.
    func syncFinished(epoch: Int) {
        guard epoch == self.epoch else { return }
        syncTask = nil
        emit(.syncCompleted(source: .stream))
    }

    /// Fold one chunk off the stream: every message's events, at most one
    /// snapshot, and the replies the request lane is waiting on.
    func ingestStream(_ chunk: [UInt8], epoch: Int) {
        guard epoch == self.epoch, let stream else { return }
        var slow = false
        for message in stream.unframer.push(chunk) {
            guard let decoded = DeviceState.decode(message) else { continue }
            switch decoded {
            case .update(let update):
                let outcome = state.applyUpdate(update)
                for event in outcome.events { emit(event) }
                slow = slow || outcome.slowChanged
                resolve(update)
                slow = forwardPosition(outcome) || slow
            case let .renderedString(page, number, value, text):
                emit(.renderedString(page: page, number: number, value: value, text: text))
                resolveRender(page: page, number: number, value: value, text: text)
            }
        }
        if slow { publishSnapshot() }
    }

    /// A folded update moved the device's position: hand the flat index to
    /// the Navigator, whichever wire carried it. Returns whether the
    /// snapshot's navigation changed.
    private func forwardPosition(_ outcome: ApplyOutcome) -> Bool {
        let moved = outcome.events.contains {
            if case .currentPosition = $0 { true } else { false }
        }
        guard moved, let index = state.currentRigIndex else { return false }
        return navigationPosition(index)
    }

    /// The stream ended — a read error, EOF, or a failed write. Both links go
    /// down together; what happens next is the reconnect policy's call.
    func streamEnded(epoch: Int) {
        guard epoch == self.epoch, stream != nil, !closed else { return }
        tearDownLinks(lost: true)
        if let backoff = options.reconnect.stream {
            reconnectAttempt += 1
            setConnection(.reconnecting(attempt: reconnectAttempt))
            publishSnapshot()
            scheduleReconnect(after: backoff.delay(attempt: reconnectAttempt))
        } else {
            setConnection(.disconnected)
            publishSnapshot()
        }
    }

    /// Close both sockets, stop every task of this life, and refuse whatever
    /// was still waiting. The tree keeps its values; only `channels` and the
    /// navigation change, because they say what is true now: the stream is
    /// `lost` when the device ended it and `closed` when this side did, and
    /// an aim made in this life is forgotten with it — silently, since there
    /// is no session left for a drop to be about. Bumps the epoch, so nothing
    /// started under this life can report back.
    private func tearDownLinks(lost: Bool) {
        epoch += 1
        resetNavigation()
        syncTask?.cancel()
        syncTask = nil
        settleTask?.cancel()
        settleTask = nil
        reopenTask?.cancel()
        reopenTask = nil
        controlIngestTask?.cancel()
        controlIngestTask = nil
        control?.close()
        control = nil
        stream?.close()
        stream = nil
        if dumpActive {
            dumpActive = false
            state.endDump()
        }
        // The control link is closed, not lost: it did not fail on its own.
        setChannel(.control, .closed)
        setChannel(.stream, lost ? .lost : .closed)
        failPending()
    }

    private func scheduleReconnect(after delay: Duration) {
        let epoch = self.epoch
        reconnectTask = Task { [weak self] in
            try? await Task.sleep(for: delay)
            guard !Task.isCancelled else { return }
            await self?.retryLife(epoch: epoch)
        }
    }

    /// One reconnect attempt: the whole connect sequence again, on this handle.
    /// The ledger spaces the dial from the close that preceded it; the backoff
    /// has already been slept.
    private func retryLife(epoch: Int) async {
        guard epoch == self.epoch, !closed, let backoff = options.reconnect.stream else { return }
        setChannel(.stream, .connecting)
        publishSnapshot()
        do {
            let (session, tail) = try await DeviceModel.dialStream(host: host, port: port)
            guard epoch == self.epoch, !closed else {
                session.close()
                return
            }
            beginLife(session: session, tail: tail)
            do {
                try await bringUpControl()
            } catch {
                // Required and refused: this life is over before it began.
                streamEnded(epoch: self.epoch)
            }
        } catch {
            guard epoch == self.epoch, !closed else { return }
            setChannel(.stream, .unavailable)
            reconnectAttempt += 1
            setConnection(.reconnecting(attempt: reconnectAttempt))
            publishSnapshot()
            scheduleReconnect(after: backoff.delay(attempt: reconnectAttempt))
        }
    }

    /// Close both links, report ``Connection/disconnected``, and finish the
    /// snapshot and event streams. Nothing reopens afterwards. Idempotent: a
    /// second call does nothing.
    public func close() {
        guard !closed else { return }
        closed = true
        reconnectTask?.cancel()
        reconnectTask = nil
        tearDownLinks(lost: false)
        setConnection(.disconnected)
        publishSnapshot()
        for (_, continuation) in eventContinuations { continuation.finish() }
        for (_, continuation) in snapshotContinuations { continuation.finish() }
        eventContinuations.removeAll()
        snapshotContinuations.removeAll()
    }

    // MARK: - The control link

    /// Open the control link now, outside any policy.
    ///
    /// Refused with ``ChannelError/tooSoon`` inside
    /// ``Generated/controlReopenMinGapMs`` of the last attempt — the device
    /// wedges under session churn, and a Reconnect button pressed twice must
    /// not be what does it — and with ``ChannelError/off`` when the model was
    /// connected with ``ControlPolicy/off``. A link already open, or already
    /// opening, is left alone. Waits for the open: on failure the link is
    /// ``ChannelState/unavailable`` and the error says why.
    public func reopenControl() async throws {
        guard options.control != .off else { throw ChannelError.off }
        guard !closed, stream != nil, state.channels.stream == .open else {
            throw ChannelError.disconnected
        }
        switch state.channels.control {
        case .connecting, .open: return
        case .closed, .unavailable, .lost: break
        }
        let gap = Duration.milliseconds(Generated.controlReopenMinGapMs)
        if let last = lastControlAttempt, last.duration(to: .now) < gap {
            throw ChannelError.tooSoon
        }
        reopenTask?.cancel()
        reopenTask = nil
        do {
            try await openControl(epoch: epoch)
        } catch let error as SessionError {
            throw ChannelError.session(error)
        } catch {
            throw ChannelError.disconnected
        }
    }

    /// The control task: dial, handshake, preamble, trigger, then ingest until
    /// the socket ends. Throws what the open threw; a link that opened and was
    /// later lost reports through `channels` instead. Throws
    /// `CancellationError` if the life ended while the dial was out.
    func openControl(epoch: Int) async throws {
        guard epoch == self.epoch, stream != nil, !closed else { throw CancellationError() }
        lastControlAttempt = .now
        setChannel(.control, .connecting)
        publishSnapshot()

        let link: ControlLink
        do {
            link = try await ControlLink.open(host: host, port: port)
        } catch {
            if epoch == self.epoch, !closed {
                setChannel(.control, .unavailable)
                refreshConnection()
                publishSnapshot()
                scheduleControlReopen()
            }
            throw error
        }
        guard epoch == self.epoch, stream != nil, !closed else {
            link.close()
            throw CancellationError()
        }

        control = link
        dumpActive = true
        state.beginDump()
        setChannel(.control, .open)
        refreshConnection()
        publishSnapshot()
        ingestControl(link.push(link.tail), epoch: epoch, from: link)

        // The dump ends on its end marker; this is the fallback if it never
        // comes, so the tree is not left refusing dump items forever.
        settleTask = Task { [weak self] in
            try? await Task.sleep(for: .milliseconds(Generated.dumpSettleMs))
            guard !Task.isCancelled else { return }
            await self?.dumpSettled(epoch: epoch, from: link)
        }
        controlIngestTask = Task { [weak self] in
            while !Task.isCancelled {
                do {
                    let items = try await link.read(wait: ControlLink.readIdle)
                    if !items.isEmpty { await self?.ingestControl(items, epoch: epoch, from: link) }
                } catch {
                    await self?.controlEnded(epoch: epoch, from: link)
                    return
                }
            }
        }
    }

    /// Fold one chunk off the control link: every item's values, tagged with
    /// the dump phase while the dump is streaming, and at most one snapshot.
    /// The run based at ``Generated/dumpEndAddress`` closes the dump once it
    /// has been folded; whatever follows it is live.
    func ingestControl(_ items: [CBORValue], epoch: Int, from link: ControlLink) {
        guard epoch == self.epoch, control === link else { return }
        var slow = false
        for raw in items {
            guard let item = Cbor.controlItem(raw) else { continue }
            let phase: Phase = dumpActive ? .dump : .live
            for entry in item.entries {
                let update: Update
                switch entry {
                case let .num(address, value):
                    // A negative value is nothing the tree stores.
                    guard let value = UInt64(exactly: value) else { continue }
                    update = Update(
                        source: .control, phase: phase, address: address, decoded: .num(value))
                case let .text(address, text):
                    update = Update(
                        source: .control, phase: phase, address: address, decoded: .text(text))
                }
                let outcome = state.applyUpdate(update)
                for event in outcome.events { emit(event) }
                slow = slow || outcome.slowChanged
                resolve(update)
                slow = forwardPosition(outcome) || slow
            }
            if dumpActive && item.base == Generated.dumpEndAddress { finishDump() }
        }
        if slow { publishSnapshot() }
    }

    /// The settle time elapsed without the end marker.
    func dumpSettled(epoch: Int, from link: ControlLink) {
        guard epoch == self.epoch, control === link, dumpActive else { return }
        finishDump()
    }

    private func finishDump() {
        dumpActive = false
        state.endDump()
        settleTask?.cancel()
        settleTask = nil
        emit(.syncCompleted(source: .control))
    }

    /// The control socket ended while the stream is still up.
    func controlEnded(epoch: Int, from link: ControlLink) {
        guard epoch == self.epoch, control === link, !closed else { return }
        link.close()
        control = nil
        controlIngestTask = nil
        settleTask?.cancel()
        settleTask = nil
        if dumpActive {
            // The dump did not finish; it is simply over.
            dumpActive = false
            state.endDump()
        }
        setChannel(.control, .lost)
        refreshConnection()
        publishSnapshot()
        scheduleControlReopen()
    }

    /// Queue the one automatic reopen the policy allows, if it allows any.
    private func scheduleControlReopen() {
        guard let requested = options.reconnect.controlReopen else { return }
        let wait = Swift.max(requested, .milliseconds(Generated.controlReopenMinGapMs))
        let epoch = self.epoch
        reopenTask = Task { [weak self] in
            try? await Task.sleep(for: wait)
            guard !Task.isCancelled else { return }
            _ = try? await self?.openControl(epoch: epoch)
        }
    }

    // MARK: - Connection bookkeeping

    /// Move one link's state and say so. Every transition is an event.
    func setChannel(_ channel: Channel, _ next: ChannelState) {
        switch channel {
        case .stream:
            guard state.channels.stream != next else { return }
            state.channels.stream = next
        case .control:
            guard state.channels.control != next else { return }
            state.channels.control = next
        }
        emit(.channelChanged(channel: channel, state: next))
    }

    /// Move the summary and say so. ``DeviceEvent/connected`` is raised when
    /// the stream comes up — not when the control link merely recovers — and
    /// ``DeviceEvent/disconnected`` when the model stops trying.
    func setConnection(_ next: Connection) {
        let previous = state.connection
        guard previous != next else { return }
        state.connection = next
        switch next {
        case .connected where previous != .degraded:
            emit(.connected)
        case .disconnected:
            emit(.disconnected)
        default:
            break
        }
        emit(.connectionChanged(next))
    }

    /// Re-derive the summary from the two links while the stream is up:
    /// degraded when a control link that was asked for is not there.
    func refreshConnection() {
        guard state.channels.stream == .open else { return }
        let missing = state.channels.control == .unavailable || state.channels.control == .lost
        setConnection(options.control != .off && missing ? .degraded : .connected)
    }
}
