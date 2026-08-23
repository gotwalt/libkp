import Foundation
import LibKP
import Network

// A command socket for driving the app from outside it — the same taps the
// buttons make, plus a readout of what the store believes, so a navigation bug
// can be reproduced and inspected without a pair of hands on the trackpad.

/// A line-oriented TCP server on loopback that exposes the rig navigator and the
/// store's position state.
///
/// Off unless `KP_DEBUG_PORT` names a port, so a normal launch opens nothing.
/// It binds `127.0.0.1` only, and every command it accepts is one the UI already
/// offers — it is a remote control for the buttons, not a back door past them.
///
/// One command per line, one reply line per command:
///
/// ```
/// state              the store's position, as JSON
/// bank up|down       step one bank, keeping the slot
/// rig up|down        step one rig
/// slot <1-5>         load a slot in the loaded bank
/// morph on|off       morph the rig, or return it to base
/// reconnect          tear down and reconnect
/// ```
///
/// ```sh
/// KP_DEBUG_PORT=5799 swift run MetersApp
/// printf 'bank up\nstate\n' | nc 127.0.0.1 5799
/// ```
@MainActor
final class DebugControl {
    /// The environment variable naming the port to listen on.
    static let portVariable = "KP_DEBUG_PORT"

    private let store: DeviceStore
    private let listener: NWListener
    /// Open connections, held so they live as long as the client keeps them.
    private var connections: [ObjectIdentifier: NWConnection] = [:]

    /// Start a server if `KP_DEBUG_PORT` is set and parses as a port, else
    /// return `nil` and leave nothing listening.
    static func fromEnvironment(store: DeviceStore) -> DebugControl? {
        guard let raw = ProcessInfo.processInfo.environment[portVariable] else { return nil }
        guard let number = UInt16(raw.trimmed), let port = NWEndpoint.Port(rawValue: number) else {
            Log.conn("\(portVariable)=\(raw) is not a port — debug control not started")
            return nil
        }
        do {
            return try DebugControl(store: store, port: port)
        } catch {
            Log.conn("debug control could not listen on \(number): \(error)")
            return nil
        }
    }

    private init(store: DeviceStore, port: NWEndpoint.Port) throws {
        self.store = store
        let parameters = NWParameters.tcp
        parameters.requiredLocalEndpoint = .hostPort(host: .ipv4(.loopback), port: port)
        parameters.allowLocalEndpointReuse = true
        // The required local endpoint already names the port; passing it again
        // as `on:` is rejected as a conflicting argument.
        listener = try NWListener(using: parameters)
        listener.newConnectionHandler = { [weak self] connection in
            Task { @MainActor in self?.accept(connection) }
        }
        listener.start(queue: .main)
        Log.conn("debug control listening on 127.0.0.1:\(port.rawValue)")
    }

    private func accept(_ connection: NWConnection) {
        let id = ObjectIdentifier(connection)
        connections[id] = connection
        connection.stateUpdateHandler = { [weak self] state in
            guard case .cancelled = state else { return }
            Task { @MainActor in self?.drop(id) }
        }
        connection.start(queue: .main)
        receive(connection, id: id, buffer: Data())
    }

    private func drop(_ id: ObjectIdentifier) { connections[id] = nil }

    /// Read until a newline, run what came before it, and go again. A partial
    /// line is carried across reads.
    private func receive(_ connection: NWConnection, id: ObjectIdentifier, buffer: Data) {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 4096) {
            [weak self] data, _, isComplete, error in
            Task { @MainActor in
                guard let self else { return }
                guard error == nil else {
                    connection.cancel()
                    self.drop(id)
                    return
                }
                var buffer = buffer
                if let data { buffer.append(data) }
                while let newline = buffer.firstIndex(of: UInt8(ascii: "\n")) {
                    let line = String(decoding: buffer[..<newline], as: UTF8.self)
                    buffer = buffer[buffer.index(after: newline)...]
                    let reply = self.run(line)
                    connection.send(
                        content: Data((reply + "\n").utf8), completion: .idempotent)
                }
                if isComplete {
                    connection.cancel()
                    self.drop(id)
                    return
                }
                self.receive(connection, id: id, buffer: buffer)
            }
        }
    }

    /// Run one command line, returning the single line to send back.
    private func run(_ line: String) -> String {
        let words = line.split(whereSeparator: \.isWhitespace).map(String.init)
        guard let command = words.first?.lowercased() else { return state() }
        let argument = words.count > 1 ? words[1].lowercased() : ""
        Log.cmd("debug control: \(line.trimmed)")
        switch command {
        case "state":
            return state()
        case "bank":
            switch argument {
            case "up": store.stepBank(forward: true)
            case "down": store.stepBank(forward: false)
            default: return "error: bank up|down"
            }
        case "rig":
            switch argument {
            case "up": store.stepRig(by: 1)
            case "down": store.stepRig(by: -1)
            default: return "error: rig up|down"
            }
        case "slot":
            guard let slot = Int(argument), (1...Params.bankSlots).contains(slot) else {
                return "error: slot 1-\(Params.bankSlots)"
            }
            store.selectSlot(slot)
        case "morph":
            switch argument {
            case "on": store.setMorphed(true)
            case "off": store.setMorphed(false)
            default: return "error: morph on|off"
            }
        case "reconnect":
            store.restart()
        default:
            return "error: unknown command \(command)"
        }
        return "ok"
    }

    /// The store's position, as one line of JSON. Deliberately raw: the numbers
    /// the navigator actually reads, not a rendering of them.
    private func state() -> String {
        let fields: [(String, String)] = [
            ("phase", quoted(store.phase.label)),
            ("bank", number(store.bank)),
            ("slot", number(store.slot)),
            ("rig_index", number(store.rigIndex.map(Int.init))),
            ("selected_slot", number(store.selectedSlot)),
            ("device_slot", number(store.deviceSlot)),
            ("highlighted_slot", number(store.highlightedSlot)),
            ("rig_name", quoted(store.state.rig.name?.trimmed ?? "")),
            ("morph", number(store.state.morph.map(Int.init))),
            ("is_morphed", store.isMorphed.map { $0 ? "true" : "false" } ?? "null"),
            (
                "bank_preview",
                "["
                    + store.state.bank.slots
                    .map { quoted($0.rigName?.trimmed ?? "") }
                    .joined(separator: ",") + "]"
            ),
        ]
        return "{" + fields.map { "\"\($0.0)\":\($0.1)" }.joined(separator: ",") + "}"
    }

    private func number(_ value: Int?) -> String { value.map(String.init) ?? "null" }

    private func quoted(_ text: String) -> String {
        let escaped =
            text
            .replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"")
        return "\"\(escaped)\""
    }
}
