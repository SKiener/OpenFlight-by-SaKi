import SwiftUI
import UIKit
import WebKit

/// Owns the one `WKWebView` (design §6): configuration, the bridge, navigation policy and the
/// load-error state the native UI shows.
@MainActor
final class WebShell: NSObject, ObservableObject {
    /// Set when the page could not be loaded (Pi not reachable, wrong Wi-Fi); nil after a load.
    @Published private(set) var loadError: String?
    @Published private(set) var canGoBack = false
    /// The launch monitor's page is loaded from the configured origin. Set by a finished load,
    /// cleared by a failed one or a new base origin; a reload of a loaded page keeps it.
    @Published private(set) var isConnected = false
    /// Called when a new document of the configured origin commits in the main frame (the page
    /// starts over), before its scripts run. Same-document navigations do not commit.
    var onPageCommit: (() -> Void)?

    /// The web view, built on first use and never in `init`.
    ///
    /// The shell is created in `App.init`, which runs before `UIApplicationMain` has created the
    /// application. A view built that early adds its gesture recognizers while `UIApp` is still
    /// nil, so UIKit never registers them with the gesture environment and leaves them without a
    /// delayed-event dispatcher (`_UIGestureRecognizerRegisterInContainer` does this only on the
    /// first registration). On iOS 27 the scroll view's delays-touches-began recognizer then adds
    /// a nil touch on the first tap and the app aborts in
    /// `-[UIGestureRecognizer _delayTouchesForEvent:inPhase:]`. The first use is the SwiftUI view
    /// (or `load`), after the application exists.
    var webView: WKWebView {
        if let createdWebView { return createdWebView }
        let webView = makeWebView()
        createdWebView = webView
        return webView
    }

    /// Whether `webView` has been built yet (tests check that `init` does not build it).
    var isWebViewCreated: Bool { createdWebView != nil }

    let bridge: NativeBridge
    private(set) var baseURL: URL
    private var createdWebView: WKWebView?
    private var backObservation: NSKeyValueObservation?
    #if DEBUG
    var bridgeSelfTestDone = false
    #endif

    init(services: BridgeServices, config: BridgeConfig, baseURL: URL) {
        self.baseURL = baseURL
        bridge = NativeBridge(services: services, config: config, baseURL: baseURL)
        super.init()
    }

    private func makeWebView() -> WKWebView {
        let configuration = WKWebViewConfiguration()
        // Default, persistent data store: the pairing cookie survives app restarts (design F10).
        configuration.websiteDataStore = .default()
        configuration.allowsInlineMediaPlayback = true
        // Shot callouts play without a tap.
        configuration.mediaTypesRequiringUserActionForPlayback = []
        configuration.applicationNameForUserAgent = CompanionServices.userAgentToken
        bridge.install(in: configuration.userContentController)

        let webView = WKWebView(frame: .zero, configuration: configuration)
        bridge.webView = webView
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsBackForwardNavigationGestures = true
        webView.scrollView.contentInsetAdjustmentBehavior = .never
        webView.isOpaque = false
        webView.backgroundColor = .black
        webView.scrollView.backgroundColor = .black
        #if DEBUG
        if #available(iOS 16.4, *) { webView.isInspectable = true }
        #endif
        backObservation = webView.observe(\.canGoBack, options: [.initial, .new]) { [weak self] view, _ in
            let value = view.canGoBack
            Task { @MainActor in self?.canGoBack = value }
        }
        return webView
    }

    /// Whether anything has been loaded yet (a deep link can arrive before the first appearance).
    private(set) var hasStartedLoading = false

    /// Loads `page` (default: `/`) of a (new) base origin. Only this origin may use the bridge.
    func load(baseURL: URL, page: URL? = nil) {
        self.baseURL = baseURL
        bridge.baseURL = baseURL
        loadError = nil
        isConnected = false
        hasStartedLoading = true
        webView.load(URLRequest(url: page ?? baseURL))
    }

    /// Reloads the current page, or the start page when nothing has loaded yet.
    func reload() {
        loadError = nil
        if let current = webView.url, OriginPolicy.matches(url: current, base: baseURL) {
            webView.reload()
        } else {
            webView.load(URLRequest(url: baseURL))
        }
    }

    func goBack() {
        if webView.canGoBack { webView.goBack() }
    }

