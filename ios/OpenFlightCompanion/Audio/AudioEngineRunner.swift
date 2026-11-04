import AVFAudio
import Foundation

/// What `AudioCaptureController` drives on its serial control queue: the session and engine.
/// `AudioEngineRunner` in the app; tests substitute a fake to control the timing of `start()`.
protocol AudioEngineControlling: AnyObject, Sendable {
    var isRunning: Bool { get }
    /// Stops any running engine first, then configures the session and starts a new engine.
    func start() throws -> AudioEngineRunner.Started
    func stop(deactivate: Bool)
    func sessionIsIntact() -> Bool
    func currentSession() -> AudioStatus.Session
}

/// The AVAudioSession and AVAudioEngine side of the capture (design §9.1). Not thread-safe and
/// may block (session activation): `AudioCaptureController` calls it only from its control queue.
final class AudioEngineRunner: AudioEngineControlling, @unchecked Sendable {
    static let preferredSampleRate = 48_000.0
    static let preferredIOBufferDuration = 0.005
    /// `.defaultToSpeaker`: output goes to the loudspeaker, not the receiver (the spoken shot
    /// callouts play through this session while the capture runs). No Bluetooth options, so a
    /// headset never takes over the input (design §9.1).
    static let categoryOptions: AVAudioSession.CategoryOptions = [.defaultToSpeaker, .mixWithOthers]

    struct Started {
        let ring: CaptureRing
        let sampleRate: Double
        let session: AudioStatus.Session
        let inputLatencyMicroseconds: Int64
    }

    private var engine: AVAudioEngine?
    private var sink: AVAudioSinkNode?
    /// Kept until the engine has stopped: the IO thread writes into it through a raw pointer.
    private var ring: CaptureRing?

    var isRunning: Bool { engine?.isRunning ?? false }

    /// Configures and activates the session, then starts `inputNode → AVAudioSinkNode`.
    func start() throws -> Started {
        stop(deactivate: false)
        let session = AVAudioSession.sharedInstance()
        try Self.configure(session)
        try session.setActive(true)

        let engine = AVAudioEngine()
        let input = engine.inputNode
        // Voice processing (AGC, echo cancellation) stays off: it is a separate opt-in (F12).
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else {
            throw BridgeError(.unavailable, "no audio input")
        }
        guard format.commonFormat == .pcmFormatFloat32 else {
            throw BridgeError(.internal, "unexpected input format \(format)")
        }
        guard let ring = CaptureRing.forCapture(sampleRate: format.sampleRate) else {
            throw BridgeError(.internal, "could not allocate the capture ring")
        }
        let pointer = ring.pointer
        let stride: UInt32 = format.isInterleaved ? format.channelCount : 1

        // The realtime block: a copy into the preallocated ring and one stamp record. It never
        // allocates, locks, logs or touches a Swift object (design §9.2).
        let sink = AVAudioSinkNode { timestamp, frameCount, bufferList in
            let buffers = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: bufferList))
            let data: UnsafePointer<Float>? = buffers.count > 0
                ? UnsafePointer(buffers[0].mData?.assumingMemoryBound(to: Float.self)) : nil
            let stamp = timestamp.pointee
            var flags: UInt32 = 0
            if stamp.mFlags.contains(.sampleTimeValid) { flags |= UInt32(OFL_STAMP_SAMPLE_TIME_VALID) }
            let hostValid = stamp.mFlags.contains(.hostTimeValid)
            if hostValid { flags |= UInt32(OFL_STAMP_HOST_TIME_VALID) }
            ofl_ring_write(pointer, data, frameCount, stride, stamp.mSampleTime, hostValid ? stamp.mHostTime : mach_absolute_time(), flags)
            return noErr
        }
        engine.attach(sink)
        engine.connect(input, to: sink, format: format)
        engine.prepare()
        do {
            try engine.start()
        } catch {
            engine.detach(sink)
            throw error
        }
        self.engine = engine
        self.sink = sink
        self.ring = ring
        return Started(
            ring: ring,
            sampleRate: format.sampleRate,
            session: Self.describe(session),
            inputLatencyMicroseconds: Int64((session.inputLatency * 1e6).rounded())
        )
    }

    /// Stops the engine (synchronously: the IO thread is done with the ring afterwards) and,
    /// with `deactivate`, hands the session back so the web page's audio plays as before.
    func stop(deactivate: Bool) {
        if let engine {
            engine.stop()
            if let sink { engine.detach(sink) }
        }
        engine = nil
        sink = nil
        ring = nil
        if deactivate {
            try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        }
    }

    /// Whether the shared session still has our category, mode and a built-in input. The web
    /// view's own audio may reconfigure it (risk R3).
    func sessionIsIntact() -> Bool {
        let session = AVAudioSession.sharedInstance()
        return session.category == .playAndRecord && session.mode == .measurement
            && session.currentRoute.inputs.contains { $0.portType == .builtInMic }
    }

    func currentSession() -> AudioStatus.Session {
        Self.describe(AVAudioSession.sharedInstance())
    }

    private static func configure(_ session: AVAudioSession) throws {
        try session.setCategory(.playAndRecord, mode: .measurement, options: categoryOptions)
        // Requests: the hardware may give something else, which `describe` reads back (F13).
        try? session.setPreferredSampleRate(preferredSampleRate)
        try? session.setPreferredIOBufferDuration(preferredIOBufferDuration)
        if let builtIn = session.availableInputs?.first(where: { $0.portType == .builtInMic }) {
            try? session.setPreferredInput(builtIn)
        }
    }

    private static func describe(_ session: AVAudioSession) -> AudioStatus.Session {
        let input = session.currentRoute.inputs.first
        return AudioStatus.Session(
            sampleRateHz: session.sampleRate,
            ioBufferMs: session.ioBufferDuration * 1_000,
            inputLatencyMs: session.inputLatency * 1_000,
            inputPort: input?.portType.rawValue,
            inputDataSource: session.inputDataSource?.dataSourceName ?? input?.selectedDataSource?.dataSourceName,
            category: session.category.rawValue,
            mode: session.mode.rawValue
        )
    }
}
