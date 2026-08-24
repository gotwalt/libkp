import Foundation

// MARK: - The request lane

/// What a request resolves to: the value that landed at its address.
enum Reply: Sendable {
    case num(UInt64)
    case text(String)

    var num: UInt64? {
        if case .num(let value) = self { return value }
        return nil
    }
    var text: String? {
        if case .text(let text) = self { return text }
        return nil
    }
}

/// The identity of a `$7C` rendered-string request: its reply mirrors all
/// three, so all three are the key.
struct RenderKey: Hashable, Sendable {
    let page: UInt8
    let number: UInt8
    let value: UInt16
}

/// One request waiting on the lane.
///
/// The reply can land before the requester has suspended on it — the actor is
/// free while the request is being written, and the device answers in tens of
/// milliseconds — so the entry holds either the continuation or the result,
/// whichever comes first, and joins them when the other arrives.
final class PendingRequest {
    let id: UInt64
    /// Whether a string, rather than a numeric, is what answers this.
    let wantsText: Bool
    var result: Result<Reply, RequestError>?
    var continuation: CheckedContinuation<Reply, any Error>?
    var timeout: Task<Void, Never>?

    init(id: UInt64, wantsText: Bool) {
        self.id = id
        self.wantsText = wantsText
    }

    /// Hand the outcome to the requester, now or when it arrives.
    func settle(_ outcome: Result<Reply, RequestError>) {
        timeout?.cancel()
        timeout = nil
        guard let continuation else {
            if result == nil { result = outcome }
            return
        }
        self.continuation = nil
        switch outcome {
        case .success(let reply): continuation.resume(returning: reply)
        case .failure(let error): continuation.resume(throwing: error)
        }
    }
}

extension DeviceModel {
    // MARK: - Public requests

    /// Request one numeric parameter's current value (function `$41`) and
    /// return it. The device answers with a `$01` at the same address, which
    /// folds into the snapshot on its way here.
    ///
    /// Throws ``RequestError/timeout`` after ``Generated/requestTimeoutMs``
    /// with no value at the address — the request is never resent — and
    /// ``RequestError/unreadable`` at once, sending nothing, for the morph
    /// position: the stream never answers it. An unsolicited push at the
    /// address counts as the reply; it is no less current.
    public func requestParam(page: UInt8, number: UInt8) async throws -> UInt16 {
        let address = UInt32(page) * 128 + UInt32(number)
        let reply = try await ask(
            Nrpn.requestSingle(
                product: DeviceModel.product, device: DeviceModel.device, page: page, number: number
            ), at: address, text: false)
        return UInt16(clamping: reply.num ?? 0)
    }

    /// Request one string parameter (function `$43`), e.g. a page-0 string tag,
    /// and return it. The `$03` reply folds into the snapshot on the way.
    public func requestString(
        page: UInt8 = Generated.pageStrings, number: UInt8
    ) async throws -> String {
        let address = UInt32(page) * 128 + UInt32(number)
        let reply = try await ask(
            Nrpn.requestString(
                product: DeviceModel.product, device: DeviceModel.device, page: page, number: number
            ), at: address, text: true)
        return reply.text ?? ""
    }

    /// Request an extended-address numeric parameter (function `$46`) — the
    /// device's current bank and rig slot live here — and return the `$06`
    /// reply's value, which spans 35 bits.
    public func requestExtParam(address: UInt32) async throws -> UInt64 {
        let reply = try await ask(
            Nrpn.requestExtendedParam(
                product: DeviceModel.product, device: DeviceModel.device, address: address
            ), at: address, text: false)
        return reply.num ?? 0
    }

    /// Request an extended-address string (function `$47`) — the bank preview's
    /// names live here — and return the `$07` reply's text.
    public func requestExtString(address: UInt32) async throws -> String {
        let reply = try await ask(
            Nrpn.requestExtendedString(
                product: DeviceModel.product, device: DeviceModel.device, address: address
            ), at: address, text: true)
        return reply.text ?? ""
    }

    /// Request a parameter value rendered to its exact display text (function
    /// `$7C`) — e.g. `"5.2"`, `"120 BPM"`, `"<0.0>"` instead of a generic
    /// percentage — and return it. The `$3C` reply is also raised as
    /// ``DeviceEvent/renderedString(page:number:value:text:)``; it is never
    /// stored.
    public func requestRender(page: UInt8, number: UInt8, value: UInt16) async throws -> String {
        let key = RenderKey(page: page, number: number, value: value)
        let message = Nrpn.requestRenderedString(
            product: DeviceModel.product, device: DeviceModel.device,
            page: page, number: number, value: value)
        guard state.channels.stream == .open else { throw RequestError.disconnected }
        guard await acquireSlot() else { throw RequestError.disconnected }
        defer { releaseSlot() }
        let entry = register(wantsText: true, at: .render(key))
        pendingRenders[key, default: []].append(entry)
        do {
            try await write(message)
        } catch {
            remove(entry.id, at: .render(key))
            entry.settle(.failure(.disconnected))
            throw RequestError.disconnected
        }
        return try await wait(for: entry).text ?? ""
    }

