import AVFAudio
import Combine
import Foundation
import UIKit

/// Drains the capture ring every 10 ms on the processing queue (design §9.2) and hands the
/// results to the main actor in order.
final class ProcessingLoop: @unchecked Sendable {
    struct Stats: Equatable {
        var onsets = 0
        var lastSeq = 0
        var droppedFrames: Int64 = 0
    }

    static let interval = DispatchTimeInterval.milliseconds(10)

    private let queue: DispatchQueue
    private let processor: CaptureProcessor
    private let deliver: (CaptureProcessor.Output, Stats) -> Void
    private var timer: DispatchSourceTimer?
    private var lastStats = Stats()

    init(queue: DispatchQueue, processor: CaptureProcessor, deliver: @escaping (CaptureProcessor.Output, Stats) -> Void) {
        self.queue = queue
        self.processor = processor
        self.deliver = deliver
    }

    func start() {
        queue.async { [self] in
            let timer = DispatchSource.makeTimerSource(queue: queue)
            timer.schedule(deadline: .now() + Self.interval, repeating: Self.interval, leeway: .milliseconds(2))
            timer.setEventHandler { [weak self] in self?.tick() }
            self.timer = timer
            timer.resume()
        }
    }

    /// Cancels the timer; samples still in the ring are discarded with it.
    func stop() {
        queue.async { [self] in
            timer?.cancel()
            timer = nil
        }
    }

    private func tick() {
        let output = processor.drain()
        let stats = Stats(onsets: processor.onsetCount, lastSeq: processor.lastSeq, droppedFrames: processor.droppedFrames)
        guard !output.onsets.isEmpty || !output.snippets.isEmpty || stats != lastStats else { return }
        lastStats = stats
        deliver(output, stats)
    }
}

/// Owns impact-audio capture (design §9): when it runs, the session and engine, the processing
/// loop, debug WAVs, events to the page and the `audio.*` bridge commands.
///
/// Capture runs while the app is in the foreground, the launch monitor's page is loaded
/// ("connected") and the native setting is on. The page can stop it with `audio.stop()` until
/// its next load or `audio.start()`. Everything it produces is log-only.
@MainActor
final class AudioCaptureController: ObservableObject {
    static let maxStoredOnsets = 500
    static let statusInterval: TimeInterval = 30
    /// More restarts than this within `restartWindow` puts the capture into `error`.
    static let maxRestarts = 5
    static let restartWindow: TimeInterval = 10

    @Published private(set) var status: AudioStatus

    /// Delivers `audio_onset` / `audio_status` to the page (`NativeBridge.dispatch`).
    var eventSink: ((JSONValue) -> Void)?

    let store: CaptureStore
    private let settings: AppSettings
    private let runner: AudioEngineControlling
    private let permission: () -> AudioStatus.Permission
    private let controlQueue = DispatchQueue(label: "openflight.audio.control", qos: .userInitiated)
    private let processingQueue = DispatchQueue(label: "openflight.audio.processing", qos: .utility)
    private let storeQueue = DispatchQueue(label: "openflight.audio.store", qos: .utility)

    private var sceneActive = false
    private var connected = false
    private var pageStopped = false
    /// `audio.start({ debug_capture })` for the captures of this page load.
    private var pageDebugOverride: Bool?
    /// After a failed start: no automatic retry until one of the inputs changes.
    private var blockedAfterFailure = false
    private var hadCapture = false
    private var resumeAfterInterruption = false
    private var restartTimes: [Date] = []
    /// Bumped on every start and stop, so a late callback of an older attempt is ignored.
    private var generation = 0
    private var loop: ProcessingLoop?
    private var statusTimer: Timer?
    private var recentOnsets: [JSONValue] = []
    private var startWaiters: [CheckedContinuation<Void, Never>] = []
    private var observers: [NSObjectProtocol] = []
    private var cancellables: Set<AnyCancellable> = []

