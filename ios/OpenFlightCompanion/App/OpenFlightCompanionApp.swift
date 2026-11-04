import SwiftUI

/*
 * OpenFlight iPhone companion (docs/ios-companion/design.md).
 *
 * A thin shell: the whole UI is the launch monitor's own web page, loaded from the Pi over its
 * Wi-Fi hotspot. The native parts are what a page served over plain http cannot do:
 * barometer, GPS, an Open-Meteo fetch over cellular while Wi-Fi has no internet, keeping
 * the screen on, and the impact sound timed on the phone's clock (log-only). The page reaches
 * them through `window.openflightNative`.
 */
@main
struct OpenFlightCompanionApp: App {
    @Environment(\.scenePhase) private var scenePhase
    @StateObject private var settings: AppSettings
    @StateObject private var shell: WebShell
    private let services: CompanionServices

    init() {
        let settings = AppSettings()
        let services = CompanionServices(settings: settings)
        self.services = services
        let shell = WebShell(services: services, config: services.bridgeConfig, baseURL: settings.baseURL)
        // Audio events go to the page; a new document clears what the last page asked for
        // (audio overrides, the accelerometer).
        services.audio.eventSink = { [weak shell] detail in shell?.bridge.dispatch(detail) }
        shell.onPageCommit = { [weak services] in services?.pageDidCommit() }
        services.audio.follow(connected: shell.$isConnected)
        _settings = StateObject(wrappedValue: settings)
        _shell = StateObject(wrappedValue: shell)
    }

    var body: some Scene {
        WindowGroup {
            ContentView(shell: shell, audio: services.audio)
                .environmentObject(settings)
                .preferredColorScheme(.dark)
                .onChange(of: scenePhase, initial: true) { _, phase in
                    services.setScenePhase(phase)
                }
        }
    }
}
