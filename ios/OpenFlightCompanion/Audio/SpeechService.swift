import AVFAudio
import Foundation

/// The shared session as the speech side sees it. `SystemSpeechAudioSession` in the app; tests
/// substitute a fake.
protocol SpeechAudioSessionControlling: AnyObject {
    var category: AVAudioSession.Category { get }
    var categoryOptions: AVAudioSession.CategoryOptions { get }
    func activatePlayback(options: AVAudioSession.CategoryOptions) throws
}

final class SystemSpeechAudioSession: SpeechAudioSessionControlling {
    var category: AVAudioSession.Category { AVAudioSession.sharedInstance().category }
    var categoryOptions: AVAudioSession.CategoryOptions { AVAudioSession.sharedInstance().categoryOptions }

    func activatePlayback(options: AVAudioSession.CategoryOptions) throws {
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(.playback, mode: .default, options: options)
        try session.setActive(true)
    }
}

/// What the shared session must be for a callout to be heard on the loudspeaker, with the
/// ring/silent switch on. Pure, so tests cover every capture state.
enum SpeechSessionPolicy {
    enum Action: Equatable {
        /// The session already plays aloud: leave it alone.
        case keep
        /// Switch to `.playback` with `playbackOptions` and activate it.
        case usePlayback
    }

    /// Mixed with other audio, as the capture session is; never ducks or stops the owner's music.
    static let playbackOptions: AVAudioSession.CategoryOptions = [.mixWithOthers]

    static func action(
        captureState: AudioStatus.State,
        category: AVAudioSession.Category,
        options: AVAudioSession.CategoryOptions
    ) -> Action {
        switch captureState {
        case .starting, .running:
            // The impact capture owns the session (`.playAndRecord`, `.measurement`,
            // `AudioEngineRunner.categoryOptions`): it ignores the silent switch and its
            // `.defaultToSpeaker` plays on the loudspeaker. Changing the category would cut
            // the onset capture (`sessionIsIntact`), and a start in flight sets its own.
            return .keep
        case .idle, .interrupted, .error:
            // Without the capture the session is the default `.soloAmbient`, which the silent
            // switch mutes, or whatever the web view's audio left behind.
            return category == .playback && options == playbackOptions ? .keep : .usePlayback
        }
    }
}

/// The part of `AVSpeechSynthesizer` the service uses.
protocol SpeechSynthesizing: AnyObject {
    func speak(_ utterance: AVSpeechUtterance)
    @discardableResult func stopSpeaking(at boundary: AVSpeechBoundary) -> Bool
}

extension AVSpeechSynthesizer: SpeechSynthesizing {}

/// Spoken shot callouts for the page (`speech.*`): AVSpeechSynthesizer with the language's
/// default voice, as the web version only sets `lang`. Utterances queue in the synthesizer, so
/// the page's shot order holds. Unlike Web Speech in Safari no priming tap is needed.
@MainActor
final class SpeechService {
    private let synthesizer: SpeechSynthesizing
    private let session: SpeechAudioSessionControlling
    private let captureState: () -> AudioStatus.State

    init(
        synthesizer: SpeechSynthesizing = SpeechService.makeSynthesizer(),
        session: SpeechAudioSessionControlling = SystemSpeechAudioSession(),
        captureState: @escaping () -> AudioStatus.State
    ) {
        self.synthesizer = synthesizer
        self.session = session
        self.captureState = captureState
    }

    nonisolated static func makeSynthesizer() -> AVSpeechSynthesizer {
        let synthesizer = AVSpeechSynthesizer()
        // Speak through the app's session, which `prepareSession` keeps audible.
        synthesizer.usesApplicationAudioSession = true
        return synthesizer
    }

    /// The utterance for one callout: the text in the default voice for `lang` (nil, the
    /// system voice, when that language has none installed).
    nonisolated static func utterance(for args: SpeechSpeakArguments) -> AVSpeechUtterance {
        let utterance = AVSpeechUtterance(string: args.text)
        utterance.voice = AVSpeechSynthesisVoice(language: args.lang)
        return utterance
    }

    func speak(_ args: SpeechSpeakArguments) -> Result<JSONValue, BridgeError> {
        prepareSession()
        if args.interrupt { synthesizer.stopSpeaking(at: .immediate) }
        synthesizer.speak(Self.utterance(for: args))
        return .success(.object(["speaking": .bool(true)]))
    }

    /// Stops now and drops anything queued (page hidden, callouts off, new page).
    @discardableResult
    func cancel() -> JSONValue {
        synthesizer.stopSpeaking(at: .immediate)
        return .object(["speaking": .bool(false)])
    }

    private func prepareSession() {
        let action = SpeechSessionPolicy.action(
            captureState: captureState(), category: session.category, options: session.categoryOptions
        )
        guard action == .usePlayback else { return }
        do {
            try session.activatePlayback(options: SpeechSessionPolicy.playbackOptions)
        } catch {
            // Still speak: at worst the silent switch mutes this callout, as in Safari.
            print("[speech] could not activate the playback session: \(error)")
        }
    }
}