    /// `runner` and `permission` are the hardware side; tests pass fakes.
    init(
        settings: AppSettings,
        store: CaptureStore = CaptureStore(root: CaptureStore.defaultRoot),
        runner: AudioEngineControlling = AudioEngineRunner(),
        permission: @escaping () -> AudioStatus.Permission = AudioCaptureController.currentPermission
    ) {
        self.settings = settings
        self.store = store
        self.runner = runner
        self.permission = permission
        var status = AudioStatus()
        status.deviceModel = Self.deviceModel
        status.permission = permission()
        status.debugCapture = settings.debugCaptureEnabled
        self.status = status
        observeSession()
        // `@Published` publishes before the value changes, so the handlers run one turn later.
        settings.$audioCaptureEnabled.dropFirst().removeDuplicates().sink { [weak self] _ in
            DispatchQueue.main.async { MainActor.assumeIsolated { self?.inputsChanged() } }
        }.store(in: &cancellables)
        settings.$debugCaptureEnabled.dropFirst().removeDuplicates().sink { [weak self] _ in
            DispatchQueue.main.async { MainActor.assumeIsolated { self?.debugSettingChanged() } }
        }.store(in: &cancellables)
    }

    static var isInputAvailable: Bool { AVAudioSession.sharedInstance().isInputAvailable }

    // MARK: Inputs

    /// The scene is not in the background (inactive counts as active: see `setScenePhase`).
    func setSceneActive(_ active: Bool) {
        guard active != sceneActive else { return }
        sceneActive = active
        inputsChanged()
    }

    func setConnected(_ value: Bool) {
        guard value != connected else { return }
        connected = value
        inputsChanged()
    }

    /// Follows the web shell's "page loaded" state (`WebShell.isConnected`).
    func follow(connected publisher: Published<Bool>.Publisher) {
        // `@Published` publishes on the main thread, before the property changes; the value
        // passed in is the new one.
        publisher.removeDuplicates().sink { [weak self] value in
            MainActor.assumeIsolated { self?.setConnected(value) }
        }.store(in: &cancellables)
    }

    /// A new document committed in the main frame, before any of its scripts run: an earlier
    /// `audio.stop()` or debug override of the previous page no longer holds. Not at the end of
    /// the load (`didFinish`): by then the new page's own scripts may already have called
    /// `audio.stop()` or `audio.start({ debug_capture })`, and those must hold.
    func pageDidCommit() {
        let changed = pageStopped || pageDebugOverride != nil
        pageStopped = false
        if pageDebugOverride != nil {
            pageDebugOverride = nil
            if isActive, status.debugCapture != effectiveDebugCapture {
                restart()
                return
            }
        }
        if changed { inputsChanged() }
    }

    private var effectiveDebugCapture: Bool { pageDebugOverride ?? settings.debugCaptureEnabled }

    private var wanted: Bool {
        settings.audioCaptureEnabled && sceneActive && connected && !pageStopped
    }

    private var isActive: Bool { status.state == .starting || status.state == .running }

    private func inputsChanged() {
        blockedAfterFailure = false
        reconcile()
    }

    private func debugSettingChanged() {
        if pageDebugOverride == nil, isActive, status.debugCapture != effectiveDebugCapture { restart() }
        if !isActive { status.debugCapture = effectiveDebugCapture }
    }

    private func reconcile() {
        if wanted {
            switch status.state {
            case .idle:
                start()
            case .interrupted:
                // A call or Siri holds the session: wait for the end of the interruption.
                if !resumeAfterInterruption { start() }
            case .error:
                if !blockedAfterFailure { start() }
            case .starting, .running:
                break
            }
        } else if status.state != .idle {
            stop(to: .idle)
        }
        if !wanted || blockedAfterFailure { resumeStartWaiters() }
    }

    // MARK: Start and stop

    private func start() {
        generation += 1
        let attempt = generation
        status.permission = permission()
        switch status.permission {
        case .denied:
            fail(BridgeError(.permissionDenied, "microphone access is denied for OpenFlight"))
            return
        case .undetermined:
            status.state = .starting
            AVAudioApplication.requestRecordPermission { granted in
                DispatchQueue.main.async {
                    MainActor.assumeIsolated {
                        guard attempt == self.generation else { return }
                        self.status.permission = granted ? .granted : .denied
                        if granted { self.start() } else {
                            self.fail(BridgeError(.permissionDenied, "microphone access is denied for OpenFlight"))
                        }
                    }
                }
            }
            return
        case .granted:
            break
        }

        let debug = effectiveDebugCapture
        status.state = .starting
        status.restarted = hadCapture
        status.error = nil
        status.debugCapture = debug
        emitStatus()
        let runner = runner
        controlQueue.async {
            let result = Result { try runner.start() }
            DispatchQueue.main.async {
                MainActor.assumeIsolated { self.didStart(result, attempt: attempt, debug: debug) }
            }
        }
    }