    // MARK: - Refresh

    /// Ask the device for every value the routing table marks `request = true`
    /// — the connect-time burst — through the lane, and wait for the replies.
    ///
    /// Read-only: it only issues value requests and changes nothing on the
    /// device. The replies fold into the tree as they land. Returns once every
    /// one has answered; throws ``RequestError/timeout`` if any did not (the
    /// others still landed), or ``RequestError/disconnected`` if the stream
    /// went away underneath it.
    public func refresh() async throws {
        try await request(rows: Generated.stateRoutes.filter(\.request))
    }

    /// Re-request the rig strings and every effect slot's Type/State — the
    /// subset of ``refresh()`` a rig load changes.
    public func refreshRig() async throws {
        let fields: Set<Route.Field> = [
            .rigName, .rigAuthor, .rigDate, .rigComment, .ampName, .cabinetName, .effectType,
            .effectOn,
        ]
        try await request(
            rows: Generated.stateRoutes.filter { $0.request && fields.contains($0.field) })
    }

    /// Request the current bank's five-slot name preview (rig / amp / cabinet
    /// names) as extended strings (`$47`). The `$07` replies fold into
    /// ``DeviceState/bank``. The device also pushes this block unasked on a
    /// bank change, so a controller need only call this once at connect —
    /// which ``SyncStrategy/streamBurst`` already does.
    public func refreshBank() async throws {
        let fields: Set<Route.Field> = [.bankRigName, .bankAmpName, .bankCabinetName]
        try await request(
            rows: Generated.stateRoutes.filter { $0.request && fields.contains($0.field) })
    }

    /// Ask the device where it is: the current bank and rig slot, as two `$46`
    /// extended-parameter requests. The `$06` replies fold into
    /// ``DeviceState/currentBank`` and ``DeviceState/currentRigSlot``.
    ///
    /// Only needed once, at connect — the burst covers it — since the device
    /// pushes an unsolicited `$06` for whichever of the two changed on every
    /// subsequent rig change, whoever caused it.
    public func refreshPosition() async throws {
        let fields: Set<Route.Field> = [.currentBank, .currentRigSlot]
        try await request(
            rows: Generated.stateRoutes.filter { $0.request && fields.contains($0.field) })
    }

    /// Issue every row's request at once — the lane paces them — and report
    /// the worst outcome.
    private func request(rows: [Route]) async throws {
        let outcomes = await withTaskGroup(of: RequestError?.self) { group in
            for row in rows {
                group.addTask { await self.request(row: row) }
            }
            var out: [RequestError?] = []
            for await outcome in group { out.append(outcome) }
            return out
        }
        if outcomes.contains(.disconnected) { throw RequestError.disconnected }
        if outcomes.contains(.timeout) { throw RequestError.timeout }
    }

    /// One row's request: a paged address goes as `$41`/`$43`, an extended
    /// one as `$46`/`$47`, by whether the row stores text.
    private func request(row: Route) async -> RequestError? {
        do {
            let paged = row.address < 16384
            let page = UInt8(truncatingIfNeeded: row.address / 128)
            let number = UInt8(truncatingIfNeeded: row.address % 128)
            switch (row.kind == .text, paged) {
            case (true, true): _ = try await requestString(page: page, number: number)
            case (true, false): _ = try await requestExtString(address: row.address)
            case (false, true): _ = try await requestParam(page: page, number: number)
            case (false, false): _ = try await requestExtParam(address: row.address)
            }
            return nil
        } catch let error as RequestError {
            return error
        } catch {
            return .disconnected
        }
    }

    // MARK: - The lane

    /// Send one request and wait for a value at `address`.
    ///
    /// The pending entry is registered *before* the write goes out: the actor
    /// is free while the write is in flight, and a reply that beat the entry
    /// would otherwise wait out the full timeout for nothing.
    private func ask(_ message: [UInt8], at address: UInt32, text: Bool) async throws -> Reply {
        guard Routes.lookup(address)?.wire != .control else { throw RequestError.unreadable }
        guard state.channels.stream == .open else { throw RequestError.disconnected }
        guard await acquireSlot() else { throw RequestError.disconnected }
        defer { releaseSlot() }
        let entry = register(wantsText: text, at: .address(address))
        pending[address, default: []].append(entry)
        do {
            try await write(message)
        } catch {
            remove(entry.id, at: .address(address))
            entry.settle(.failure(.disconnected))
            throw RequestError.disconnected
        }
        return try await wait(for: entry)
    }

