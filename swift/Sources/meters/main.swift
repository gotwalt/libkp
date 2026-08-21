import Dispatch
import Foundation
import LibKP

// `meters` — a live full-screen terminal view of a Profiler's realtime stream.
//
// It connects, performs the read-only initial rig sync, and then renders the
// current patch: rig header, amp and cabinet, the eight effect blocks with
// their on/off state and effect type, the tuner strobe, the level bars, a
// tempo pulse and the last parameter seen.

// MARK: - Command line

struct Options {
    var host: String?
    var showAll = false
    var width = 44
}

enum ArgumentResult {
    case parsed(Options)
    case help
    case invalid(String)
}

func parseArguments(_ arguments: [String]) -> ArgumentResult {
    var options = Options()
    var index = 1
    while index < arguments.count {
        let argument = arguments[index]
        switch argument {
        case "--ip":
            guard index + 1 < arguments.count else { return .invalid("--ip needs an address") }
            options.host = arguments[index + 1]
            index += 2
        case "--all":
            options.showAll = true
            index += 1
        case "--width":
            guard index + 1 < arguments.count, let value = Int(arguments[index + 1]) else {
                return .invalid("--width needs a number")
            }
            options.width = min(max(value, 8), 512)
            index += 2
        case "--help", "-h":
            return .help
        default:
            return .invalid("unknown argument \"\(argument)\"")
        }
    }
    return .parsed(options)
}

let usage = """
    usage: meters [--ip <addr>] [--all] [--width <n>]

      --ip <addr>   device IPv4 address; discovery is used when omitted
      --all         show all eleven raw meter fields, not just the level bars
      --width <n>   bar width in characters (default 44)
    """

// MARK: - Terminal

enum Term {
    static let clear = "\u{1B}[2J"
    static let home = "\u{1B}[H"
    static let hideCursor = "\u{1B}[?25l"
    static let showCursor = "\u{1B}[?25h"
    static let clearLine = "\u{1B}[K"
    static let reset = "\u{1B}[0m"
    static let bold = "\u{1B}[1m"
    static let dim = "\u{1B}[2m"
    static let red = "\u{1B}[31m"
    static let green = "\u{1B}[32m"
    static let yellow = "\u{1B}[33m"
    static let cyan = "\u{1B}[36m"
    static let brightYellow = "\u{1B}[93m"
    static let white = "\u{1B}[97m"

    static func write(_ text: String) {
        FileHandle.standardOutput.write(Data(text.utf8))
    }

    static func enterFullScreen() { write(clear + hideCursor) }
    static func restore() { write(showCursor + reset + "\n") }
}

// MARK: - View model