    private func didStart(_ result: Result<AudioEngineRunner.Started, Error>, attempt: Int, debug: Bool) {
        guard attempt == generation else {
            // A stop or restart came in meanwhile. Every generation bump after this attempt's
            // `runner.start()` was queued also queued a `runner.stop` (`stop(to:)`) or a newer
            // `runner.start()` (which stops first) behind it on the serial control queue, so this
            // engine is already taken care of. Stopping it here would queue a stop behind the
            // newer attempt's start and kill that engine instead.
            return
        }
        switch result {
        case .failure(let error):
            stopEngine(deactivate: true)
            fail(error as? BridgeError ?? BridgeError(.internal, "audio start failed: \(error.localizedDescription)"))
        case .success(let started):
            let captureId = UUID().uuidString.lowercased()
            let context = CaptureContext(
                captureId: captureId,
                sampleRate: started.sampleRate,
                inputLatencyMicroseconds: started.inputLatencyMicroseconds,
                debugCapture: debug
            )
            let processor = CaptureProcessor(context: context, ring: started.ring)
            let loop = ProcessingLoop(queue: processingQueue, processor: processor) { [weak self] output, stats in
                DispatchQueue.main.async {
                    MainActor.assumeIsolated { self?.deliver(output, stats: stats, attempt: attempt, context: context) }
                }
            }
            self.loop = loop
            loop.start()
            hadCapture = true
            let clock = ClockPair.capture()
            status.state = .running
            status.captureId = captureId
            status.startedEpochMs = clock.epochMilliseconds
            status.session = started.session
            status.onsets = 0
            status.lastSeq = 0
            status.droppedFrames = 0
            status.error = nil
            prepareStore(context: context, session: started.session)
            emitStatus()
            startStatusTimer()
            resumeStartWaiters()
        }
    }

    /// Stops capture; `.interrupted` keeps the session for the system to resume.
    private func stop(to state: AudioStatus.State) {
        generation += 1
        if state != .interrupted { resumeAfterInterruption = false }
        loop?.stop()
        loop = nil
        statusTimer?.invalidate()
        statusTimer = nil
        stopEngine(deactivate: state != .interrupted)
        let previous = status.state
        status.state = state
        if state == .idle { status.error = nil }
        status.debugCapture = effectiveDebugCapture
        if previous != state { emitStatus() }
        resumeStartWaiters()
    }

    private func stopEngine(deactivate: Bool) {
        let runner = runner
        controlQueue.async { runner.stop(deactivate: deactivate) }
    }

    /// Stops and starts again with a new `capture_id` (`restarted: true`).
    private func restart() {
        let now = Date()
        restartTimes = restartTimes.filter { now.timeIntervalSince($0) < Self.restartWindow } + [now]
        guard restartTimes.count <= Self.maxRestarts else {
            stop(to: .idle)
            fail(BridgeError(.internal, "audio capture kept restarting; stopped"))
            return
        }
        stop(to: .idle)
        reconcile()
    }

    private func fail(_ error: BridgeError) {
        status.state = .error
        status.error = error
        blockedAfterFailure = true
        emitStatus()
        resumeStartWaiters()
    }

    // MARK: Output

    private func deliver(_ output: CaptureProcessor.Output, stats: ProcessingLoop.Stats, attempt: Int, context: CaptureContext) {
        guard attempt == generation else { return }
        for payload in output.onsets {
            recentOnsets.append(payload)
            eventSink?(payload)
        }
        if recentOnsets.count > Self.maxStoredOnsets { recentOnsets.removeFirst(recentOnsets.count - Self.maxStoredOnsets) }
        status.onsets = stats.onsets
        status.lastSeq = stats.lastSeq
        status.droppedFrames = stats.droppedFrames
        guard !output.snippets.isEmpty else { return }
        let store = store
        storeQueue.async {
            for snippet in output.snippets {
                try? store.writeSnippet(
                    captureId: context.captureId, seq: snippet.seq, samples: snippet.samples,
                    sampleRate: context.sampleRate, payload: snippet.payload
                )
            }
        }
    }

