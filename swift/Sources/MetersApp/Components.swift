import LibKP
import SwiftUI

// The reusable pieces the dashboard is assembled from: indicators, bars, the
// block cards, the effect grid, the tuner strobe and the meter list.

// MARK: - Indicators

/// The block on/off indicator — the GUI counterpart of the terminal example's
/// `mark()`: ● on, ○ off, · unknown.
struct StatusDot: View {
    /// The block's state, or `nil` if the device has not reported it yet.
    let on: Bool?

    var body: some View {
        ZStack {
            switch on {
            case .some(true):
                Circle().fill(Color.green)
            case .some(false):
                Circle().strokeBorder(Color.secondary, lineWidth: 1.5)
            case .none:
                Circle().fill(Color.secondary.opacity(0.4)).frame(width: 5, height: 5)
            }
        }
        .frame(width: 9, height: 9)
    }
}

// MARK: - Bars

/// A thin accent-colored bar for secondary values such as gain and effect mix.
struct MiniBar: View {
    /// The fill amount, clamped to 0…1.
    let fraction: Double

    var body: some View {
        GeometryReader { geometry in
            ZStack(alignment: .leading) {
                Capsule().fill(.quaternary)
                Capsule()
                    .fill(Color.accentColor)
                    .frame(width: geometry.size.width * clamp(fraction))
            }
        }
        .frame(height: 4)
    }
}

/// A level meter with a peak-hold tick.
///
/// The gradient always spans the whole track — green through yellow into red —
/// and the level simply reveals more of it, so a given color always means the
/// same level regardless of how far the bar has filled.
struct LevelBarView: View {
    /// The current level, clamped to 0…1.
    let fraction: Double
    /// The decaying peak-hold position, clamped to 0…1. Hidden when ~zero.
    let peak: Double

    private static let tickWidth: Double = 2.5

    private static let gradient = LinearGradient(
        stops: [
            Gradient.Stop(color: .green, location: 0),
            Gradient.Stop(color: .green, location: 0.55),
            Gradient.Stop(color: .yellow, location: 0.75),
            Gradient.Stop(color: .red, location: 0.95),
        ],
        startPoint: .leading,
        endPoint: .trailing
    )

    var body: some View {
        GeometryReader { geometry in
            let width = geometry.size.width
            ZStack(alignment: .leading) {
                Capsule().fill(.quaternary)
                Capsule()
                    .fill(LevelBarView.gradient)
                    .mask(alignment: .leading) {
                        Rectangle().frame(width: width * clamp(fraction))
                    }
                if peak > 0.01 {
                    Capsule()
                        .fill(Color.primary)
                        .frame(width: LevelBarView.tickWidth)
                        .offset(x: (width - LevelBarView.tickWidth) * clamp(peak))
                }
            }
        }
        .frame(height: 10)
        .animation(.linear(duration: 0.05), value: fraction)
    }
}

// MARK: - Blocks

/// One boxed signal-chain block: an icon, a title with its on/off dot, the
/// block's name, and an optional trailing accessory.
struct BlockCard<Accessory: View>: View {
    /// SF Symbol shown at the leading edge.
    let icon: String
    /// The block's label, e.g. "Amplifier".
    let title: String
    /// The block's name as reported by the device.
    let name: String?
    /// The block's on/off state, or `nil` if unknown.
    let on: Bool?

    private let accessory: Accessory

    init(
        icon: String, title: String, name: String?, on: Bool?,
        @ViewBuilder accessory: () -> Accessory
    ) {
        self.icon = icon
        self.title = title
        self.name = name
        self.on = on
        self.accessory = accessory()
    }

    var body: some View {
        GroupBox {
            HStack(spacing: 10) {
                Image(systemName: icon)
                    .font(.title2)
                    .foregroundStyle(.secondary)
                    .frame(width: 28)
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 5) {
                        Text(title)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        StatusDot(on: on)
                    }
                    Text(name?.trimmed.nonEmpty ?? "—")
                        .font(.callout)
                        .fontWeight(.medium)
                        .lineLimit(1)
                }
                Spacer(minLength: 0)
                accessory
            }
            .padding(4)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

extension BlockCard where Accessory == EmptyView {
    /// A block card with no trailing accessory.
    init(icon: String, title: String, name: String?, on: Bool?) {
        self.init(icon: icon, title: title, name: name, on: on) { EmptyView() }
    }
}

/// The global output readouts: the rig's own volume and the physical Master
/// Volume knob. Both are shown read-only — master especially, since the knob is
/// a potentiometer the device treats as ground truth (there is no soft-takeover
/// to write against).
struct OutputCard: View {
    /// Rig Volume (NRPN `0x04/1`), or `nil` if not seen yet.
    let rigVolume: UInt16?
    /// The Master Volume knob's value (Headphone/Monitor), or `nil` if unseen.
    let masterVolume: UInt16?

