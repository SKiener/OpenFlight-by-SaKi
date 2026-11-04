import Foundation
import WebKit

/// What the bridge commands do. The app's implementation talks to sensors and the network;
/// tests inject a fake.
@MainActor
protocol BridgeServices: AnyObject {
    func getSensors(_ args: GetSensorsArguments) async -> JSONValue
    func fetchWeather(_ args: FetchWeatherArguments) async -> Result<JSONValue, BridgeError>
    func setKeepAwake(_ on: Bool) -> JSONValue
    func audioStart(_ args: AudioStartArguments) async -> Result<JSONValue, BridgeError>
    func audioStop() -> JSONValue
    func audioStatus() -> JSONValue
    func audioOnsets(_ args: AudioOnsetsArguments) -> JSONValue
    func audioExportCaptures() async -> Result<JSONValue, BridgeError>
    func motionStart() -> Result<JSONValue, BridgeError>
    func motionSamples(_ args: MotionSamplesArguments) -> JSONValue
    func motionStop() -> JSONValue
    func speechSpeak(_ args: SpeechSpeakArguments) -> Result<JSONValue, BridgeError>
    func speechCancel() -> JSONValue
}

/// Routes `postMessage` calls from `window.openflightNative` (design §7) and dispatches
/// native → web events.
@MainActor
final class NativeBridge: NSObject, WKScriptMessageHandlerWithReply {
    private let services: BridgeServices
    let config: BridgeConfig
    /// The only origin allowed to use the bridge.
    var baseURL: URL
    weak var webView: WKWebView?

    init(services: BridgeServices, config: BridgeConfig, baseURL: URL) {
        self.services = services
        self.config = config
        self.baseURL = baseURL
    }

    // MARK: Injection

    /// The shim with its config filled in; nil only if the bundle lacks the resource.
    nonisolated static func shimSource(config: BridgeConfig, bundle: Bundle = .main) -> String? {
        guard let url = bundle.url(forResource: "openflight-native", withExtension: "js"),
              let template = try? String(contentsOf: url, encoding: .utf8)
        else { return nil }
        return template.replacingOccurrences(of: "__OPENFLIGHT_NATIVE_CONFIG__", with: config.json.jsonString())
    }

    /// Adds the document-start shim and the reply handler to `controller`, both in the page's
    /// own content world (design F11). The handler is held through a weak proxy, because the
    /// user content controller retains its handlers.
    func install(in controller: WKUserContentController) {
        if let source = Self.shimSource(config: config) {
            let script = WKUserScript(
                source: source, injectionTime: .atDocumentStart, forMainFrameOnly: true, in: .page
            )
            controller.addUserScript(script)
        } else {
            assertionFailure("openflight-native.js missing from the app bundle")
        }
        controller.addScriptMessageHandler(WeakReplyHandler(self), contentWorld: .page, name: BridgeProtocol.handlerName)
    }

    // MARK: Requests

    nonisolated func userContentController(
        _ userContentController: WKUserContentController,
        didReceive message: WKScriptMessage,
        replyHandler: @escaping @MainActor @Sendable (Any?, String?) -> Void
    ) {
        MainActor.assumeIsolated {
            let origin = message.frameInfo.securityOrigin
            let allowed = message.frameInfo.isMainFrame
                && OriginPolicy.matches(scheme: origin.protocol, host: origin.host, port: origin.port, base: baseURL)
            let body = message.body
            Task { @MainActor in
                let envelope = await self.handle(body: body, originAllowed: allowed)
                replyHandler(envelope.foundationObject, nil)
            }
        }
    }

