import AppKit
import SwiftUI

// `MetersApp` — the `meters` terminal example rebuilt as a native SwiftUI app.
//
// It connects (by discovery, or to an address entered in Settings), performs
// the read-only initial rig sync, and then renders the current patch: rig
// header, amp and cabinet, the eight effect blocks with their on/off state and
// effect type, the tuner strobe, and the level bars. Unlike the terminal
// example it is not purely read-only: clicking a signal-chain block toggles
// that effect on or off. Everything else it sends is a value request.

@main
struct MetersApp: App {
    @StateObject private var store = DeviceStore()
    /// The loopback command socket, when `KP_DEBUG_PORT` asks for one. Held for
    /// the app's lifetime; `nil` — and nothing listening — otherwise.
    @State private var debugControl: DebugControl?

    var body: some Scene {
        Window("KP Meters", id: "main") {
            DashboardView()
                .environmentObject(store)
                .onAppear {
                    // `swift run` starts a bare executable with no app bundle,
                    // so it launches as an accessory with no menu bar and no
                    // focus. Promote it and pull it to the front by hand.
                    NSApp.setActivationPolicy(.regular)
                    NSApp.activate(ignoringOtherApps: true)
                    store.start()
                    if debugControl == nil {
                        debugControl = DebugControl.fromEnvironment(store: store)
                    }
                }
        }
        .defaultSize(width: 760, height: 640)

        Settings {
            SettingsView()
                .environmentObject(store)
        }
    }
}
