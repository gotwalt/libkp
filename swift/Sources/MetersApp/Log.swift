import Foundation
import LibKP

// Tracing for the connect cycle, the commands this app sends, and the events
// that come back — enough to reconstruct, after the fact, who moved the rig.

/// Timestamped tracing, written to stderr — visible both under `swift run
/// MetersApp` and in Xcode's console.
///
/// Only the lines that say *what moved* are on by default. The raw event stream
/// underneath them is enormous — one rig change is ~1500 `paramChanged` and
/// `stringTag` replies, and the meter lane alone runs a few hundred messages a
/// second — so all of it is gated behind `KP_LOG_VERBOSE` in the environment.
/// What survives is the connect cycle, the commands this app sends, and the
/// handful of snapshot fields the rig navigator actually reads.
enum Log {
    /// The lane a line belongs to, printed as a fixed-width column.
    enum Lane: String {
        /// The connect / retry cycle.
        case conn
        /// A command this app sent to the device.
        case cmd
        /// Something that arrived from the device.
        case evt
        /// A published store property changing.
        case ui
    }

    /// Whether the high-rate lane is logged too.
    static let verbose = ProcessInfo.processInfo.environment["KP_LOG_VERBOSE"] != nil

    /// Zero for the elapsed-time column.
    private static let start = Date()
    /// Serializes writes so interleaved lanes stay whole.
    private static let lock = NSLock()

    /// Log a connect-cycle line.
    static func conn(_ message: @autoclosure () -> String) { write(.conn, message()) }
    /// Log a command this app is sending.
    static func cmd(_ message: @autoclosure () -> String) { write(.cmd, message()) }
    /// Log something the device sent.
    static func evt(_ message: @autoclosure () -> String) { write(.evt, message()) }
    /// Log a published store property changing.
    static func ui(_ message: @autoclosure () -> String) { write(.ui, message()) }

    /// Render an optional as its value or an em-dash, so a `nil` bank reads the
    /// same in the log as it does on the stepper.
    static func opt<Value>(_ value: Value?) -> String {
        value.map { "\($0)" } ?? "—"
    }

    /// A one-line rendering of a device event, or `nil` for the ones that say
    /// nothing the snapshot deltas do not already say more legibly.
    ///
    /// A rig load replays the whole parameter tree — every effect slot, every
    /// string tag, the amp and cabinet pages — as individual replies. Printing
    /// them buries the two lines that matter (the rig changed; the bank preview
    /// moved), so the raw stream is ``verbose``-only.
    static func describe(_ event: DeviceEvent) -> String? {
        switch event {
        case .status, .beatPulse, .tunerDeviance, .tunerNote:
            return verbose ? "\(event)" : nil
        case let .stringTag(number):
            return verbose ? "stringTag \(number)" : nil
        case let .bankPreview(number):
            return verbose ? "bankPreview \(number)" : nil
        case let .effectChanged(slot):
            return verbose ? "effectChanged slot \(slot)" : nil
        case let .paramChanged(page, number, value):
            return verbose ? "paramChanged \(hex(page))/\(number) = \(value)" : nil
        case let .renderedString(page, number, value, text):
            return verbose ? "rendered \(hex(page))/\(number) \(value) = \"\(text)\"" : nil
        case .rigChanged:
            // The store logs this one itself, with how long it has been since
            // our own last navigation — the part that says who caused it.
            return nil
        case let .tempoBpm(bpm):
            return "tempoBpm \(bpm)"
        case let .morphChanged(value):
            return "morph \(value)"
        case let .morphButton(on):
            return "morphButton \(on ? "press" : "release")"
        case let .currentPosition(bank, slot):
            return "currentPosition bank \(opt(bank)) slot \(opt(slot))"
        case .connected:
            return "connected"
        case .disconnected:
            return "disconnected"
        case let .connectionChanged(connection):
            return "connection \(connection)"
        case let .channelChanged(channel, state):
            return "channel \(channel) \(state)"
        case let .syncCompleted(source):
            return "sync completed (\(source))"
        case let .requestTimedOut(address):
            return "request timed out at \(address)"
        }
    }

    /// An NRPN page as `$xx`, the way the docs write it.
    private static func hex(_ byte: UInt8) -> String {
        "$" + String(byte, radix: 16, uppercase: true).leftPadded(to: 2, with: "0")
    }

    private static func write(_ lane: Lane, _ message: String) {
        let elapsed = String(format: "%8.3f", Date().timeIntervalSince(start))
        let column = lane.rawValue.padding(toLength: 4, withPad: " ", startingAt: 0)
        lock.lock()
        defer { lock.unlock() }
        fputs("[\(elapsed)] \(column) \(message)\n", stderr)
    }
}

extension String {
    /// This string padded on the left to `width`, for fixed-width hex.
    fileprivate func leftPadded(to width: Int, with pad: Character) -> String {
        count >= width ? self : String(repeating: pad, count: width - count) + self
    }
}

extension Phase {
    /// A short, log-friendly name for the phase.
    var label: String {
        switch self {
        case .idle:
            return "idle"
        case .discovering:
            return "discovering"
        case let .connecting(host):
            return "connecting(\(host))"
        case let .connected(host, name):
            return "connected(\(host)\(name.map { " · \($0)" } ?? ""))"
        case let .failed(message):
            return "failed(\(message))"
        }
    }
}