    /// Validates and runs one call; always returns an envelope.
    func handle(body: Any?, originAllowed: Bool) async -> JSONValue {
        guard originAllowed else {
            return BridgeEnvelope.failure(BridgeError(.forbidden, "origin not allowed"))
        }
        let request: BridgeRequest
        switch BridgeRequest.parse(body) {
        case .failure(let error): return BridgeEnvelope.failure(error)
        case .success(let value): request = value
        }
        switch request.command {
        case .getSensors:
            switch GetSensorsArguments.parse(request.arguments) {
            case .failure(let error): return BridgeEnvelope.failure(error)
            case .success(let args): return BridgeEnvelope.success(await services.getSensors(args))
            }
        case .fetchWeather:
            switch FetchWeatherArguments.parse(request.arguments) {
            case .failure(let error): return BridgeEnvelope.failure(error)
            case .success(let args): return BridgeEnvelope.from(await services.fetchWeather(args))
            }
        case .setKeepAwake:
            switch SetKeepAwakeArguments.parse(request.arguments) {
            case .failure(let error): return BridgeEnvelope.failure(error)
            case .success(let args): return BridgeEnvelope.success(services.setKeepAwake(args.on))
            }
        case .audioStart:
            switch AudioStartArguments.parse(request.arguments) {
            case .failure(let error): return BridgeEnvelope.failure(error)
            case .success(let args): return BridgeEnvelope.from(await services.audioStart(args))
            }
        case .audioStop:
            return BridgeEnvelope.success(services.audioStop())
        case .audioStatus:
            return BridgeEnvelope.success(services.audioStatus())
        case .audioOnsets:
            switch AudioOnsetsArguments.parse(request.arguments) {
            case .failure(let error): return BridgeEnvelope.failure(error)
            case .success(let args): return BridgeEnvelope.success(services.audioOnsets(args))
            }
        case .audioExportCaptures:
            return BridgeEnvelope.from(await services.audioExportCaptures())
        case .motionStart:
            return BridgeEnvelope.from(services.motionStart())
        case .motionSamples:
            switch MotionSamplesArguments.parse(request.arguments) {
            case .failure(let error): return BridgeEnvelope.failure(error)
            case .success(let args): return BridgeEnvelope.success(services.motionSamples(args))
            }
        case .motionStop:
            return BridgeEnvelope.success(services.motionStop())
        case .speechSpeak:
            switch SpeechSpeakArguments.parse(request.arguments) {
            case .failure(let error): return BridgeEnvelope.failure(error)
            case .success(let args): return BridgeEnvelope.from(services.speechSpeak(args))
            }
        case .speechCancel:
            return BridgeEnvelope.success(services.speechCancel())
        }
    }

    // MARK: Events

    /// The JS that delivers `detail` as `CustomEvent('openflight-native')`.
    nonisolated static func eventScript(_ detail: JSONValue) -> String {
        "window.dispatchEvent(new CustomEvent(\(JSONValue.string(BridgeProtocol.eventName).jsonString()), "
            + "{ detail: \(detail.jsonString()) }));"
    }

    /// Sends one event to the page, only while the page is the configured origin.
    func dispatch(_ detail: JSONValue) {
        guard let webView, let url = webView.url, OriginPolicy.matches(url: url, base: baseURL) else { return }
        webView.evaluateJavaScript(Self.eventScript(detail), in: nil, in: .page) { _ in }
    }
}

/// Breaks the `WKUserContentController` → handler retain cycle.
private final class WeakReplyHandler: NSObject, WKScriptMessageHandlerWithReply {
    weak var target: NativeBridge?

    init(_ target: NativeBridge) {
        self.target = target
    }

    func userContentController(
        _ userContentController: WKUserContentController,
        didReceive message: WKScriptMessage,
        replyHandler: @escaping @MainActor @Sendable (Any?, String?) -> Void
    ) {
        guard let target else {
            let envelope = BridgeEnvelope.failure(BridgeError(.unavailable, "bridge is gone"))
            MainActor.assumeIsolated { replyHandler(envelope.foundationObject, nil) }
            return
        }
        target.userContentController(userContentController, didReceive: message, replyHandler: replyHandler)
    }
}
