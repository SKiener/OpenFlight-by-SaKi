import WebKit
import XCTest
@testable import OpenFlightCompanion

/// Stands in for `AudioEngineRunner`: no session, no hardware. `start()` can be held at a gate
/// so a test decides when the control queue gets past it.
final class FakeAudioEngine: AudioEngineControlling, @unchecked Sendable {
    private let lock = NSLock()
    private var running = false
    private var holdStarts = false
    private var startCount = 0
    private var stopsAfterLastStart = 0
    private let gate = DispatchSemaphore(value: 0)

    init(holdStarts: Bool = false) {
        self.holdStarts = holdStarts
    }

    var isRunning: Bool { lock.withLock { running } }
    var starts: Int { lock.withLock { startCount } }
    /// `stop` calls since the newest engine started (a stale stop shows up here).
    var stopsSinceLastStart: Int { lock.withLock { stopsAfterLastStart } }

    /// Lets every held and future `start()` through.
    func release() {
        lock.withLock { holdStarts = false }
        for _ in 0..<8 { gate.signal() }
    }

    func start() throws -> AudioEngineRunner.Started {
        stop(deactivate: false)
        if lock.withLock({ holdStarts }) { _ = gate.wait(timeout: .now() + 5) }
        lock.withLock {
            running = true
            startCount += 1
            stopsAfterLastStart = 0
        }
        guard let ring = CaptureRing(sampleCapacity: 48_000, stampCapacity: 64) else {
            throw BridgeError(.internal, "ring")
        }
        return AudioEngineRunner.Started(ring: ring, sampleRate: 48_000, session: currentSession(), inputLatencyMicroseconds: 0)
    }

    func stop(deactivate: Bool) {
        lock.withLock {
            running = false
            stopsAfterLastStart += 1
        }
    }

    func sessionIsIntact() -> Bool { true }

    func currentSession() -> AudioStatus.Session {
        AudioStatus.Session(sampleRateHz: 48_000, ioBufferMs: 5, inputLatencyMs: 0, inputPort: "MicrophoneBuiltIn",
                            inputDataSource: nil, category: "AVAudioSessionCategoryPlayAndRecord", mode: "AVAudioSessionModeMeasurement")
    }
}

@MainActor
private func waitUntil(_ timeout: TimeInterval = 5, _ condition: () -> Bool) async throws {
    let deadline = Date().addingTimeInterval(timeout)
    while Date() < deadline {
        if condition() { return }
        try await Task.sleep(nanoseconds: 20_000_000)
    }
}

@MainActor
private func makeController(engine: FakeAudioEngine) throws -> (AudioCaptureController, AppSettings) {
    let suite = "AudioControllerTests-\(UUID().uuidString)"
    let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
    let settings = AppSettings(defaults: defaults)
    let root = FileManager.default.temporaryDirectory.appendingPathComponent("audio-controller-\(UUID().uuidString)")
    let audio = AudioCaptureController(settings: settings, store: CaptureStore(root: root), runner: engine, permission: { .granted })
    return (audio, settings)
}

@MainActor
final class AudioControllerTests: XCTestCase {
    func testRestartWhileStartingEndsWithTheNewestEngineRunning() async throws {
        let engine = FakeAudioEngine(holdStarts: true)
        let (audio, settings) = try makeController(engine: engine)
        var states: [String] = []
        audio.eventSink = { if case .string(let state)? = $0["state"] { states.append(state) } }

        audio.setSceneActive(true)
        audio.setConnected(true)  // auto start: `.starting`, the first start() waits at the gate
        settings.debugCaptureEnabled = true  // restart while still starting
        try await waitUntil { states.count >= 3 }
        XCTAssertEqual(states, ["starting", "idle", "starting"])

        engine.release()
        try await waitUntil { audio.status.state == .running }
        // Let anything still queued behind the newest start run.
        try await Task.sleep(nanoseconds: 300_000_000)
        XCTAssertEqual(audio.status.state, .running)
        XCTAssertTrue(audio.status.debugCapture)
        XCTAssertTrue(engine.isRunning, "a stale attempt's stop killed the newest engine")
        XCTAssertEqual(engine.stopsSinceLastStart, 0)
        XCTAssertEqual(engine.starts, 2)

        audio.setSceneActive(false)
        try await waitUntil { !engine.isRunning }
        XCTAssertEqual(audio.status.state, .idle)
    }

    func testStopWhileStartingLeavesTheEngineStopped() async throws {
        let engine = FakeAudioEngine(holdStarts: true)
        let (audio, _) = try makeController(engine: engine)
        audio.eventSink = { _ in }
        audio.setSceneActive(true)
        audio.setConnected(true)
        XCTAssertEqual(audio.status.state, .starting)
        audio.setSceneActive(false)  // background while the start is still in flight
        XCTAssertEqual(audio.status.state, .idle)
        engine.release()
        try await Task.sleep(nanoseconds: 300_000_000)
        XCTAssertEqual(audio.status.state, .idle)
        XCTAssertFalse(engine.isRunning, "the abandoned attempt's engine kept running")
    }

    func testPageStartWaitsThroughARestartUntilRunning() async throws {
        let engine = FakeAudioEngine(holdStarts: true)
        let (audio, _) = try makeController(engine: engine)
        audio.eventSink = { _ in }
        audio.setSceneActive(true)
        audio.setConnected(true)  // auto start without debug, held at the gate
        let page = Task { await audio.startFromPage(debugCapture: true) }  // restarts with debug
        try await Task.sleep(nanoseconds: 100_000_000)
        engine.release()
        let reply = await page.value
        guard case .success(let status) = reply else { return XCTFail("start failed: \(reply)") }
        XCTAssertEqual(status["state"], .string("running"))
        XCTAssertEqual(status["debug_capture"], .bool(true))
    }
}