    /// Where a pending entry is filed: under its address, or under the three
    /// values a rendered-string reply mirrors.
    enum Slot: Sendable {
        case address(UInt32)
        case render(RenderKey)

        /// The flat address ``DeviceEvent/requestTimedOut(address:)`` names.
        var address: UInt32 {
            switch self {
            case .address(let address): return address
            case .render(let key): return UInt32(key.page) * 128 + UInt32(key.number)
            }
        }
    }

    /// Create the entry and arm its timeout. The caller files it.
    private func register(wantsText: Bool, at slot: Slot) -> PendingRequest {
        nextRequestID &+= 1
        let entry = PendingRequest(id: nextRequestID, wantsText: wantsText)
        let id = entry.id
        entry.timeout = Task { [weak self] in
            try? await Task.sleep(for: .milliseconds(Generated.requestTimeoutMs))
            guard !Task.isCancelled else { return }
            await self?.timeOut(id: id, at: slot)
        }
        return entry
    }

    /// Unfile an entry, returning it if it was still there.
    @discardableResult
    private func remove(_ id: UInt64, at slot: Slot) -> PendingRequest? {
        switch slot {
        case .address(let address):
            guard let index = pending[address]?.firstIndex(where: { $0.id == id }) else {
                return nil
            }
            let entry = pending[address]?.remove(at: index)
            if pending[address]?.isEmpty == true { pending[address] = nil }
            return entry
        case .render(let key):
            guard let index = pendingRenders[key]?.firstIndex(where: { $0.id == id }) else {
                return nil
            }
            let entry = pendingRenders[key]?.remove(at: index)
            if pendingRenders[key]?.isEmpty == true { pendingRenders[key] = nil }
            return entry
        }
    }

    private func wait(for entry: PendingRequest) async throws -> Reply {
        try await withCheckedThrowingContinuation { continuation in
            switch entry.result {
            case .success(let reply)?: continuation.resume(returning: reply)
            case .failure(let error)?: continuation.resume(throwing: error)
            case nil: entry.continuation = continuation
            }
        }
    }

    /// The timeout fired: drop the entry if it is still waiting, say so, and
    /// never resend.
    private func timeOut(id: UInt64, at slot: Slot) {
        guard let entry = remove(id, at: slot) else { return }
        entry.settle(.failure(.timeout))
        emit(.requestTimedOut(address: slot.address))
    }

    /// Take one of the ``Generated/maxInFlightRequests`` slots, waiting for one
    /// to free up. `false` means the stream went away while waiting: the
    /// caller holds no slot and must not release one.
    private func acquireSlot() async -> Bool {
        if inFlight < Generated.maxInFlightRequests {
            inFlight += 1
            return true
        }
        return await withCheckedContinuation { laneWaiters.append($0) }
    }

    /// Hand the slot to the next waiter, or give it back.
    private func releaseSlot() {
        if !laneWaiters.isEmpty {
            laneWaiters.removeFirst().resume(returning: true)
        } else {
            inFlight -= 1
        }
    }

    /// A value landed at an address: settle whatever was waiting for one of
    /// its shape. Whichever wire carried it, and whether or not the tree
    /// changed — a reply equal to the stored value is still the reply.
    func resolve(_ update: Update) {
        switch update.decoded {
        case .num(let value):
            settle(address: update.address, with: .num(value))
        case .text(let text):
            settle(address: update.address, with: .text(text))
        case .block(let values):
            for (i, value) in values.enumerated() {
                settle(address: update.address + UInt32(i), with: .num(UInt64(value)))
            }
        }
    }

    private func settle(address: UInt32, with reply: Reply) {
        guard var entries = pending[address] else { return }
        let wantsText = reply.text != nil
        entries.removeAll { entry in
            guard entry.wantsText == wantsText else { return false }
            entry.settle(.success(reply))
            return true
        }
        pending[address] = entries.isEmpty ? nil : entries
    }

    /// A `$3C` reply landed: settle the render request it mirrors.
    func resolveRender(page: UInt8, number: UInt8, value: UInt16, text: String) {
        let key = RenderKey(page: page, number: number, value: value)
        guard let entries = pendingRenders.removeValue(forKey: key) else { return }
        for entry in entries { entry.settle(.success(.text(text))) }
    }

    /// The stream is gone: refuse everything waiting, on the wire or for a
    /// slot. Waiters are woken without a slot; the entries release theirs as
    /// their requesters unwind.
    func failPending() {
        let waiters = laneWaiters
        laneWaiters.removeAll()
        for waiter in waiters { waiter.resume(returning: false) }
        let entries = pending.values.flatMap { $0 } + pendingRenders.values.flatMap { $0 }
        pending.removeAll()
        pendingRenders.removeAll()
        for entry in entries { entry.settle(.failure(.disconnected)) }
    }
}
