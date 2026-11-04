import Combine
import Foundation
import SwiftUI
import UIKit

/// The app's `BridgeServices`: sensors, weather, keep-awake, impact audio and spoken callouts,
/// wired together once per run.
@MainActor
final class CompanionServices: BridgeServices {
    let keepAwake: KeepAwake
    let network: NetworkPaths
    let sensors: SensorHub
    let weather: WeatherService
    let audio: AudioCaptureController
    let motion: MotionProvider
    let speech: SpeechService
    let capabilities: BridgeCapabilities
    private var keepScreenOnSubscription: AnyCancellable?

    static var appVersion: String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0"
    }

    /// `applicationNameForUserAgent`, so server logs can tell the shell from Safari (design §6).
    static var userAgentToken: String { "OpenFlightCompanion/\(appVersion)" }

    init(settings: AppSettings) {
        keepAwake = KeepAwake(userEnabled: settings.keepScreenOn)
        let network = NetworkPaths()
        self.network = network
        let audio = AudioCaptureController(settings: settings)
        self.audio = audio
        let sensors = SensorHub(network: network, keepAwake: keepAwake, audioSummary: { audio.status.summary })
        self.sensors = sensors
        weather = WeatherService(.init(
            cellular: CellularWeatherTransport(),
            fallback: DefaultRouteWeatherTransport(),
            networkState: { network.state },
            phonePressureHpa: { await sensors.phonePressureHpa },
            userAgent: "\(Self.userAgentToken) (iOS)"
        ))
        let motion = MotionProvider()
        self.motion = motion
        // Reads the capture state on each callout, so the session is changed only while the
        // capture does not own it.
        speech = SpeechService(captureState: { [weak audio] in audio?.status.state ?? .idle })
        capabilities = BridgeCapabilities(
            barometer: BarometerProvider.isAvailable,
            absoluteAltitude: BarometerProvider.isAbsoluteAvailable,
            // Every iPhone has location hardware. Whether location services are on is reported
            // per snapshot (`location: null`), because that check must not run on the main thread.
            location: true,
            weather: true,
            audio: AudioCaptureController.isInputAvailable,
            keepAwake: true,
            debugCapture: true,
            motion: motion.isAvailable,
            speech: true
        )
        keepScreenOnSubscription = settings.$keepScreenOn.dropFirst().removeDuplicates()
            .sink { [weak keepAwake] on in keepAwake?.setUserEnabled(on) }
    }

    var bridgeConfig: BridgeConfig {
        BridgeConfig(appVersion: Self.appVersion, capabilities: capabilities)
    }

    func setScenePhase(_ phase: ScenePhase) {
        let active = phase == .active
        keepAwake.setScenePhase(phase)
        sensors.setForeground(active)
        // Audio stops only in the background: a system alert (the microphone prompt itself,
        // Control Center) makes the scene inactive for a moment, and must not cut the capture.
        audio.setSceneActive(phase != .background)
        if phase == .background {
            motion.stop()
            speech.cancel()
        }
    }

    /// A new document starts over: nothing keeps the accelerometer running or speaks for the old page.
    func pageDidCommit() {
        audio.pageDidCommit()
        motion.stop()
        speech.cancel()
    }

    // MARK: BridgeServices

    func getSensors(_ args: GetSensorsArguments) async -> JSONValue {
        await sensors.snapshot(args).json
    }

    func fetchWeather(_ args: FetchWeatherArguments) async -> Result<JSONValue, BridgeError> {
        await weather.fetch(args)
    }

    func setKeepAwake(_ on: Bool) -> JSONValue {
        keepAwake.setRequested(on)
        return .object(["keep_awake": .bool(keepAwake.requested)])
    }

    func audioStart(_ args: AudioStartArguments) async -> Result<JSONValue, BridgeError> {
        guard capabilities.audio else { return .failure(BridgeError(.unavailable, "no audio input")) }
        return await audio.startFromPage(debugCapture: args.debugCapture)
    }

    func audioStop() -> JSONValue { audio.stopFromPage() }

    func audioStatus() -> JSONValue { audio.statusJSON() }

    func audioOnsets(_ args: AudioOnsetsArguments) -> JSONValue {
        audio.onsets(captureId: args.captureId, afterSeq: args.afterSeq)
    }

    func audioExportCaptures() async -> Result<JSONValue, BridgeError> {
        await audio.exportCaptures()
    }

    func motionStart() -> Result<JSONValue, BridgeError> {
        guard capabilities.motion else { return .failure(BridgeError(.unavailable, "no accelerometer")) }
        return motion.start()
    }

    func motionSamples(_ args: MotionSamplesArguments) -> JSONValue { motion.samples(after: args.afterSeq) }

    func motionStop() -> JSONValue { motion.stop() }

    func speechSpeak(_ args: SpeechSpeakArguments) -> Result<JSONValue, BridgeError> { speech.speak(args) }

    func speechCancel() -> JSONValue { speech.cancel() }
}
