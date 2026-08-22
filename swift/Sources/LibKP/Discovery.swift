import Foundation

/// One raw reply from a candidate device.
public struct DiscoveryReply: Sendable, Equatable {
    /// The sender's address, as reported by the transport.
    public let host: String
    /// The raw reply payload.
    public let payload: [UInt8]

    public init(host: String, payload: [UInt8]) {
        self.host = host
        self.payload = payload
    }

    /// The device's advertised `NAME` field, if the payload parses.
    public var name: String? {
        (try? TagStream.parse(payload))?.stringValue(forKey: "NAME")
    }

    /// The device's advertised serial number (`SER#`), if present.
    public var serial: String? {
        (try? TagStream.parse(payload))?.stringValue(forKey: "SER#")
    }
}

/// Options controlling a discovery run.
public struct DiscoveryOptions: Sendable {
    /// Client MAC placed in the poll (the all-zero placeholder is fine).
    public var mac: String
    /// How long to keep listening for replies.
    public var listenFor: TimeInterval
    /// Re-send the poll this often while listening.
    public var repeatEvery: TimeInterval
    /// Extra explicit targets to send the poll to (e.g. a known device IP).
    public var extraTargets: [String]

    public init(
        mac: String = Generated.pollPlaceholderMac,
        listenFor: TimeInterval = 3,
        repeatEvery: TimeInterval = TimeInterval(Generated.pollIntervalMs) / 1000,
        extraTargets: [String] = []
    ) {
        self.mac = mac
        self.listenFor = listenFor
        self.repeatEvery = repeatEvery
        self.extraTargets = extraTargets
    }
}

/// Exclusive owner of the UDP discovery port.
///
/// Acquire one before opening a session and keep it for the session's lifetime.
/// Holding it does two things: it guarantees every reply reaches *this* process —
/// no other socket can bind the port while it is open — and it fails loudly, up
/// front, if the port is already taken, rather than letting discovery quietly come
/// up empty later. See ``DiscoverError/portUnavailable(port:)``.
///
/// ``poll(_:)`` may be called as often as needed on a held port, which is what a
/// long-running client wants: re-poll to notice Profilers appearing and
/// disappearing, without ever letting go of the port in between.
///
/// ```swift
/// let port = try DiscoveryPort()
/// defer { port.close() }
/// let replies = try await port.poll()
/// ```
///
/// This is a plain BSD socket rather than a `Network.framework` listener on
/// purpose. Sharing the port is the thing to prevent, and `NWParameters` offers
/// only `allowLocalEndpointReuse` — which sets `SO_REUSEPORT` and so invites
/// exactly the silent reply-stealing this type exists to rule out. A raw socket
/// is the only way to *decline* to share.
public final class DiscoveryPort: @unchecked Sendable {
    /// The port held.
    public let port: UInt16

    private let fd: Int32
    private let lock = NSLock()
    private var closed = false

    /// Take exclusive ownership of `port`.
    ///
    /// - Throws: ``DiscoverError/portUnavailable(port:)`` if another process
    ///   already holds it; ``DiscoverError/listenerFailed(_:)`` for any other
    ///   socket failure.
    public init(port: UInt16 = Generated.port) throws {
        self.port = port

        let handle = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP)
        guard handle >= 0 else {
            throw DiscoverError.listenerFailed("socket(): \(DiscoveryPort.errnoText())")
        }

        // Broadcast is required to reach 255.255.255.255 and the per-interface
        // subnet broadcasts. Note what is *not* set: neither SO_REUSEADDR nor
        // SO_REUSEPORT, so the bind below is exclusive.
        var on: Int32 = 1
        guard
            setsockopt(
                handle, SOL_SOCKET, SO_BROADCAST, &on, socklen_t(MemoryLayout<Int32>.size)) == 0
        else {
            let text = DiscoveryPort.errnoText()
            Darwin.close(handle)
            throw DiscoverError.listenerFailed("SO_BROADCAST: \(text)")
        }

        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = port.bigEndian
        address.sin_addr = in_addr(s_addr: in_addr_t(0))  // INADDR_ANY
        let bound = withUnsafePointer(to: &address) { raw in
            raw.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.bind(handle, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard bound == 0 else {
            let code = errno
            let text = DiscoveryPort.errnoText(code)
            Darwin.close(handle)
            // The whole point of the exclusive bind: a port already in use is a
            // conflict to report, not a condition to work around.
            if code == EADDRINUSE { throw DiscoverError.portUnavailable(port: port) }
            throw DiscoverError.listenerFailed("bind(): \(text)")
        }

        self.fd = handle
    }

    deinit { close() }

    /// Release the port. Safe to call more than once.
    public func close() {
        lock.lock()
        defer { lock.unlock() }
        guard !closed else { return }
        closed = true
        Darwin.close(fd)
    }

    /// Broadcast the poll and gather replies until the listen window ends.
    ///
    /// Returns one reply per distinct source address (last payload wins). The
    /// device answers from a fresh ephemeral port each poll, so keying by full
    /// source address would report one "device" per reply.
    public func poll(
        _ options: DiscoveryOptions = DiscoveryOptions()
    ) async throws -> [DiscoveryReply] {
        let collector = ReplyCollector()
        let packet = KemperProtocol.buildPollRequest(mac: options.mac)
        let targets = Discovery.broadcastTargets(extra: options.extraTargets)
        let cancelled = CancelFlag()

        try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation {
                (continuation: CheckedContinuation<Void, Error>) in
                DiscoveryPort.queue.async {
                    do {
                        try self.run(
                            packet: packet, targets: targets, options: options,
                            collector: collector, cancelled: cancelled)
                        continuation.resume()
                    } catch {
                        continuation.resume(throwing: error)
                    }
                }
            }
        } onCancel: {
            cancelled.set()
        }

        return collector.replies()
    }

