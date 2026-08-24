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
    /// The discovery port is already held by another process.
    ///
    /// LibKP takes UDP ``Generated/port`` exclusively for as long as a session is
    /// active. The device answers a poll only on that port, and the kernel hands
    /// each arriving reply to exactly one of the sockets bound to it — so a second
    /// listener takes replies rather than seeing a copy. Binding exclusively
    /// surfaces the clash here, at start-up, instead of as a device that
    /// intermittently "cannot be found".
    ///
    /// The usual holder is other Kemper software on the same machine — Rig Manager
    /// keeps the port open for its whole run — which must be quit first.
    case portUnavailable(port: UInt16)
    /// No poll could be sent to any target.
    case sendFailed(String)
    /// The discovery run was cancelled before it finished.
    case cancelled
}

extension DiscoverError: CustomStringConvertible {
    public var description: String {
        switch self {
        case let .listenerFailed(detail): return "failed to open the discovery listener: \(detail)"
        case let .portUnavailable(port):
            return """
                UDP port \(port) is already held by another application. LibKP needs \
                exclusive use of it while a session is active; quit any other Kemper \
                software (Rig Manager keeps this port open) and try again.
                """
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
    /// The stream link is not open, so no command can be written.
    case disconnected
    /// An effect-slot name did not match A/B/C/D/X/MOD/DLY/REV.
    case unknownSlot(String)
}

extension CommandError: CustomStringConvertible {
    public var description: String {
        switch self {
        case .disconnected: return "device model is disconnected; the stream link is closed"
        case let .unknownSlot(name):
            return "unknown effect slot \"\(name)\"; use A B C D X MOD DLY REV"
        }
    }
}

/// Why a read-only request on the stream went unanswered.
///
/// Requests travel the model's request lane: each one is sent, waits for a
/// value at its address, and is reported here rather than retried. See
/// ``DeviceModel/requestParam(page:number:)``.
public enum RequestError: Error, Equatable, Sendable {
    /// The stream link is not open, so nothing could be sent.
    case disconnected
    /// No value landed at the address inside ``Generated/requestTimeoutMs``.
    /// The request is never resent: the device ignores a request for an
    /// address it does not have, and a second copy would only cost it more.
    case timeout
    /// The address is one the stream never answers — a `wire = "control"` row
    /// of the routing table, which is the morph position — so nothing was
    /// sent. It reaches the tree through the control link instead.
    case unreadable
}

extension RequestError: CustomStringConvertible {
    public var description: String {
        switch self {
        case .disconnected: return "the stream link is not open; request not sent"
        case .timeout:
            return "no reply within \(Generated.requestTimeoutMs) ms; the request is not retried"
        case .unreadable:
            return "the address is only carried by the control channel; it cannot be requested"
        }
    }
}

/// Why ``DeviceModel/reopenControl()`` did not open the control link.
public enum ChannelError: Error, Sendable {
    /// The model was connected with ``ControlPolicy/off``; there is no control
    /// link to reopen.
    case off
    /// The last control open was less than ``Generated/controlReopenMinGapMs``
    /// ago. The device wedges under session churn, so the gap is never
    /// shortened, whoever asks.
    case tooSoon
    /// The stream link is not open. The control link only ever runs beside a
    /// live stream; reconnecting the stream brings it back on its own.
    case disconnected
    /// The open itself failed: the dial, the handshake, the preamble or the
    /// dump trigger. The control link is ``ChannelState/unavailable``.
    case session(SessionError)
}

extension ChannelError: CustomStringConvertible {
    public var description: String {
        switch self {
        case .off: return "the control link is off for this model"
        case .tooSoon:
            return
                "the control link was opened less than \(Generated.controlReopenMinGapMs) ms ago"
        case .disconnected: return "the stream link is not open; the control link needs it"
        case let .session(error): return "control link open failed: \(error)"
        }
    }
}
