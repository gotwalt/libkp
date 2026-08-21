import Foundation

/// A malformed or truncated TagStream payload.
public enum ParseError: Error, Equatable, Sendable {
    /// The payload is shorter than the minimum the parser needs.
    case tooShort(need: Int, got: Int)
    /// A length-prefixed field claims more bytes than the payload has left.
    case fieldOverrun(offset: Int, len: Int, remaining: Int)
}

extension ParseError: CustomStringConvertible {
    public var description: String {
        switch self {
        case let .tooShort(need, got):
            return "payload too short: need at least \(need) bytes, got \(got)"
        case let .fieldOverrun(offset, len, remaining):
            return
                "field at offset \(offset) claims length \(len) but only \(remaining) bytes remain"
        }
    }
}

/// Errors that can arise while discovering Profiler devices on the LAN.
public enum DiscoverError: Error, Sendable {
    /// The UDP listener could not be created or started.
    case listenerFailed(String)
    /// No poll could be sent to any target.
    case sendFailed(String)
    /// The discovery run was cancelled before it finished.
    case cancelled
}

extension DiscoverError: CustomStringConvertible {
    public var description: String {
        switch self {
        case let .listenerFailed(detail): return "failed to open the discovery listener: \(detail)"
        case let .sendFailed(detail): return "failed to send the discovery poll: \(detail)"
        case .cancelled: return "discovery cancelled"
        }
    }
}

/// Errors from the TCP session and its protocol handshake.
public enum SessionError: Error, Sendable {
    /// The TCP connection could not be established.
    case connect(address: String, detail: String)
    /// An I/O error occurred during the named phase.
    case io(phase: String, detail: String)
    /// The named phase did not complete inside its time budget.
    case timeout(phase: String, ms: UInt64)
    /// The device closed the connection.
    case closed
    /// The device answered the protocol selection with a rejection.
    case protocolRejected(name: String, detail: String?)
    /// The device offered no usable protocol in its greeting.
    case noProtocolOffered
}

extension SessionError: CustomStringConvertible {
    public var description: String {
        switch self {
        case let .connect(address, detail): return "failed to connect to \(address): \(detail)"
        case let .io(phase, detail): return "i/o error during \(phase): \(detail)"
        case let .timeout(phase, ms): return "timed out waiting for \(phase) after \(ms) ms"
        case .closed: return "connection closed by device"
        case let .protocolRejected(name, detail):
            return "device rejected protocol \"\(name)\"" + (detail.map { ": \($0)" } ?? "")
        case .noProtocolOffered: return "device offered no protocol in its greeting"
        }
    }
}

/// Error returned when a `DeviceModel` command cannot be issued.
public enum CommandError: Error, Equatable, Sendable {
    /// The ingest task has ended, so no command can be written.
    case disconnected
    /// An effect-slot name did not match A/B/C/D/X/MOD/DLY/REV.
    case unknownSlot(String)
}

extension CommandError: CustomStringConvertible {
    public var description: String {
        switch self {
        case .disconnected: return "device model is disconnected; command channel closed"
        case let .unknownSlot(name):
            return "unknown effect slot \"\(name)\"; use A B C D X MOD DLY REV"
        }
    }
}
