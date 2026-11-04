import Foundation

/// UserDefaults-backed configuration. A `-baseURL <url>` launch argument wins over the stored
/// value (UserDefaults' argument domain), which is how the Simulator run points the app at a
/// mock server; `-audioCaptureEnabled NO` and `-debugCaptureEnabled YES` work the same way.
@MainActor
final class AppSettings: ObservableObject {
    nonisolated static let defaultBaseURL = normalizedBaseURL("http://openflight.local:8080")!
    static let baseURLKey = "baseURL"

    static let audioCaptureKey = "audioCaptureEnabled"
    static let debugCaptureKey = "debugCaptureEnabled"
    static let keepScreenOnKey = "keepScreenOn"

    /// The origin the app loads and the only one the bridge serves.
    @Published private(set) var baseURL: URL

    /// Impact-audio capture runs automatically while the app is in the foreground and the
    /// launch monitor's page is loaded (design §9.1). On by default.
    @Published var audioCaptureEnabled: Bool {
        didSet { defaults.set(audioCaptureEnabled, forKey: Self.audioCaptureKey) }
    }

    /// Debug WAV snippets around each onset (design §9.7). Off by default.
    @Published var debugCaptureEnabled: Bool {
        didSet { defaults.set(debugCaptureEnabled, forKey: Self.debugCaptureKey) }
    }

    /// "Display anlassen": no auto-lock while the app is in the foreground (design F15). On by
    /// default, because at the range the phone shows the shots.
    @Published var keepScreenOn: Bool {
        didSet { defaults.set(keepScreenOn, forKey: Self.keepScreenOnKey) }
    }

    private let defaults: UserDefaults

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        baseURL = defaults.string(forKey: Self.baseURLKey).flatMap(Self.normalizedBaseURL) ?? Self.defaultBaseURL
        // `bool(forKey:)` also reads a "YES"/"NO" launch argument; `object` tells absent apart.
        audioCaptureEnabled = defaults.object(forKey: Self.audioCaptureKey) == nil ? true : defaults.bool(forKey: Self.audioCaptureKey)
        debugCaptureEnabled = defaults.bool(forKey: Self.debugCaptureKey)
        keepScreenOn = defaults.object(forKey: Self.keepScreenOnKey) == nil ? true : defaults.bool(forKey: Self.keepScreenOnKey)
    }

    /// The text the user sees in the settings field.
    var baseURLText: String { baseURL.absoluteString }

    /// Stores a manual override; false when the text is not a usable http(s) origin.
    @discardableResult
    func setBaseURL(text: String) -> Bool {
        guard let url = Self.normalizedBaseURL(text) else { return false }
        defaults.set(url.absoluteString, forKey: Self.baseURLKey)
        baseURL = url
        return true
    }

    func resetBaseURL() {
        defaults.removeObject(forKey: Self.baseURLKey)
        baseURL = defaults.string(forKey: Self.baseURLKey).flatMap(Self.normalizedBaseURL) ?? Self.defaultBaseURL
    }

    /// The page the shell opens: the origin's `/`, the page a paired phone browser shows
    /// (`baseURL` always carries the `/` path).
    var startURL: URL { baseURL }

    /// `scheme://host[:port]` from user input. A missing scheme means http (the Pi serves plain
    /// http); any path, query or fragment is dropped, because only one origin is ever used.
    nonisolated static func normalizedBaseURL(_ raw: String) -> URL? {
        var text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }
        if !text.contains("://") { text = "http://" + text }
        guard let parsed = URLComponents(string: text),
              let scheme = parsed.scheme?.lowercased(), scheme == "http" || scheme == "https",
              let host = parsed.host, !host.isEmpty,
              parsed.user == nil, parsed.password == nil
        else { return nil }
        var origin = URLComponents()
        origin.scheme = scheme
        origin.host = host.lowercased()
        if let port = parsed.port {
            guard (1...65_535).contains(port) else { return nil }
            if port != OriginPolicy.defaultPort(forScheme: scheme) { origin.port = port }
        }
        origin.path = "/"
        return origin.url
    }
}