/// Holds the fast-lane data the renderer needs between snapshots: the meter
/// frame, peak-hold decay, strobe phase history, the beat pulse and the last
/// parameter seen.
actor MeterView {
    private let width: Int
    private let showAll: Bool
    private let host: String

    private var values = [UInt16](repeating: 0, count: 11)
    private var peaks = [Double](repeating: 0, count: 11)
    private var mins = [UInt16](repeating: .max, count: 11)
    private var maxs = [UInt16](repeating: 0, count: 11)
    private var frames: UInt64 = 0
    private var recent: [Date] = []
    private var lastDecay = Date()
    private var lastParam: String?
    private var lastPulse: (at: Date, value: UInt16)?
    private var phaseHistory: [(at: Date, phase: UInt16)] = []

    private static let fullScale = Double(Generated.fullScale)
    private static let rateWindow: TimeInterval = 2

    init(host: String, width: Int, showAll: Bool) {
        self.host = host
        self.width = width
        self.showAll = showAll
    }

    // MARK: Ingest

    func handle(_ event: DeviceEvent) {
        switch event {
        case let .status(status):
            ingest(status)
        case let .beatPulse(on):
            lastPulse = (Date(), on ? Generated.fullScale : 0)
        case let .paramChanged(page, number, value):
            lastParam = "\(Params.describeNumeric(page: page, number: number)) = \(value)"
        case let .tempoBpm(bpm):
            lastParam = "Rig Settings: Tempo bpm = \(bpm)"
        case let .morphChanged(value):
            lastParam = "Page 0: Morph State = \(value)"
        case let .tunerNote(note):
            lastParam = "Looper/Freeze: Tuner Note = \(note)"
        case let .renderedString(page, number, _, text):
            lastParam = "\(Params.describe(page: page, number: number)) = \(text)"
        case let .effectChanged(slot):
            lastParam = "effect slot \(Params.effectSlots[slot].name) changed"
        case .rigChanged:
            lastParam = "rig changed"
        default:
            break
        }
    }

    private func ingest(_ status: RealtimeStatus) {
        frames += 1
        recent.append(Date())
        for (i, value) in status.raw.enumerated() {
            values[i] = value
            peaks[i] = max(peaks[i], Double(value))
            mins[i] = min(mins[i], value)
            maxs[i] = max(maxs[i], value)
        }
        if status.strobeActive {
            phaseHistory.append((Date(), status.strobePhase))
        } else {
            phaseHistory.removeAll(keepingCapacity: true)
        }
    }

    // MARK: Derived values

    /// Messages/sec over the trailing window.
    private func rate() -> Double {
        let cutoff = Date().addingTimeInterval(-MeterView.rateWindow)
        recent.removeAll { $0 < cutoff }
        return Double(recent.count) / MeterView.rateWindow
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
            let delta = ((b - a + 8192) %% 16384) - 8192
            total += Double(delta)
        }
        let dt = last.at.timeIntervalSince(first.at)
        return dt > 0.05 ? total / dt : nil
    }

    /// Decay the peak-hold markers, frame-rate independent, never below the
    /// current value.
    private func decay() {
        let now = Date()
        let dt = min(now.timeIntervalSince(lastDecay), 0.5)
        lastDecay = now
        let drop = MeterView.fullScale * 0.8 * dt  // full-scale peak falls in ~1.25 s
        for i in peaks.indices {
            peaks[i] = max(peaks[i] - drop, Double(values[i]))
        }
    }

    // MARK: Render

    func render(_ state: DeviceState) {
        decay()
        var out = Term.home
        out += header(state)
        out += blocks(state)
        out += strobeRow()
        out += meterRows()
        out += footer(state)
        Term.write(out)
    }

    private func header(_ state: DeviceState) -> String {
        let rigName = state.rig.name?.trimmed.nonEmpty ?? "—"
        let author = state.rig.author?.trimmed.nonEmpty
        let tempo = state.rig.tempoBpm.map { "\($0) BPM" }
        let live =
            state.connection == .connected
            ? "\(Term.green)●\(Term.reset)"
            : "\(Term.red)○\(Term.reset)"

        var out = "\(Term.bold)KEMPER LIVE\(Term.reset)  \(live) \(Term.dim)\(host)\(Term.reset)"
        out += "   \(Term.dim)frames \(frames) (\(String(format: "%.1f", rate()))/s)\(Term.reset)"
        out += Term.clearLine + "\n" + Term.clearLine + "\n"

        out += "  \(Term.bold)\(Term.cyan)\(rigName)\(Term.reset)"
        if let author { out += "  \(Term.dim)by \(author)\(Term.reset)" }
        if let tempo { out += "  \(Term.dim)· \(tempo)\(Term.reset)" }
        out += Term.clearLine + "\n"

        let amp = state.amp.name?.trimmed.nonEmpty ?? "—"
        let cab = state.cabinet.name?.trimmed.nonEmpty ?? "—"
        let ampMark = mark(state.amp.on)
        out += "  \(Term.dim)amp\(Term.reset) \(ampMark) \(amp)"
        out += "   \(Term.dim)cab\(Term.reset) \(cab)"
        if let gain = state.amp.gain {
            out += "   \(Term.dim)gain \(percent(gain))\(Term.reset)"
        }
        out += Term.clearLine + "\n" + Term.clearLine + "\n"
        return out
    }

    /// The eight effect blocks A B C D X MOD DLY REV.
    private func blocks(_ state: DeviceState) -> String {
        var out = "  \(Term.dim)blocks\(Term.reset)" + Term.clearLine + "\n"
        for effect in state.effects {
            let indicator = mark(effect.on)
            let type = effect.typeName ?? (effect.kind.map { "type \($0)" } ?? "—")
            let name = effect.isEmpty ? "\(Term.dim)—\(Term.reset)" : type
            let label = effect.slot.padding(toLength: 3, withPad: " ", startingAt: 0)
            out += "    \(indicator) \(Term.bold)\(label)\(Term.reset)  \(name)"
            if let mix = effect.mix {
                out += "  \(Term.dim)mix \(percent(mix))\(Term.reset)"
            }
            out += Term.clearLine + "\n"
        }
        out += Term.clearLine + "\n"
        return out
    }

    /// Indicator glyph: ● on, ○ off, · unknown.
    private func mark(_ on: Bool?) -> String {
        switch on {
        case .some(true): return "\(Term.green)●\(Term.reset)"
        case .some(false): return "\(Term.dim)○\(Term.reset)"
        case nil: return "\(Term.dim)·\(Term.reset)"
        }
    }

    private func percent(_ value: UInt16) -> String {
        String(format: "%.0f%%", Double(value) / MeterView.fullScale * 100)
    }

    /// The tuner strobe: v3 is a wrapping phase whose rotation rate tracks pitch
    /// deviance (stationary = in tune). Render a drifting marker plus a verdict.
    private func strobeRow() -> String {
        let active = values[0...Generated.strobePhaseIndex].contains { $0 > 0 }
        let drift = strobeRate()
        let verdict: String
        let markerColor: String
        switch (active, drift) {
        case (false, _):
            verdict = "\(Term.dim)idle\(Term.reset)"
            markerColor = Term.dim
        case let (true, .some(r)) where abs(r) < 600:
            verdict = "\(Term.green)● in tune\(Term.reset)"
            markerColor = Term.green
        case let (true, .some(r)) where r < 0:
            verdict = "\(Term.red)← sharp\(Term.reset)"
            markerColor = Term.red
        case (true, .some):
            verdict = "\(Term.cyan)→ flat\(Term.reset)"
            markerColor = Term.cyan
        case (true, .none):
            verdict = "\(Term.dim)…\(Term.reset)"
            markerColor = Term.dim
        }

        let position = Int(
            Double(values[Generated.strobePhaseIndex]) / MeterView.fullScale
                * Double(width - 1))
        var strobe = "\(Term.dim)[\(Term.reset)"
        for i in 0..<width {
            if active && i == position {
                strobe += "\(markerColor)◆\(Term.reset)"
            } else {
                strobe += "\(Term.dim)·\(Term.reset)"
            }
        }
        strobe += "\(Term.dim)]\(Term.reset)"
        return "  \(pad("tuner (strobe)", 19)) \(strobe)  \(verdict)" + Term.clearLine + "\n"
    }

    private func meterRows() -> String {
        var out = ""
        for field in Params.meterFields {
            guard showAll || field.render == "bar" else { continue }
            let value = values[field.index]
            let low = mins[field.index] <= maxs[field.index] ? mins[field.index] : 0
            let high = mins[field.index] <= maxs[field.index] ? maxs[field.index] : 0
            let frac = Double(value) / MeterView.fullScale
            let peakFrac = peaks[field.index] / MeterView.fullScale
            out += "  \(pad(label(for: field), 19)) \(bar(frac, peakFrac))"
            out += "  \(pad(String(value), 5, right: true))"
            out +=
                "  \(Term.dim)range \(pad(String(low), 5, right: true))–\(pad(String(high), 5))\(Term.reset)"
            out += Term.clearLine + "\n"
        }
        return out
    }

    /// Short, column-friendly labels for the meter rows.
    private static let shortLabels: [String: String] = [
        "stack_level": "stack (pre-vol)",
        "stack_power": "stack power",
        "rig_out_level": "rig out (post-vol)",
        "rig_out_power": "rig out power",
        "loudness": "loudness (rms)",
        "strobe_seg_low": "strobe seg lo",
        "strobe_seg_mid": "strobe seg mid",
        "strobe_seg_high": "strobe seg hi",
        "strobe_phase": "strobe phase",
    ]

    private func label(for field: MeterField) -> String {
        let short =
            MeterView.shortLabels[field.id] ?? field.id.replacingOccurrences(of: "_", with: " ")
        return showAll ? "v\(field.index) \(short)" : short
    }

    private func footer(_ state: DeviceState) -> String {
        var out = Term.clearLine + "\n"
        let tempo: String
        switch lastPulse {
        case let .some(pulse) where Date().timeIntervalSince(pulse.at) < 0.15 && pulse.value > 8192:
            tempo = "\(Term.brightYellow)♩ ●\(Term.reset)"
        case .some:
            tempo = "♩ ·"
        case .none:
            tempo = "♩ \(Term.dim)(no pulse seen)\(Term.reset)"
        }
        out += "  tempo: \(tempo)"
        if let bpm = state.rig.tempoBpm { out += "  \(Term.dim)\(bpm) BPM\(Term.reset)" }
        if let morph = state.morph { out += "   \(Term.dim)morph \(percent(morph))\(Term.reset)" }
        out += Term.clearLine + "\n"

        out += "  last param: \(lastParam ?? "(none)")" + Term.clearLine + "\n"
        out += "  \(Term.dim)(play into the device — Ctrl-C to quit; --all for raw fields)"
        out += "\(Term.reset)" + Term.clearLine + "\n"
        out += "\u{1B}[J"  // clear anything below
        return out
    }

    /// A colored bar with a peak-hold marker. Green → yellow → red by position.
    private func bar(_ frac: Double, _ peakFrac: Double) -> String {
        let clamped = min(max(frac, 0), 1)
        let filled = Int((clamped * Double(width)).rounded())
        let peakCell = min(Int((min(max(peakFrac, 0), 1) * Double(width)).rounded()), width - 1)

        var out = "\(Term.dim)[\(Term.reset)"
        for i in 0..<width {
            let position = (Double(i) + 0.5) / Double(width)
            if i == peakCell && peakFrac > 0.01 {
                out += "\(Term.white)│\(Term.reset)"
            } else if i < filled {
                let color = position < 0.6 ? Term.green : (position < 0.85 ? Term.yellow : Term.red)
                out += "\(color)█\(Term.reset)"
            } else {
                out += "\(Term.dim)·\(Term.reset)"
            }
        }
        out += "\(Term.dim)]\(Term.reset)"
        return out
    }

    /// Pad (or truncate) to an exact column width so the rows stay aligned.
    private func pad(_ text: String, _ width: Int, right: Bool = false) -> String {
        if text.count > width { return String(text.prefix(width)) }
        let padding = String(repeating: " ", count: width - text.count)
        return right ? padding + text : text + padding
    }
}