    var body: some View {
        GroupBox {
            HStack(spacing: 10) {
                Image(systemName: "dial.medium")
                    .font(.title2)
                    .foregroundStyle(.secondary)
                    .frame(width: 28)
                VStack(alignment: .leading, spacing: 5) {
                    Text("Output")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    volumeRow("Rig", rigVolume)
                    volumeRow("Master", masterVolume)
                }
            }
            .padding(4)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    @ViewBuilder private func volumeRow(_ label: String, _ value: UInt16?) -> some View {
        HStack(spacing: 6) {
            Text(label)
                .font(.caption2)
                .foregroundStyle(.secondary)
                .frame(width: 46, alignment: .leading)
            if let value {
                MiniBar(fraction: Double(value) / MeterFrame.fullScale)
                Text(Format.percent(value))
                    .font(.caption2)
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
                    .frame(width: 34, alignment: .trailing)
            } else {
                Text("—")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }
}

/// The eight effect slots A B C D X MOD DLY REV, laid out four across.
struct EffectGridView: View {
    /// The slots in signal-chain order.
    let effects: [Effect]
    /// Called with a slot's short name when its card is clicked.
    let toggle: (String) -> Void

    var body: some View {
        LazyVGrid(
            columns: Array(repeating: GridItem(.flexible(), spacing: 10), count: 4),
            spacing: 10
        ) {
            ForEach(effects, id: \.slot) { effect in
                EffectCardView(effect: effect) { toggle(effect.slot) }
            }
        }
    }
}

/// One effect slot: its short name, on/off dot, effect type, category, and mix.
/// Clicking the card toggles the slot; an empty (or not-yet-reported) slot is
/// inert and drawn dimmed.
struct EffectCardView: View {
    /// The slot to render.
    let effect: Effect
    /// Ask the device to flip the slot on or off.
    let toggle: () -> Void

    @State private var hovering = false

    /// Only a loaded slot with a known state can be toggled.
    private var togglable: Bool { !effect.isEmpty && effect.on != nil }

    var body: some View {
        Button(action: toggle) {
            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    Text(effect.slot)
                        .font(.caption.bold())
                        .foregroundStyle(.secondary)
                    Spacer()
                    StatusDot(on: effect.on)
                }
                Text(typeText)
                    .font(.callout)
                    .fontWeight(.medium)
                    .foregroundStyle(effect.isEmpty ? Color.secondary : Color.primary)
                    .lineLimit(1)
                // A blank line rather than no line, so cards stay the same height.
                Text(effect.categoryName ?? " ")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                if !effect.isEmpty, let mix = effect.mix {
                    HStack(spacing: 6) {
                        MiniBar(fraction: Double(mix) / MeterFrame.fullScale)
                        Text(Format.percent(mix))
                            .font(.caption2)
                            .monospacedDigit()
                            .foregroundStyle(.secondary)
                            .frame(width: 34, alignment: .trailing)
                    }
                } else {
                    // Keep every card the same height whether or not it has a mix.
                    Color.clear.frame(height: 4)
                }
            }
            .opacity(effect.isEmpty ? 0.45 : 1)
            .padding(10)
            .frame(maxWidth: .infinity, minHeight: 88, alignment: .topLeading)
            .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(fill))
            .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous).strokeBorder(border))
            .contentShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
        }
        .buttonStyle(.plain)
        .disabled(!togglable)
        .onHover { hovering = $0 }
        .help(togglable ? "Turn \(effect.slot) \(effect.on == true ? "off" : "on")" : "")
    }

    private var typeText: String {
        if effect.isEmpty { return "Empty" }
        return effect.typeName ?? effect.kind.map { "Type \($0)" } ?? "—"
    }

    private var fill: Color {
        if effect.isEmpty { return Color.primary.opacity(0.02) }
        let base = effect.on == true ? Color.accentColor : Color.primary
        return base.opacity(effect.on == true ? (hovering ? 0.14 : 0.08) : (hovering ? 0.08 : 0.04))
    }

    private var border: Color {
        if effect.isEmpty { return Color.primary.opacity(0.05) }
        return effect.on == true
            ? Color.accentColor.opacity(0.5)
            : Color.primary.opacity(hovering ? 0.2 : 0.08)
    }
}

// MARK: - Tuner

/// The tuner strobe track.
///
/// Meter field v3 is a wrapping 0–16383 phase whose *rotation rate* tracks
/// pitch deviance: the marker slides along the track, drifting left when the
/// note is sharp and right when it is flat, and holds still when it is in tune.
/// The dots are the track; the diamond is the current phase. The track lays
/// dots at a fixed pitch, so it fills whatever width it is given.
struct TunerStrobeView: View {
    /// The latest rendered frame.
    let meters: MeterFrame