    // MARK: - The blocking poll loop

    /// Runs on ``queue``: send a round, wait for readability, drain, repeat until
    /// the window closes. Blocking here keeps it off the cooperative pool.
    private func run(
        packet: [UInt8],
        targets: [String],
        options: DiscoveryOptions,
        collector: ReplyCollector,
        cancelled: CancelFlag
    ) throws {
        let deadline = Date().addingTimeInterval(options.listenFor)
        var nextPoll = Date.distantPast
        var buffer = [UInt8](repeating: 0, count: 2048)

        while true {
            if cancelled.isSet { throw DiscoverError.cancelled }
            let now = Date()
            if now >= deadline { break }

            if now >= nextPoll {
                try sendRound(packet: packet, targets: targets)
                nextPoll = now.addingTimeInterval(options.repeatEvery)
            }

            // Wake for whichever comes first: the next poll or the deadline.
            let window = min(deadline, nextPoll).timeIntervalSince(Date())
            var descriptor = pollfd(fd: fd, events: Int16(POLLIN), revents: 0)
            let ready = Darwin.poll(&descriptor, 1, Int32(max(0, window * 1000).rounded()))
            if ready < 0 {
                if errno == EINTR { continue }
                throw DiscoverError.listenerFailed("poll(): \(DiscoveryPort.errnoText())")
            }
            if ready == 0 { continue }

            var from = sockaddr_in()
            var fromLength = socklen_t(MemoryLayout<sockaddr_in>.size)
            let received = withUnsafeMutablePointer(to: &from) { raw in
                raw.withMemoryRebound(to: sockaddr.self, capacity: 1) { sender in
                    buffer.withUnsafeMutableBytes {
                        recvfrom($0.baseAddress, $0.count, 0, sender, &fromLength, on: fd)
                    }
                }
            }
            if received < 0 {
                if errno == EINTR || errno == EAGAIN { continue }
                throw DiscoverError.listenerFailed("recvfrom(): \(DiscoveryPort.errnoText())")
            }
            guard received > 0 else { continue }

            collector.record(
                host: Discovery.ipv4String(from.sin_addr.s_addr),
                payload: Array(buffer[0..<received]))
        }
    }

    /// Send the poll to every target. One unreachable target (a firewall denying
    /// global broadcast, say) must not abort the sweep — only a total failure does.
    private func sendRound(packet: [UInt8], targets: [String]) throws {
        var sent = 0
        var lastError = ""
        for target in targets {
            var address = sockaddr_in()
            address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
            address.sin_family = sa_family_t(AF_INET)
            address.sin_port = port.bigEndian
            guard inet_pton(AF_INET, target, &address.sin_addr) == 1 else {
                lastError = "\(target): not an IPv4 address"
                continue
            }
            let result = withUnsafePointer(to: &address) { raw in
                raw.withMemoryRebound(to: sockaddr.self, capacity: 1) { destination in
                    packet.withUnsafeBytes {
                        sendto(
                            fd, $0.baseAddress, $0.count, 0, destination,
                            socklen_t(MemoryLayout<sockaddr_in>.size))
                    }
                }
            }
            if result < 0 {
                lastError = "\(target): \(DiscoveryPort.errnoText())"
            } else {
                sent += 1
            }
        }
        if sent == 0 && !lastError.isEmpty { throw DiscoverError.sendFailed(lastError) }
    }

    private static let queue = DispatchQueue(label: "com.libkp.discovery")

    private static func errnoText(_ code: Int32 = errno) -> String {
        String(cString: strerror(code))
    }
}