/// Euclidean remainder, so a negative dividend still wraps forward.
infix operator %% : MultiplicationPrecedence
func %% (lhs: Int, rhs: Int) -> Int {
    let r = lhs % rhs
    return r < 0 ? r + rhs : r
}

extension String {
    var trimmed: String { trimmingCharacters(in: .whitespacesAndNewlines) }
    var nonEmpty: String? { isEmpty ? nil : self }
}

// MARK: - Entry point

func fail(_ message: String) -> Never {
    Term.restore()
    FileHandle.standardError.write(Data("error: \(message)\n".utf8))
    exit(1)
}

let options: Options
switch parseArguments(CommandLine.arguments) {
case let .parsed(parsed):
    options = parsed
case .help:
    FileHandle.standardOutput.write(Data((usage + "\n").utf8))
    exit(0)
case let .invalid(message):
    FileHandle.standardError.write(Data("error: \(message)\n\n\(usage)\n".utf8))
    exit(2)
}

// Restore the terminal on Ctrl-C rather than leaving the cursor hidden.
signal(SIGINT, SIG_IGN)
let interrupt = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
interrupt.setEventHandler {
    Term.restore()
    exit(0)
}
interrupt.resume()

let host: String
if let given = options.host {
    host = given
} else {
    FileHandle.standardError.write(Data("No --ip given; discovering...\n".utf8))
    do {
        guard let reply = try await Discovery.findFirst(listenFor: 3) else {
            fail("no device found; pass --ip <addr>")
        }
        if let name = reply.name {
            FileHandle.standardError.write(Data("Discovered \"\(name)\" at \(reply.host)\n".utf8))
        }
        host = reply.host
    } catch {
        fail("discovery failed: \(error)")
    }
}

