import SwiftUI

// The Settings scene (⌘,): how the app finds the device, and how much of the
// raw meter block the level list shows.

/// The app's preferences. Every change that affects the session restarts it,
/// so the dashboard always reflects what is configured here.
struct SettingsView: View {
    @EnvironmentObject private var store: DeviceStore
    @AppStorage(SettingsKeys.mode) private var mode = ConnectionMode.automatic.rawValue
    @AppStorage(SettingsKeys.manualHost) private var manualHost = ""
    @AppStorage(SettingsKeys.showAllMeters) private var showAllMeters = false

    var body: some View {
        Form {
            Section("Connection") {
                Picker("Find the device", selection: modeBinding) {
                    Label("Automatic discovery", systemImage: "dot.radiowaves.left.and.right")
                        .tag(ConnectionMode.automatic.rawValue)
                    Label("Manual address", systemImage: "network")
                        .tag(ConnectionMode.manual.rawValue)
                }
                .pickerStyle(.radioGroup)

                TextField(
                    "Device address",
                    text: $manualHost,
                    prompt: Text("e.g. 192.168.1.50")
                )
                .disabled(mode != ConnectionMode.manual.rawValue)
                .onSubmit { store.restart() }
            }

            Section("Meters") {
                Toggle("Show all eleven raw meter fields", isOn: $showAllMeters)
                Text(
                    "Adds the three tuner strobe segment drivers, the strobe phase and the "
                        + "stack and rig-output power fields to the level list."
                )
                .font(.caption)
                .foregroundStyle(.secondary)
            }

            Section {
                Button("Reconnect Now") { store.restart() }
            }
        }
        .formStyle(.grouped)
        .frame(width: 440)
    }

    /// Writes the choice through to `@AppStorage` and restarts the session in
    /// the same step. Reacting in `onChange` would mean the one-parameter form,
    /// which is all macOS 13 has and is deprecated from macOS 14 on.
    private var modeBinding: Binding<String> {
        Binding(
            get: { mode },
            set: { newValue in
                mode = newValue
                store.restart()
            }
        )
    }
}
