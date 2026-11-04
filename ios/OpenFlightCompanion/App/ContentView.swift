import SwiftUI

/// The web UI full screen, a native error screen when the Pi cannot be reached, and one small
/// gear button for the native settings. Next to the gear, a
/// microphone symbol shows that impact-audio capture is on (design §9.1).
struct ContentView: View {
    @EnvironmentObject private var settings: AppSettings
    @ObservedObject var shell: WebShell
    @ObservedObject var audio: AudioCaptureController
    @State private var showSettings = false
    @State private var loadedOnce = false

    var body: some View {
        ZStack(alignment: .bottomTrailing) {
            CompanionWebView(shell: shell)
                .ignoresSafeArea()
            if let message = shell.loadError {
                ConnectionErrorView(
                    message: message,
                    baseURL: settings.baseURLText,
                    retry: { shell.reload() },
                    openSettings: { showSettings = true }
                )
            }
            HStack(spacing: 0) {
                if let indicator = AudioIndicator(status: audio.status) {
                    Image(systemName: indicator.symbol)
                        .font(.system(size: 13))
                        .foregroundStyle(.white.opacity(0.35))
                        .padding(.vertical, 10)
                        .accessibilityLabel(indicator.label)
                        .accessibilityIdentifier("audioIndicator")
                }
                Button {
                    showSettings = true
                } label: {
                    Image(systemName: "gearshape.fill")
                        .font(.system(size: 15))
                        .foregroundStyle(.white.opacity(0.35))
                        .padding(10)
                }
                .accessibilityLabel("Einstellungen")
            }
            .padding(.trailing, 6)
            .padding(.bottom, 2)
        }
        .background(Color.black)
        .onAppear {
            guard !loadedOnce else { return }
            loadedOnce = true
            // A deep link that launched the app may already have loaded its page.
            if !shell.hasStartedLoading { shell.load(baseURL: settings.startURL) }
        }
        .onChange(of: settings.baseURL) { _, url in
            // A deep link switches the origin and loads its own page in one step.
            if shell.baseURL != url { shell.load(baseURL: url) }
        }
        .onOpenURL { url in
            CompanionDeepLink.open(url, settings: settings, shell: shell)
        }
        .sheet(isPresented: $showSettings) {
            SettingsSheet(shell: shell, audio: audio)
                .environmentObject(settings)
        }
    }
}

/// Shown instead of a blank page when the launch monitor cannot be loaded.
struct ConnectionErrorView: View {
    let message: String
    let baseURL: String
    let retry: () -> Void
    let openSettings: () -> Void

    var body: some View {
        VStack(spacing: 16) {
            Text("OpenFlight")
                .font(.largeTitle).bold()
            Text(message)
                .multilineTextAlignment(.center)
                .foregroundStyle(.secondary)
                .padding(.horizontal, 32)
            Text(baseURL)
                .font(.footnote.monospaced())
                .foregroundStyle(.tertiary)
            HStack(spacing: 12) {
                Button("Einstellungen", action: openSettings)
                    .buttonStyle(.bordered)
                Button("Erneut versuchen", action: retry)
                    .buttonStyle(.borderedProminent)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color.black)
    }
}

/// The microphone symbol next to the gear; nil while capture is off.
struct AudioIndicator: Equatable {
    let symbol: String
    let label: String

    init?(status: AudioStatus) {
        switch status.state {
        case .idle:
            return nil
        case .starting:
            symbol = "mic"
            label = "Mikrofon startet"
        case .running:
            symbol = "mic.fill"
            label = "Mikrofon nimmt das Treffgeräusch auf"
        case .interrupted:
            symbol = "mic.slash"
            label = "Mikrofon unterbrochen"
        case .error:
            symbol = "mic.slash"
            label = status.permission == .denied ? "Kein Mikrofonzugriff" : "Mikrofon-Fehler"
        }
    }