/// `recvfrom` with the buffer arguments first, so the pointer dances above nest
/// in a readable order.
private func recvfrom(
    _ buffer: UnsafeMutableRawPointer?,
    _ count: Int,
    _ flags: Int32,
    _ sender: UnsafeMutablePointer<sockaddr>,
    _ senderLength: UnsafeMutablePointer<socklen_t>,
    on fd: Int32
) -> Int {
    Darwin.recvfrom(fd, buffer, count, flags, sender, senderLength)
}

/// A cancellation bit shared between the task and the blocking loop.
private final class CancelFlag: @unchecked Sendable {
    private let lock = NSLock()
    private var flag = false

    func set() {
        lock.lock()
        flag = true
        lock.unlock()
    }

    var isSet: Bool {
        lock.lock()
        defer { lock.unlock() }
        return flag
    }
}

/// UDP discovery: broadcast the poll, collect Profiler replies.
///
/// Discovery and the TCP session share port 5727. The client broadcasts a fixed
/// poll packet; Profilers on the LAN reply on the same port. The exchange was
/// established by observed experimentation.
public enum Discovery {
    /// Acquire the discovery port, poll once, and release it.
    ///
    /// A convenience for one-shot callers such as a CLI. Anything that goes on to
    /// open a session should hold a ``DiscoveryPort`` across the session instead,
    /// so no other process can take the port midway through.
    public static func discover(
        _ options: DiscoveryOptions = DiscoveryOptions()
    ) async throws -> [DiscoveryReply] {
        let port = try DiscoveryPort()
        defer { port.close() }
        return try await port.poll(options)
    }

    /// Convenience: discover for `listenFor` seconds and return the first device
    /// found.
    public static func findFirst(listenFor: TimeInterval = 3) async throws -> DiscoveryReply? {
        var options = DiscoveryOptions()
        options.listenFor = listenFor
        return try await discover(options).first
    }

    /// Broadcast targets: the global broadcast address, every local IPv4
    /// interface's subnet-broadcast address, plus any caller-supplied extras.
    static func broadcastTargets(extra: [String]) -> [String] {
        var targets = ["255.255.255.255"]
        for address in localBroadcastAddresses() where !targets.contains(address) {
            targets.append(address)
        }
        for address in extra where !targets.contains(address) {
            targets.append(address)
        }
        return targets
    }

    /// Every non-loopback IPv4 interface's subnet-broadcast address.
    static func localBroadcastAddresses() -> [String] {
        var head: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&head) == 0, let first = head else { return [] }
        defer { freeifaddrs(head) }

        var out = [String]()
        var cursor: UnsafeMutablePointer<ifaddrs>? = first
        while let entry = cursor {
            defer { cursor = entry.pointee.ifa_next }
            let flags = Int32(entry.pointee.ifa_flags)
            guard flags & IFF_UP != 0, flags & IFF_LOOPBACK == 0 else { continue }
            guard let rawAddress = entry.pointee.ifa_addr,
                rawAddress.pointee.sa_family == UInt8(AF_INET),
                let rawMask = entry.pointee.ifa_netmask
            else { continue }

            let address = rawAddress.withMemoryRebound(to: sockaddr_in.self, capacity: 1) {
                $0.pointee.sin_addr.s_addr
            }
            let mask = rawMask.withMemoryRebound(to: sockaddr_in.self, capacity: 1) {
                $0.pointee.sin_addr.s_addr
            }
            let broadcast = address | ~mask
            let text = ipv4String(broadcast)
            if !out.contains(text) { out.append(text) }
        }
        return out
    }

    /// Format a network-byte-order IPv4 address.
    static func ipv4String(_ networkOrder: in_addr_t) -> String {
        let host = UInt32(bigEndian: networkOrder)
        return "\((host >> 24) & 0xFF).\((host >> 16) & 0xFF).\((host >> 8) & 0xFF).\(host & 0xFF)"
    }
}

/// Collects replies keyed by source host, filtering out poll packets.
///
/// A poll carries a `POLL` field; a device reply carries `NAME`/`SER#`/…, so
/// recording a poll would report a random client's address as the Profiler.
final class ReplyCollector: @unchecked Sendable {
    private let lock = NSLock()
    private var byHost: [String: [UInt8]] = [:]

    func record(host: String, payload: [UInt8]) {
        guard !ReplyCollector.isPoll(payload) else { return }
        lock.lock()
        byHost[host] = payload
        lock.unlock()
    }

    func replies() -> [DiscoveryReply] {
        lock.lock()
        defer { lock.unlock() }
        return byHost.keys.sorted().map { DiscoveryReply(host: $0, payload: byHost[$0]!) }
    }

    /// Whether a packet is a discovery poll (ours, echoed back, or another
    /// client's) rather than a device reply.
    static func isPoll(_ packet: [UInt8]) -> Bool {
        guard let stream = try? TagStream.parse(packet) else { return false }
        return stream.keyValues().contains { $0.key == "POLL" }
    }
}
