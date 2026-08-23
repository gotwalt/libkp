import AppKit
import LibKP
import SwiftUI

// The main window: the whole dashboard when a session is live, and the
// connect / retry placeholder when it is not.

// MARK: - Window

/// The app's one window. It swaps between the dashboard and the connection
/// placeholder, and carries the status badge and the Reconnect button.
struct DashboardView: View {
    @EnvironmentObject private var store: DeviceStore
    @AppStorage(SettingsKeys.showAllMeters) private var showAllMeters = false

    var body: some View {
        Group {
            if case .connected = store.phase {
                dashboard
            } else {
                ConnectionPlaceholderView(phase: store.phase) { store.restart() }
            }
        }
        .frame(minWidth: 700, minHeight: 600)
        .navigationTitle(windowTitle)
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button {
                    store.restart()
                } label: {
                    Label("Reconnect", systemImage: "arrow.clockwise")
                }
                .keyboardShortcut("r")
                .help("Drop the session and connect again")
            }
        }
    }

    /// "KP Meters", plus the device once one is in play — its discovery name
    /// when known, and its address.
    private var windowTitle: String {
        switch store.phase {
        case let .connected(host, name):
            if let name = name?.trimmed.nonEmpty { return "KP Meters - \(name) · \(host)" }
            return "KP Meters - \(host)"
        case let .connecting(host):
            return "KP Meters - \(host)"
        default:
            return "KP Meters"
        }
    }

    private var dashboard: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                VStack(alignment: .leading, spacing: 8) {
                    RigNavigationView()
                    RigHeaderView(state: store.state, beatActive: store.meters.beatActive)
                }

                HStack(spacing: 12) {
                    BlockCard(
                        icon: "guitars", title: "Amplifier",
                        name: store.state.amp.name, on: store.state.amp.on
                    ) {
                        gainAccessory
                    }
                    BlockCard(
                        icon: "hifispeaker", title: "Cabinet",
                        name: store.state.cabinet.name, on: store.state.cabinet.on)
                    TunerBlockCard(meters: store.meters)
                    OutputCard(
                        rigVolume: store.state.rig.volume,
                        masterVolume: store.state.output.masterVolume)
                }

                section("Signal Chain", icon: "slider.horizontal.3") {
                    EffectGridView(effects: store.state.effects) { store.toggleEffect($0) }
                }
                section("Levels", icon: "waveform") {
                    MeterListView(meters: store.meters, showAll: showAllMeters)
                }
            }
            .padding(16)
        }
    }

    @ViewBuilder private var gainAccessory: some View {
        if let gain = store.state.amp.gain {
            VStack(alignment: .trailing, spacing: 4) {
                Text("Gain \(Format.percent(gain))")
                    .font(.caption)
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
                MiniBar(fraction: Double(gain) / MeterFrame.fullScale)
                    .frame(width: 70)
            }
        }
    }

    /// A titled box around one part of the dashboard.
    private func section<Content: View>(
        _ title: String, icon: String, @ViewBuilder content: () -> Content
    ) -> some View {
        GroupBox {
            content()
                .padding(6)
                .frame(maxWidth: .infinity, alignment: .leading)
        } label: {
            Label(title, systemImage: icon)
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
                .textCase(.uppercase)
        }
    }
}

// MARK: - Rig navigation

/// Rig switching: previous/next rig, previous/next bank, and the five slot
/// buttons — each labelled with the rig name the current bank holds in that
/// slot. The slot names come from the bank preview the device pushes on every
/// bank change (also read at connect), so they always describe the bank the
/// device is actually on.
///
/// The position readout is the device's own, never this app's arithmetic: the
/// `$06` it pushes at the current-bank and current-rig-slot addresses keeps it
/// live, so it follows front-panel changes as readily as taps here. It reads `—`
/// only before the first report has arrived.
///
/// Every move is computed in the flat rig index and sent as an absolute bank
/// preselect plus a slot load, so any rig is one hop away and bank boundaries
/// are not special. Nothing assumes how many rigs the device holds.
///
/// The highlighted slot prefers this app's last tap until the device reports
/// where it landed, then the device's own slot, then matching the loaded rig
/// name against the five preview names (`DeviceStore.deviceSlot`).
struct RigNavigationView: View {
    @EnvironmentObject private var store: DeviceStore

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 12) {
                HStack(spacing: 4) {
                    Button {
                        store.stepRig(by: -1)
                    } label: {
                        Image(systemName: "chevron.left")
                    }
                    .help("Previous rig")
                    Button {
                        store.stepRig(by: 1)
                    } label: {
                        Image(systemName: "chevron.right")
                    }
                    .help("Next rig")
                }

                Divider().frame(height: 16)

                HStack(spacing: 4) {
                    Button {
                        store.stepBank(forward: false)
                    } label: {
                        Image(systemName: "chevron.left.2")
                    }
                    .help("Previous bank")
                    Button {
                        store.stepBank(forward: true)
                    } label: {
                        Image(systemName: "chevron.right.2")
                    }
                    .help("Next bank")
                }

                Divider().frame(height: 16)

                Text(position)
                    .font(.callout)
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
                    .help("Where the device reports it is")

                Spacer()
            }
            .buttonStyle(.bordered)
            .controlSize(.small)

            HStack(spacing: 6) {
                ForEach(1...5, id: \.self) { slot in
                    slotButton(slot)
                }
            }
        }
    }

    /// The device's reported position, or an em-dash before the first report.
    private var position: String {
        guard let bank = store.bank, let slot = store.slot else { return "Bank — · Rig —" }
        return "Bank \(bank) · Rig \(slot)"
    }

    /// The bank-preview slot for a 1-based slot number, if the device has sent it.
    private func previewSlot(_ slot: Int) -> BankSlot? {
        let slots = store.state.bank.slots
        return slots.indices.contains(slot - 1) ? slots[slot - 1] : nil
    }

    private func slotButton(_ slot: Int) -> some View {
        let preview = previewSlot(slot)
        let name = preview?.rigName?.trimmed.nonEmpty
        let selected = store.highlightedSlot == slot
        return Button {
            store.selectSlot(slot)
        } label: {
            VStack(spacing: 2) {
                Text("\(slot)")
                    .font(.caption.bold())
                    .monospacedDigit()
                Text(name ?? "—")
                    .font(.caption2)
                    .lineLimit(1)
                    .truncationMode(.tail)
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 5)
            .padding(.horizontal, 4)
            .foregroundStyle(slotTextColor(selected: selected, hasName: name != nil))
            .background(
                RoundedRectangle(cornerRadius: 6, style: .continuous)
                    .fill(selected ? Color.accentColor : Color.primary.opacity(0.06))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 6, style: .continuous)
                    .strokeBorder(
                        selected ? Color.accentColor : Color.primary.opacity(0.12), lineWidth: 1)
            )
            .contentShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
        }
        .buttonStyle(.plain)
        .help(slotHelp(slot, preview))
    }

    /// The slot label colour: white on the accent fill when selected, otherwise
    /// primary for a named slot and secondary for an empty one.
    private func slotTextColor(selected: Bool, hasName: Bool) -> Color {
        if selected { return .white }
        return hasName ? .primary : .secondary
    }

    /// A tooltip naming the rig and, when known, its amp and cabinet.
    private func slotHelp(_ slot: Int, _ preview: BankSlot?) -> String {
        guard let preview, let rig = preview.rigName?.trimmed.nonEmpty else {
            return "Load slot \(slot)"
        }
        var parts = [rig]
        if let amp = preview.ampName?.trimmed.nonEmpty { parts.append(amp) }
        if let cab = preview.cabinetName?.trimmed.nonEmpty { parts.append(cab) }
        return parts.joined(separator: " · ")
    }
}