FileHandle.standardError.write(Data("Connecting to \(host):\(Generated.port) ...\n".utf8))

let model: DeviceModel
do {
    model = try await DeviceModel.connect(host: host)
} catch {
    fail("\(error)")
}

// `DeviceModel.connect` already ran the read-only rig sync (rig/amp/cab names
// and each effect slot's type + on/off). Ask for the handful of extra values
// the header shows; these are requests too, so nothing on the device changes.
for (page, number) in [
    (Generated.ampPage, Generated.ampOnNumber),
    (Generated.ampPage, Generated.gainNumber),
    (Generated.pageRigSettings, Generated.tempoNumber),
    (Generated.pageRigSettings, Generated.rigVolumeNumber),
    (Generated.pageMorph, Generated.morphNumber),
    (Generated.systemPage, Generated.mainVolumeNumber),
] {
    try? await model.requestParam(page: page, number: number)
}

let view = MeterView(host: host, width: options.width, showAll: options.showAll)
Term.enterFullScreen()

// One task drains the granular event stream into the view model...
let eventTask = Task {
    for await event in await model.events() {
        await view.handle(event)
    }
}

// ...while the main loop re-renders from the snapshot at a steady frame rate.
while true {
    let state = await model.snapshot()
    await view.render(state)
    if state.connection == .disconnected { break }
    try? await Task.sleep(nanoseconds: 50_000_000)
}

eventTask.cancel()
Term.restore()
FileHandle.standardError.write(Data("device closed the connection\n".utf8))