/// Bridge services whose `audio.*` commands go to a real `AudioCaptureController`.
@MainActor
private final class AudioOnlyServices: BridgeServices {
    let audio: AudioCaptureController

    init(audio: AudioCaptureController) { self.audio = audio }

    func getSensors(_ args: GetSensorsArguments) async -> JSONValue { .null }
    func fetchWeather(_ args: FetchWeatherArguments) async -> Result<JSONValue, BridgeError> {
        .failure(BridgeError(.network, "offline in tests"))
    }
    func setKeepAwake(_ on: Bool) -> JSONValue { .null }
    func audioStart(_ args: AudioStartArguments) async -> Result<JSONValue, BridgeError> {
        await audio.startFromPage(debugCapture: args.debugCapture)
    }
    func audioStop() -> JSONValue { audio.stopFromPage() }
    func audioStatus() -> JSONValue { audio.statusJSON() }
    func audioOnsets(_ args: AudioOnsetsArguments) -> JSONValue { .array([]) }
    func audioExportCaptures() async -> Result<JSONValue, BridgeError> { .failure(BridgeError(.unavailable, "tests")) }
    func motionStart() -> Result<JSONValue, BridgeError> { .failure(BridgeError(.unavailable, "tests")) }
    func motionSamples(_ args: MotionSamplesArguments) -> JSONValue { .null }
    func motionStop() -> JSONValue { .null }
    func speechSpeak(_ args: SpeechSpeakArguments) -> Result<JSONValue, BridgeError> { .failure(BridgeError(.unavailable, "tests")) }
    func speechCancel() -> JSONValue { .null }
}

/// A real `WKWebView` wired as the app wires it: what a page asks for during its own load holds.
@MainActor
final class AudioPageLoadTests: XCTestCase {
    private static let base = URL(string: "http://openflight.test:8080/")!
    private var engine: FakeAudioEngine!
    private var audio: AudioCaptureController!
    private var services: AudioOnlyServices!
    private var shell: WebShell!

    override func setUp() async throws {
        engine = FakeAudioEngine()
        audio = try makeController(engine: engine).0
        audio.eventSink = { _ in }
        services = AudioOnlyServices(audio: audio)
        shell = WebShell(services: services, config: FakeServices.config, baseURL: Self.base)
        shell.onPageCommit = { [weak audio] in audio?.pageDidCommit() }
        audio.follow(connected: shell.$isConnected)
        audio.setSceneActive(true)
        let scene = UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first
        scene?.windows.first?.addSubview(shell.webView)
        shell.webView.frame = CGRect(x: 0, y: 0, width: 10, height: 10)
    }

    override func tearDown() async throws {
        audio.setSceneActive(false)
        shell.webView.removeFromSuperview()
        shell = nil
        services = nil
        audio = nil
        engine = nil
    }

    private func load(_ script: String) async throws {
        let page = "<!doctype html><html><head><script>\(script)</script></head><body>x</body></html>"
        shell.webView.loadHTMLString(page, baseURL: Self.base)
        let deadline = Date().addingTimeInterval(10)
        while Date() < deadline {
            if shell.isConnected, !shell.webView.isLoading,
               (try? await js("return document.readyState")) as? String == "complete" { break }
            try await Task.sleep(nanoseconds: 20_000_000)
        }
        // didFinish and anything it set off.
        try await Task.sleep(nanoseconds: 300_000_000)
    }

    private func js(_ body: String) async throws -> Any? {
        try await shell.webView.callAsyncJavaScript(body, arguments: [:], in: nil, contentWorld: .page)
    }

    func testInlineStopHoldsAfterThePageFinishesLoading() async throws {
        try await load("window.__stop = 'pending'; openflightNative.audio.stop().then(function (s) { window.__stop = s.state; });")
        let reply = try await js("return window.__stop")
        XCTAssertEqual(reply as? String, "idle")
        XCTAssertEqual(audio.status.state, .idle, "didFinish undid the page's own audio.stop()")
        XCTAssertEqual(engine.starts, 0)

        // The next load starts over: without a stop, capture runs.
        try await load("window.__x = 1;")
        try await waitUntil { audio.status.state == .running }
        XCTAssertEqual(audio.status.state, .running)
    }

    func testInlineStartWithDebugHoldsAfterThePageFinishesLoading() async throws {
        try await load("""
            window.__start = 'pending';
            openflightNative.audio.start({ debug_capture: true })
              .then(function (s) { window.__start = s.state + '/' + s.debug_capture; },
                    function (e) { window.__start = 'error ' + e.code; });
            """)
        try await waitUntil { audio.status.state == .running }
        let reply = try await js("return window.__start")
        XCTAssertEqual(reply as? String, "running/true")
        XCTAssertEqual(audio.status.state, .running)
        XCTAssertTrue(audio.status.debugCapture, "didFinish wiped the page's debug override")
        XCTAssertEqual(engine.starts, 1, "the load restarted the capture")
        XCTAssertTrue(engine.isRunning)

        // A new document drops the override: the capture restarts with the native setting.
        try await load("window.__x = 1;")
        try await waitUntil { audio.status.state == .running && !audio.status.debugCapture }
        XCTAssertFalse(audio.status.debugCapture)
        XCTAssertTrue(engine.isRunning)
    }
}