    private static let inset: Double = 4
    private static let dotSpacing: Double = 9
    private static let dotRadius: Double = 1.5
    private static let markerRadius: Double = 6

    var body: some View {
        Canvas { context, size in
            let inset = TunerStrobeView.inset
            let span = size.width - inset * 2
            let midline = size.height / 2
            let dotCount = max(Int(span / TunerStrobeView.dotSpacing) + 1, 2)

            for i in 0..<dotCount {
                let position = Double(i) / Double(dotCount - 1)
                let x = inset + span * position
                let radius = TunerStrobeView.dotRadius
                let dot = Path(
                    ellipseIn: CGRect(
                        x: x - radius, y: midline - radius,
                        width: radius * 2, height: radius * 2))
                context.fill(dot, with: .style(.quaternary))
            }

            guard meters.strobeActive else { return }
            let x = inset + span * clamp(meters.strobePosition)
            let radius = TunerStrobeView.markerRadius
            var marker = Path()
            marker.move(to: CGPoint(x: x, y: midline - radius))
            marker.addLine(to: CGPoint(x: x + radius, y: midline))
            marker.addLine(to: CGPoint(x: x, y: midline + radius))
            marker.addLine(to: CGPoint(x: x - radius, y: midline))
            marker.closeSubpath()
            context.fill(marker, with: .color(meters.verdict.color))
        }
        .frame(height: 17)
    }
}

extension StrobeVerdict {
    /// The verdict's wording.
    var text: String {
        switch self {
        case .idle: return "Idle"
        case .measuring: return "Listening…"
        case .inTune: return "In tune"
        case .sharp: return "Sharp"
        case .flat: return "Flat"
        }
    }

    /// The color shared by the verdict text and the strobe marker.
    var color: Color {
        switch self {
        case .idle, .measuring: return .gray
        case .inTune: return .green
        case .sharp: return .red
        case .flat: return .cyan
        }
    }
}

// MARK: - Meters

/// The level list: one row per meter field, with a bar, the raw value and the
/// range observed since the last connect.
struct MeterListView: View {
    /// The latest rendered frame.
    let meters: MeterFrame
    /// Whether to include the strobe and power fields, not just the bars.
    let showAll: Bool

    var body: some View {
        Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 8) {
            ForEach(fields, id: \.index) { field in
                GridRow {
                    Text(label(for: field))
                        .font(.callout)
                        .frame(width: 170, alignment: .leading)
                    LevelBarView(
                        fraction: meters.fraction(field.index),
                        peak: meters.peakFraction(field.index)
                    )
                    Text("\(meters.values[field.index])")
                        .font(.caption)
                        .monospacedDigit()
                        .foregroundStyle(.secondary)
                        .gridColumnAlignment(.trailing)
                    Text(rangeText(field))
                        .font(.caption)
                        .monospacedDigit()
                        .foregroundStyle(.tertiary)
                        .gridColumnAlignment(.trailing)
                }
            }
        }
    }

    private var fields: [MeterField] {
        Params.meterFields.filter { showAll || $0.render == "bar" }
    }

    /// Human-friendly labels for the meter rows.
    private static let shortLabels: [String: String] = [
        "stack_level": "Stack",
        "stack_power": "Stack Power",
        "rig_out_level": "Rig Out",
        "rig_out_power": "Rig Out Power",
        "loudness": "Loudness",
        "strobe_seg_low": "Strobe Segment Low",
        "strobe_seg_mid": "Strobe Segment Mid",
        "strobe_seg_high": "Strobe Segment High",
        "strobe_phase": "Strobe Phase",
    ]

    private func label(for field: MeterField) -> String {
        let short =
            MeterListView.shortLabels[field.id]
            ?? field.id.replacingOccurrences(of: "_", with: " ").capitalized
        return showAll ? "v\(field.index) \(short)" : short
    }

    private func rangeText(_ field: MeterField) -> String {
        let bounds = meters.range(field.index)
        return "\(bounds.low)–\(bounds.high)"
    }
}

// MARK: - Formatting

/// The value formatting the dashboard shares with the terminal example.
enum Format {
    /// A 14-bit value as a whole-number percentage of full scale.
    static func percent(_ value: UInt16) -> String {
        String(format: "%.0f%%", Double(value) / MeterFrame.fullScale * 100)
    }
}

extension String {
    /// The string with leading and trailing whitespace removed.
    var trimmed: String { trimmingCharacters(in: .whitespacesAndNewlines) }
    /// `nil` if the string is empty, otherwise the string.
    var nonEmpty: String? { isEmpty ? nil : self }
}

/// Clamp to the 0…1 unit range the bars and the strobe track work in.
private func clamp(_ value: Double) -> Double { min(max(value, 0), 1) }