    private func prepareStore(context: CaptureContext, session: AudioStatus.Session) {
        let store = store
        let info: JSONValue = .object([
            "capture_id": .string(context.captureId),
            "sample_rate_hz": .number(context.sampleRate),
            "input_latency_us": .integer(context.inputLatencyMicroseconds),
            "io_buffer_ms": .number(session.ioBufferMs),
            "input_port": .text(session.inputPort),
            "input_data_source": .text(session.inputDataSource),
            "session_category": .text(session.category),
            "session_mode": .text(session.mode),
            "snippet_ms": OnsetConfig.v1.thresholds["snippet_ms"] ?? .null,
            "thresholds": OnsetConfig.v1.thresholds,
            "detector_version": .string(OnsetConfig.detectorVersion),
            "device_model": .string(Self.deviceModel),
            "started_epoch_ms": .number(status.startedEpochMs),
        ])
        storeQueue.async {
            store.activeCaptureId = context.debugCapture ? context.captureId : nil
            if context.debugCapture { try? store.writeCaptureInfo(captureId: context.captureId, info: info) }
        }
    }

    private func emitStatus() {
        status.clock = ClockPair.capture()
        eventSink?(status.json)
    }

    private func startStatusTimer() {
        statusTimer?.invalidate()
        statusTimer = Timer.scheduledTimer(withTimeInterval: Self.statusInterval, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.emitStatus() }
        }
    }

    private func resumeStartWaiters() {
        guard status.state != .starting else { return }
        let waiters = startWaiters
        startWaiters.removeAll()
        waiters.forEach { $0.resume() }
    }

    // MARK: Session notifications (design §9.1)

    private func observeSession() {
        let center = NotificationCenter.default
        observers.append(center.addObserver(forName: AVAudioSession.interruptionNotification, object: nil, queue: .main) { [weak self] note in
            let type = (note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt).flatMap(AVAudioSession.InterruptionType.init)
            let options = AVAudioSession.InterruptionOptions(rawValue: note.userInfo?[AVAudioSessionInterruptionOptionKey] as? UInt ?? 0)
            MainActor.assumeIsolated { self?.interruption(type, shouldResume: options.contains(.shouldResume)) }
        })
        observers.append(center.addObserver(forName: AVAudioSession.routeChangeNotification, object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.checkSession() }
        })
        observers.append(center.addObserver(forName: .AVAudioEngineConfigurationChange, object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.checkSession() }
        })
        observers.append(center.addObserver(forName: AVAudioSession.mediaServicesWereResetNotification, object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated {
                guard let self, self.isActive else { return }
                self.restart()
            }
        })
    }

    private func interruption(_ type: AVAudioSession.InterruptionType?, shouldResume: Bool) {
        switch type {
        case .began:
            guard isActive else { return }
            resumeAfterInterruption = true
            stop(to: .interrupted)
        case .ended:
            guard status.state == .interrupted, resumeAfterInterruption else { return }
            resumeAfterInterruption = false
            // Without `.shouldResume` the capture waits for the next foreground transition.
            if shouldResume { reconcile() }
        default:
            break
        }
    }

    /// A route or engine configuration change: the web view's audio (R3), a headset or the
    /// system may have changed the session. Re-applies it by restarting when it no longer
    /// matches or the engine stopped; otherwise only reports the new route.
    private func checkSession() {
        guard status.state == .running else { return }
        let attempt = generation
        let runner = runner
        controlQueue.async {
            let intact = runner.isRunning && runner.sessionIsIntact()
            let session = runner.currentSession()
            DispatchQueue.main.async {
                MainActor.assumeIsolated {
                    guard attempt == self.generation, self.status.state == .running else { return }
                    if intact && session.sampleRateHz == self.status.session.sampleRateHz {
                        self.status.session = session
                        self.emitStatus()
                    } else {
                        self.restart()
                    }
                }
            }
        }
    }

    // MARK: Bridge commands

    func startFromPage(debugCapture: Bool?) async -> Result<JSONValue, BridgeError> {
        guard settings.audioCaptureEnabled else {
            return .failure(BridgeError(.unavailable, "audio capture is switched off in the app settings"))
        }
        connected = true  // the page that calls is loaded from the configured origin
        pageStopped = false
        if let debugCapture {
            let changed = debugCapture != effectiveDebugCapture
            pageDebugOverride = debugCapture
            if changed, isActive { restart() }
        }
        blockedAfterFailure = false
        reconcile()
        // A restart passes through `idle` and starts again before a waiter runs: wait until the
        // capture has settled (running, error, or stopped by something else), not only until
        // the first change.
        while status.state == .starting {
            await withCheckedContinuation { startWaiters.append($0) }
        }
        if status.state == .error, let error = status.error { return .failure(error) }
        return .success(statusJSON())
    }

    func stopFromPage() -> JSONValue {
        pageStopped = true
        reconcile()
        return statusJSON()
    }

    func statusJSON() -> JSONValue {
        var snapshot = status
        snapshot.clock = ClockPair.capture()
        return snapshot.json
    }

    func onsets(captureId: String, afterSeq: Int) -> JSONValue {
        .array(recentOnsets.filter {
            $0["capture_id"] == .string(captureId) && (Self.seq(of: $0) ?? 0) > afterSeq
        })
    }

    private static func seq(of payload: JSONValue) -> Int? {
        if case .int(let value)? = payload["seq"] { return Int(value) }
        return nil
    }

    /// Zips the captures and shows the share sheet; resolves when the sheet closes.
    func exportCaptures() async -> Result<JSONValue, BridgeError> {
        let store = store
        let exported: Result<(URL?, CaptureStore.Summary), Error> = await withCheckedContinuation { continuation in
            storeQueue.async {
                continuation.resume(returning: Result { (try store.exportZip(), store.summary()) })
            }
        }
        switch exported {
        case .failure(let error):
            return .failure(BridgeError(.internal, "export failed: \(error.localizedDescription)"))
        case .success((nil, let summary)):
            return .success(Self.exportResult(shared: false, summary: summary))
        case .success((let url?, let summary)):
            guard let presenter = Self.topViewController() else {
                return .failure(BridgeError(.unavailable, "no window to show the share sheet in"))
            }
            let shared = await withCheckedContinuation { continuation in
                let sheet = UIActivityViewController(activityItems: [url], applicationActivities: nil)
                sheet.completionWithItemsHandler = { _, completed, _, _ in
                    try? FileManager.default.removeItem(at: url)
                    continuation.resume(returning: completed)
                }
                sheet.popoverPresentationController?.sourceView = presenter.view
                presenter.present(sheet, animated: true)
            }
            return .success(Self.exportResult(shared: shared, summary: summary))
        }
    }

    /// File count and size for the settings sheet.
    func captureSummary() async -> CaptureStore.Summary {
        let store = store
        return await withCheckedContinuation { continuation in
            storeQueue.async { continuation.resume(returning: store.summary()) }
        }
    }

    private static func exportResult(shared: Bool, summary: CaptureStore.Summary) -> JSONValue {
        .object(["shared": .bool(shared), "files": .integer(summary.files), "bytes": .integer(summary.bytes)])
    }

    private static func topViewController() -> UIViewController? {
        let scene = UIApplication.shared.connectedScenes
            .compactMap { $0 as? UIWindowScene }
            .first { $0.activationState == .foregroundActive } ?? UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first
        var top = scene?.windows.first { $0.isKeyWindow }?.rootViewController ?? scene?.windows.first?.rootViewController
        while let presented = top?.presentedViewController, !presented.isBeingDismissed { top = presented }
        return top
    }

    // MARK: Helpers

    static func currentPermission() -> AudioStatus.Permission {
        switch AVAudioApplication.shared.recordPermission {
        case .granted: return .granted
        case .denied: return .denied
        case .undetermined: return .undetermined
        @unknown default: return .undetermined
        }
    }

    /// `iPhone18,2`-style identifier (the Simulator reports the simulated model).
    static let deviceModel: String = {
        if let simulated = ProcessInfo.processInfo.environment["SIMULATOR_MODEL_IDENTIFIER"] { return simulated }
        var info = utsname()
        uname(&info)
        return withUnsafeBytes(of: &info.machine) { raw in
            String(decoding: raw.prefix { $0 != 0 }, as: UTF8.self)
        }
    }()
}