// MARK: - Header

/// The loaded rig: its name and author, with the morph control and the rig
/// tempo — whose metronome icon flashes on the beat pulse — at the trailing
/// edge.
///
/// The morph position is CBOR-only, so it reads `—` until that channel is up.
/// The toggle works either way: it is a Control Change, which the streaming
/// session carries.
struct RigHeaderView: View {
    @EnvironmentObject private var store: DeviceStore
    /// The latest snapshot.
    let state: DeviceState
    /// True during the brief window after an "on" beat pulse.
    let beatActive: Bool

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text(state.rig.name?.trimmed.nonEmpty ?? "—")
                .font(.system(.title, design: .rounded).weight(.bold))
                .lineLimit(1)
            if let author = state.rig.author?.trimmed.nonEmpty {
                Text("by \(author)")
                    .font(.title3)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            MorphControl(morph: state.morph, isMorphed: store.isMorphed) {
                store.setMorphed($0)
            }
            if let tempo = state.rig.tempoBpm {
                Label {
                    Text("\(tempo) BPM")
                } icon: {
                    Image(systemName: beatActive ? "metronome.fill" : "metronome")
                        .foregroundStyle(beatActive ? Color.yellow : Color.secondary)
                }
                .font(.callout)
                .monospacedDigit()
                .foregroundStyle(.secondary)
            }
        }
    }
}

// MARK: - Connection

/// What the window shows while there is no live session: the discovery and
/// connect progress, or the failure with a way out of it.
struct ConnectionPlaceholderView: View {
    /// The current connect phase.
    let phase: Phase
    /// Start another connect cycle.
    let retry: () -> Void

    var body: some View {
        VStack(spacing: 14) {
            switch phase {
            case .idle, .discovering:
                ProgressView()
                    .controlSize(.large)
                Text("Looking for a Profiler…")
                    .font(.title3)
                    .fontWeight(.medium)
                Text("Broadcasting discovery on the local network.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            case let .connecting(host):
                ProgressView()
                    .controlSize(.large)
                Text("Connecting to \(host)…")
                    .font(.title3)
                    .fontWeight(.medium)
            case let .failed(message):
                Image(systemName: "antenna.radiowaves.left.and.right.slash")
                    .font(.system(size: 36))
                    .foregroundStyle(.secondary)
                Text(message)
                    .multilineTextAlignment(.center)
                    .frame(maxWidth: 380)
                HStack(spacing: 10) {
                    Button("Try Again", action: retry)
                    Button("Settings…") { openSettingsWindow() }
                }
            case .connected:
                EmptyView()
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

/// Open the Settings scene. `SettingsLink` would be the tidy way to do this,
/// but it needs macOS 14, so ask AppKit to fire the standard action instead.
@MainActor
func openSettingsWindow() {
    NSApp.sendAction(Selector(("showSettingsWindow:")), to: nil, from: nil)
}

// MARK: - Tuner block

/// The tuner as a compact block alongside the amp and cabinet: the verdict on
/// the title row, the strobe track underneath.
struct TunerBlockCard: View {
    /// The latest rendered frame.
    let meters: MeterFrame

    var body: some View {
        GroupBox {
            HStack(spacing: 10) {
                Image(systemName: "tuningfork")
                    .font(.title2)
                    .foregroundStyle(.secondary)
                    .frame(width: 28)
                VStack(alignment: .leading, spacing: 2) {
                    HStack {
                        Text("Tuner")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        Spacer()
                        Text(meters.verdict.text)
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(meters.verdict.color)
                    }
                    TunerStrobeView(meters: meters)
                }
            }
            .padding(4)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}
