import Foundation
import Network

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

/// UDP discovery: broadcast the poll, collect Profiler replies.
///
/// Discovery and the TCP session share one port (5727). The client broadcasts a
/// fixed poll packet; Profilers on the LAN reply on the same port. The exchange
/// was established by observed experimentation.
public enum Discovery {
    /// Broadcast the discovery poll and gather replies until the listen window
    /// ends. Returns one reply per distinct source address (last payload wins).
    public static func discover(
        _ options: DiscoveryOptions = DiscoveryOptions()
    ) async throws -> [DiscoveryReply] {
        let collector = ReplyCollector()
        let poll = KemperProtocol.buildPollRequest(mac: options.mac)
        let targets = broadcastTargets(extra: options.extraTargets)

        let listener = try makeListener(collector: collector)
        defer { listener.cancel() }

        let senders = targets.map { makeSender(to: $0, collector: collector) }
        defer { senders.forEach { $0.cancel() } }

        let deadline = Date().addingTimeInterval(options.listenFor)
        while Date() < deadline {
            for sender in senders {
                sender.send(content: Data(poll), completion: .contentProcessed { _ in })
            }
            let wait = min(options.repeatEvery, max(deadline.timeIntervalSinceNow, 0))
            if wait <= 0 { break }
            try? await Task.sleep(nanoseconds: UInt64(wait * 1_000_000_000))
            if Task.isCancelled { throw DiscoverError.cancelled }
        }
        return collector.replies()
    }

    /// Convenience: discover for `listenFor` seconds and return the first device
    /// found.
    public static func findFirst(listenFor: TimeInterval = 3) async throws -> DiscoveryReply? {
        var options = DiscoveryOptions()
        options.listenFor = listenFor
        return try await discover(options).first
    }

    // MARK: - Transport plumbing

    private static func makeListener(collector: ReplyCollector) throws -> NWListener {
        let parameters = NWParameters.udp
        parameters.allowLocalEndpointReuse = true
        guard let port = NWEndpoint.Port(rawValue: Generated.port) else {
            throw DiscoverError.listenerFailed("invalid port")
        }
        let listener: NWListener
        do {
            listener = try NWListener(using: parameters, on: port)
        } catch {
            throw DiscoverError.listenerFailed(error.localizedDescription)
        }
        listener.newConnectionHandler = { connection in
            connection.start(queue: discoveryQueue)
            connection.receiveMessage { data, _, _, _ in
                if let data, !data.isEmpty {
                    collector.record(
                        host: endpointHost(connection.endpoint), payload: [UInt8](data))
                }
                connection.cancel()
            }
        }
        listener.start(queue: discoveryQueue)
        return listener
    }

    private static func makeSender(to host: String, collector: ReplyCollector) -> NWConnection {
        let parameters = NWParameters.udp
        parameters.allowLocalEndpointReuse = true
        // Poll from the shared port so a device that answers the source port is
        // heard by the listener bound there.
        if let localPort = NWEndpoint.Port(rawValue: Generated.port) {
            parameters.requiredLocalEndpoint = .hostPort(host: .ipv4(.any), port: localPort)
        }
        let connection = NWConnection(
            host: NWEndpoint.Host(host),
            port: NWEndpoint.Port(rawValue: Generated.port) ?? .any,
            using: parameters
        )
        connection.start(queue: discoveryQueue)
        receiveLoop(connection, collector: collector)
        return connection
    }

    /// Some stacks deliver the reply on the sending socket rather than the
    /// listener; drain it either way.
    private static func receiveLoop(_ connection: NWConnection, collector: ReplyCollector) {
        connection.receiveMessage { data, _, _, error in
            if let data, !data.isEmpty {
                collector.record(host: endpointHost(connection.endpoint), payload: [UInt8](data))
            }
            guard error == nil else { return }
            receiveLoop(connection, collector: collector)
        }
    }

    private static let discoveryQueue = DispatchQueue(label: "com.libkp.discovery")

    private static func endpointHost(_ endpoint: NWEndpoint) -> String {
        switch endpoint {
        case let .hostPort(host, _):
            switch host {
            case let .ipv4(address):
                return "\(address)".components(separatedBy: "%").first ?? "\(address)"
            case let .ipv6(address):
                return "\(address)".components(separatedBy: "%").first ?? "\(address)"
            case let .name(name, _): return name
            @unknown default: return "\(host)"
            }
        default:
            return "\(endpoint)"
        }
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
    private static func ipv4String(_ networkOrder: in_addr_t) -> String {
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