    /// One line for the settings sheet.
    static func describe(_ status: AudioStatus, enabled: Bool) -> String {
        guard enabled else { return "Aus." }
        switch status.state {
        case .idle:
            return "Startet, sobald der Launch-Monitor verbunden ist."
        case .starting:
            return "Startet …"
        case .running:
            let rate = status.session.sampleRateHz.map { String(format: " · %.1f kHz", $0 / 1_000) } ?? ""
            return "Läuft · \(status.onsets) Treffer\(rate)"
        case .interrupted:
            return "Unterbrochen (Anruf, Siri oder eine andere App)."
        case .error:
            if status.permission == .denied {
                return "Kein Mikrofonzugriff. Erlauben unter Einstellungen › OpenFlight."
            }
            return "Fehler: \(status.error?.message ?? "unbekannt")"
        }
    }
}

struct SettingsSheet: View {
    @EnvironmentObject private var settings: AppSettings
    @ObservedObject var shell: WebShell
    @ObservedObject var audio: AudioCaptureController
    @Environment(\.dismiss) private var dismiss
    @State private var draft = ""
    @State private var invalid = false
    @State private var captures: CaptureStore.Summary?
    @State private var exporting = false

    var body: some View {
        NavigationStack {
            Form {
                Section("Launch-Monitor") {
                    TextField("http://openflight.local:8080", text: $draft)
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .onSubmit(apply)
                    if invalid {
                        Text("Keine gültige http(s)-Adresse.")
                            .font(.footnote)
                            .foregroundStyle(.red)
                    }
                    Button("Übernehmen", action: apply)
                    Button("Standard wiederherstellen") {
                        settings.resetBaseURL()
                        draft = settings.baseURLText
                        invalid = false
                    }
                    Text("Standard: \(AppSettings.defaultBaseURL.absoluteString). Das iPhone muss im WLAN des Pi sein.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
                Section {
                    Toggle("Treffgeräusch aufnehmen", isOn: $settings.audioCaptureEnabled)
                    Text(AudioIndicator.describe(audio.status, enabled: settings.audioCaptureEnabled))
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                    Toggle("Debug-Aufnahmen (WAV)", isOn: $settings.debugCaptureEnabled)
                    Button {
                        exporting = true
                        Task {
                            _ = await audio.exportCaptures()
                            captures = await audio.captureSummary()
                            exporting = false
                        }
                    } label: {
                        Text(exportTitle)
                    }
                    .disabled(exporting || (captures?.files ?? 0) == 0)
                } header: {
                    Text("Mikrofon")
                } footer: {
                    Text("Läuft im Vordergrund, solange der Launch-Monitor verbunden ist, und wird nur protokolliert. Debug-Aufnahmen speichern ±200 ms um jeden Treffer, höchstens \(CaptureStore.defaultMaxFiles) Dateien.")
                }
                Section {
                    Toggle("Display anlassen", isOn: $settings.keepScreenOn)
                } header: {
                    Text("Display")
                } footer: {
                    Text("Keine automatische Sperre, solange OpenFlight im Vordergrund ist.")
                }
                Section("Seite") {
                    Button("Neu laden") {
                        shell.reload()
                        dismiss()
                    }
                    Button("Zurück") {
                        shell.goBack()
                        dismiss()
                    }
                    .disabled(!shell.canGoBack)
                }
            }
            .navigationTitle("OpenFlight")
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Fertig") { dismiss() }
                }
            }
            .onAppear { draft = settings.baseURLText }
            .task { captures = await audio.captureSummary() }
            .onChange(of: audio.status.lastSeq) {
                Task { captures = await audio.captureSummary() }
            }
        }
    }

    private var exportTitle: String {
        guard let captures, captures.files > 0 else { return "Debug-Aufnahmen exportieren" }
        let megabytes = Double(captures.bytes) / 1_048_576
        return String(format: "Debug-Aufnahmen exportieren (%d, %.1f MB)", captures.files, megabytes)
    }

    private func apply() {
        if settings.baseURLText == AppSettings.normalizedBaseURL(draft)?.absoluteString {
            invalid = false
            return
        }
        invalid = !settings.setBaseURL(text: draft)
        if !invalid {
            draft = settings.baseURLText
            dismiss()
        }
    }
}
