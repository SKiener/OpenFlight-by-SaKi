import AVFAudio
import XCTest
@testable import OpenFlightCompanion

@MainActor
final class SpeechServiceTests: XCTestCase {
    private var synthesizer: FakeSynthesizer!
    private var session: FakeSpeechSession!
    private var captureState: AudioStatus.State = .idle
    private var service: SpeechService!

    override func setUp() async throws {
        synthesizer = FakeSynthesizer()
        session = FakeSpeechSession()
        captureState = .idle
        service = SpeechService(synthesizer: synthesizer, session: session, captureState: { [unowned self] in captureState })
    }

    private func args(_ text: String, _ lang: String = "de-DE", interrupt: Bool = false) -> SpeechSpeakArguments {
        SpeechSpeakArguments(text: text, lang: lang, interrupt: interrupt)
    }

    // MARK: Speaking

    func testQueuesCalloutsInOrder() throws {
        let reply = try service.speak(args("Carry 151 Meter")).get()
        _ = try service.speak(args("Carry 160 Meter")).get()

        XCTAssertEqual(reply, .object(["speaking": .bool(true)]))
        XCTAssertEqual(synthesizer.log, ["speak:Carry 151 Meter", "speak:Carry 160 Meter"])
    }

    func testInterruptCutsOffFirst() throws {
        _ = try service.speak(args("Carry 151 Meter")).get()
        _ = try service.speak(args("Carry 165 Meter", interrupt: true)).get()

        XCTAssertEqual(synthesizer.log, ["speak:Carry 151 Meter", "stop", "speak:Carry 165 Meter"])
    }

    func testCancelStopsNow() {
        XCTAssertEqual(service.cancel(), .object(["speaking": .bool(false)]))
        XCTAssertEqual(synthesizer.log, ["stop"])
        XCTAssertEqual(synthesizer.boundaries, [.immediate])
    }

    func testUsesTheDefaultVoiceOfTheLanguage() throws {
        let utterance = SpeechService.utterance(for: args("Carry 151 Meter", "de-DE"))
        XCTAssertEqual(utterance.speechString, "Carry 151 Meter")
        let expected = AVSpeechSynthesisVoice(language: "de-DE")
        XCTAssertEqual(utterance.voice?.identifier, expected?.identifier)
        if let voice = utterance.voice { XCTAssertTrue(voice.language.hasPrefix("de")) }
    }

    func testTheAppSynthesizerSpeaksThroughTheAppSession() {
        XCTAssertTrue(SpeechService.makeSynthesizer().usesApplicationAudioSession)
    }

    // MARK: Audio session

    func testWithoutTheCaptureSwitchesToPlaybackSoTheSilentSwitchDoesNotMute() throws {
        session.category = .soloAmbient
        _ = try service.speak(args("Carry 151 Meter")).get()

        XCTAssertEqual(session.activations, [SpeechSessionPolicy.playbackOptions])
        XCTAssertEqual(session.category, .playback)

        // Already set: not activated again for every callout.
        _ = try service.speak(args("Carry 160 Meter")).get()
        XCTAssertEqual(session.activations.count, 1)
    }

    func testLeavesTheRunningCaptureSessionAlone() throws {
        for state in [AudioStatus.State.running, .starting] {
            captureState = state
            session.category = .playAndRecord
            session.categoryOptions = AudioEngineRunner.categoryOptions
            _ = try service.speak(args("Carry 151 Meter")).get()
            XCTAssertEqual(session.activations, [], "\(state) must keep the capture's session")
        }
        XCTAssertEqual(synthesizer.log.count, 2, "and still speaks")
    }

    func testTakesTheSessionBackAfterTheCaptureStopped() throws {
        // A stopped capture leaves its category behind (inactive).
        for state in [AudioStatus.State.idle, .error, .interrupted] {
            session.reset(category: .playAndRecord, options: AudioEngineRunner.categoryOptions)
            captureState = state
            _ = try service.speak(args("Carry 151 Meter")).get()
            XCTAssertEqual(session.activations, [SpeechSessionPolicy.playbackOptions], "\(state)")
        }
    }

    func testStillSpeaksWhenTheSessionCannotBeActivated() throws {
        session.failure = BridgeError(.unavailable, "session busy")
        _ = try service.speak(args("Carry 151 Meter")).get()
        XCTAssertEqual(synthesizer.log, ["speak:Carry 151 Meter"])
    }

    func testPolicy() {
        let playback = SpeechSessionPolicy.playbackOptions
        XCTAssertEqual(SpeechSessionPolicy.action(captureState: .running, category: .playAndRecord, options: AudioEngineRunner.categoryOptions), .keep)
        XCTAssertEqual(SpeechSessionPolicy.action(captureState: .starting, category: .soloAmbient, options: []), .keep)
        XCTAssertEqual(SpeechSessionPolicy.action(captureState: .idle, category: .playback, options: playback), .keep)
        XCTAssertEqual(SpeechSessionPolicy.action(captureState: .idle, category: .playback, options: [.duckOthers]), .usePlayback)
        XCTAssertEqual(SpeechSessionPolicy.action(captureState: .idle, category: .ambient, options: playback), .usePlayback)
        XCTAssertEqual(SpeechSessionPolicy.action(captureState: .error, category: .soloAmbient, options: []), .usePlayback)
    }

    /// The sessions a callout can play through: both reach the loudspeaker and mix with music.
    func testSessionOptionsPlayAloudOnTheLoudspeaker() {
        XCTAssertTrue(AudioEngineRunner.categoryOptions.contains(.defaultToSpeaker), "not the receiver")
        XCTAssertTrue(AudioEngineRunner.categoryOptions.contains(.mixWithOthers))
        XCTAssertEqual(AudioEngineRunner.categoryOptions, [.defaultToSpeaker, .mixWithOthers], "unchanged for the onset capture")
        XCTAssertEqual(SpeechSessionPolicy.playbackOptions, [.mixWithOthers])
    }
}

private final class FakeSynthesizer: SpeechSynthesizing {
    var log: [String] = []
    var boundaries: [AVSpeechBoundary] = []

    func speak(_ utterance: AVSpeechUtterance) {
        log.append("speak:\(utterance.speechString)")
    }

    func stopSpeaking(at boundary: AVSpeechBoundary) -> Bool {
        log.append("stop")
        boundaries.append(boundary)
        return true
    }
}

private final class FakeSpeechSession: SpeechAudioSessionControlling {
    var category: AVAudioSession.Category = .soloAmbient
    var categoryOptions: AVAudioSession.CategoryOptions = []
    var activations: [AVAudioSession.CategoryOptions] = []
    var failure: Error?

    func reset(category: AVAudioSession.Category, options: AVAudioSession.CategoryOptions) {
        self.category = category
        categoryOptions = options
        activations = []
    }

    func activatePlayback(options: AVAudioSession.CategoryOptions) throws {
        if let failure { throw failure }
        activations.append(options)
        category = .playback
        categoryOptions = options
    }
}