    /// German text for a failed load; nil for errors that are part of normal navigation.
    nonisolated static func friendlyMessage(for error: Error) -> String? {
        let error = error as NSError
        if error.domain == NSURLErrorDomain, error.code == NSURLErrorCancelled { return nil }
        // "Frame load interrupted" is WebKit's normal response to a cancelled navigation.
        if error.domain == "WebKitErrorDomain", error.code == 102 { return nil }
        let unreachable = [
            NSURLErrorCannotFindHost, NSURLErrorCannotConnectToHost, NSURLErrorTimedOut,
            NSURLErrorNotConnectedToInternet, NSURLErrorDNSLookupFailed, NSURLErrorNetworkConnectionLost,
        ]
        if error.domain == NSURLErrorDomain, unreachable.contains(error.code) {
            return "Der Launch-Monitor ist nicht erreichbar. Ist das iPhone mit dem WLAN des Pi verbunden und der Pi eingeschaltet?"
        }
        return error.localizedDescription
    }
}

extension WebShell: WKNavigationDelegate, WKUIDelegate {
    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping @MainActor @Sendable (WKNavigationActionPolicy) -> Void
    ) {
        guard let url = navigationAction.request.url else {
            decisionHandler(.cancel)
            return
        }
        let scheme = url.scheme?.lowercased() ?? ""
        if ["about", "blob", "data"].contains(scheme) {
            decisionHandler(.allow)
            return
        }
        let sameOrigin = OriginPolicy.matches(url: url, base: baseURL)
        let isMainFrame = navigationAction.targetFrame?.isMainFrame ?? true
        if sameOrigin {
            if navigationAction.targetFrame == nil {
                // target=_blank on our own origin: stay in the shell.
                webView.load(navigationAction.request)
                decisionHandler(.cancel)
            } else {
                decisionHandler(.allow)
            }
            return
        }
        if isMainFrame {
            // Other hosts never replace the OpenFlight page; they open in Safari.
            if ["http", "https", "mailto", "tel"].contains(scheme) {
                UIApplication.shared.open(url)
            }
            decisionHandler(.cancel)
            return
        }
        // A foreign subframe may load, but the bridge refuses it (main frame + origin check).
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, didCommit navigation: WKNavigation!) {
        if let url = webView.url, OriginPolicy.matches(url: url, base: baseURL) {
            onPageCommit?()
        }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        loadError = nil
        if let url = webView.url, OriginPolicy.matches(url: url, base: baseURL) {
            isConnected = true
        }
        #if DEBUG
        runBridgeSelfTestIfRequested()
        #endif
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        if let message = Self.friendlyMessage(for: error) {
            loadError = message
            isConnected = false
        }
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        if let message = Self.friendlyMessage(for: error) {
            loadError = message
            isConnected = false
        }
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        isConnected = false
        reload()
    }

    func webView(
        _ webView: WKWebView,
        createWebViewWith configuration: WKWebViewConfiguration,
        for navigationAction: WKNavigationAction,
        windowFeatures: WKWindowFeatures
    ) -> WKWebView? {
        // window.open: handled by the navigation policy above; never a second web view.
        nil
    }
}

#if DEBUG
extension WebShell {
    /// Debug builds only: with the launch argument `-bridgeSelfTest YES`, calls the bridge from
    /// the loaded page once and prints the results, so a Simulator run can check the full path
    /// (page → shim → native → reply) without Safari Web Inspector.
    func runBridgeSelfTestIfRequested() {
        guard UserDefaults.standard.bool(forKey: "bridgeSelfTest"), !bridgeSelfTestDone else { return }
        bridgeSelfTestDone = true
        let script = """
            const out = { native: typeof window.openflightNative };
            if (out.native === 'undefined') return JSON.stringify(out);
            out.capabilities = window.openflightNative.capabilities;
            try { out.sensors = await window.openflightNative.getSensors({ location_max_age_s: 0, timeout_ms: 8000 }); }
            catch (e) { out.sensors_error = e.code + ': ' + e.message; }
            const loc = out.sensors && out.sensors.location;
            const lat = loc && loc.latitude != null ? loc.latitude : 48.1374;
            const lon = loc && loc.longitude != null ? loc.longitude : 11.5755;
            try { out.weather = await window.openflightNative.fetchWeather({ latitude: lat, longitude: lon }); }
            catch (e) { out.weather_error = e.code + ': ' + e.message; }
            try { await window.openflightNative.fetchWeather({ latitude: 999, longitude: 0 }); }
            catch (e) { out.bad_request = e.code; }
            return JSON.stringify(out);
        """
        Task { @MainActor in
            do {
                let result = try await webView.callAsyncJavaScript(script, arguments: [:], in: nil, contentWorld: .page)
                print("[bridgeSelfTest] \(result ?? "nil")")
            } catch {
                print("[bridgeSelfTest] failed: \(error)")
            }
        }
    }
}
#endif

/// SwiftUI host for the shell's web view.
struct CompanionWebView: UIViewRepresentable {
    let shell: WebShell

    func makeUIView(context: Context) -> WKWebView { shell.webView }

    func updateUIView(_ webView: WKWebView, context: Context) {}
}
