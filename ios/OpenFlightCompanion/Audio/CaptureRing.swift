import Foundation

/// Owns one C capture ring (`SPSCRing.h`, design §9.2) and is its consumer-side API.
///
/// The realtime sink block does not go through this class: it captures `pointer` and calls
/// `ofl_ring_write` directly, so the IO thread never touches a Swift object. `write` here is the
/// same call for tests. The ring must outlive the engine that writes into it: the engine runner
/// keeps this object until the engine has stopped.
final class CaptureRing: @unchecked Sendable {
    /// `OFLStampRecord.flags` bits, for callers outside the bridging header (tests).
    static let sampleTimeValid = UInt32(OFL_STAMP_SAMPLE_TIME_VALID)
    static let hostTimeValid = UInt32(OFL_STAMP_HOST_TIME_VALID)

    let pointer: OpaquePointer
    let sampleCapacity: Int
    let stampCapacity: Int

    init?(sampleCapacity: Int, stampCapacity: Int) {
        guard sampleCapacity > 0, stampCapacity > 0, sampleCapacity <= Int(UInt32.max), stampCapacity <= Int(UInt32.max),
              let ring = ofl_ring_create(UInt32(sampleCapacity), UInt32(stampCapacity))
        else { return nil }
        pointer = ring
        self.sampleCapacity = sampleCapacity
        self.stampCapacity = stampCapacity
    }

    deinit { ofl_ring_destroy(pointer) }

    /// 8 s of audio (design §9.2) and room for 8 s of 64-frame buffers.
    static func forCapture(sampleRate: Double) -> CaptureRing? {
        CaptureRing(sampleCapacity: Int((8 * sampleRate).rounded(.up)), stampCapacity: 8_192)
    }

    /// Producer side, as the sink block calls it (tests only).
    func write(_ samples: [Float], sampleTime: Double, hostTime: UInt64, flags: UInt32) {
        samples.withUnsafeBufferPointer { buffer in
            ofl_ring_write(pointer, buffer.baseAddress, UInt32(buffer.count), 1, sampleTime, hostTime, flags)
        }
    }

    func peek() -> OFLStampRecord? {
        var record = OFLStampRecord()
        return ofl_ring_peek_stamp(pointer, &record) ? record : nil
    }

    func copy(start: UInt64, count: Int, into target: UnsafeMutablePointer<Float>) {
        ofl_ring_copy(pointer, start, UInt32(count), target)
    }

    func pop() { ofl_ring_pop_stamp(pointer) }

    /// Frames lost because the sample ring or the stamp ring was full.
    var droppedFrames: UInt64 { ofl_ring_dropped_frames(pointer) + ofl_ring_lost_frames(pointer) }
}

/// What one capture is configured with; fixed from start to stop.
struct CaptureContext: Equatable {
    let captureId: String
    let sampleRate: Double
    /// `AVAudioSession.inputLatency` at start, µs.
    let inputLatencyMicroseconds: Int64
    /// Debug WAV snippets on (design §9.7).
    let debugCapture: Bool
}

/// The processing-queue half of the pipeline (design §9.2-§9.8): drains the capture ring, feeds
/// the analyzer, keeps the sample → host time fit, and turns each accepted onset into its
/// `audio_onset` payload. Single-threaded: only the processing queue calls it.
final class CaptureProcessor {
    struct Snippet {
        let seq: Int
        let samples: [Float]
        /// The onset's payload, written next to the WAV as its JSON sidecar.
        let payload: JSONValue
    }

    struct Output {
        var onsets: [JSONValue] = []
        var snippets: [Snippet] = []
    }

    let context: CaptureContext
    let ring: CaptureRing
    let analyzer: ImpactAnalyzer
    private(set) var fit: HostTimeFit
    private let clock: () -> ClockPair
    private let ticksToMicroseconds: (UInt64) -> Double
    private var scratch: [Float]
    /// Hardware sample time the next buffer should start at; a jump is a hardware gap.
    private var expectedSampleTime: Double?
    /// `produced_start + frames` of the previous record. Frames the ring lost to a full stamp
    /// ring already moved the next record's `produced_start` past this.
    private var expectedProducedStart: UInt64?
    /// Frames the hardware skipped (its sample clock jumped) on top of the ring's own drops.
    private var skippedFrames: Int64 = 0
    private var indexOffset: Int64 = 0
    /// Payloads waiting for their snippet (bounded: the analyzer emits snippets in order).
    private var awaitingSnippet: [Int: JSONValue] = [:]
    private(set) var lastSeq = 0
    private(set) var onsetCount = 0
    private(set) var processedFrames: Int64 = 0

    init(
        context: CaptureContext,
        ring: CaptureRing,
        config: OnsetConfig = .v1,
        clock: @escaping () -> ClockPair = { ClockPair.capture() },
        ticksToMicroseconds: @escaping (UInt64) -> Double = HostTicks.microseconds
    ) {
        self.context = context
        self.ring = ring
        analyzer = ImpactAnalyzer(sampleRate: context.sampleRate, config: config)
        analyzer.snippetsEnabled = context.debugCapture
        fit = HostTimeFit(nominalSampleRate: context.sampleRate)
        self.clock = clock
        self.ticksToMicroseconds = ticksToMicroseconds
        scratch = [Float](repeating: 0, count: 8_192)
    }

    /// Frames lost to a full ring or a hardware gap; each such gap reset the detector.
    var droppedFrames: Int64 { Int64(ring.droppedFrames) + skippedFrames }

    /// Onset payloads still waiting for their debug snippet (bounded by 8).
    var awaitingSnippetCount: Int { awaitingSnippet.count }

    /// Processes everything the IO thread has published so far.
    func drain() -> Output {
        var output = Output()
        while let record = ring.peek() {
            consume(record, into: &output)
            ring.pop()
        }
        return output
    }

    private func consume(_ record: OFLStampRecord, into output: inout Output) {
        let frames = Int(record.frames)
        if record.flags & UInt32(OFL_STAMP_SAMPLE_TIME_VALID) != 0 {
            if let expected = expectedSampleTime {
                let jump = record.sample_time - expected
                // The part of the jump the ring already counted (lost stamp records) is in
                // `produced_start`; only the rest is a hardware gap.
                let lost = expectedProducedStart.map { Double(record.produced_start &- $0) } ?? 0
                let skipped = (jump - lost).rounded()
                if jump >= 1 {
                    if skipped >= 1 {
                        skippedFrames += Int64(skipped)
                        indexOffset += Int64(skipped)
                    }
                } else if jump <= -1 {
                    fit.reset()  // the IO restarted its sample clock
                }
            }
            expectedSampleTime = record.sample_time + Double(frames)
        }
        expectedProducedStart = record.produced_start + UInt64(frames)
        let start = Int64(record.produced_start) + indexOffset
        fit.add(sample: start, hostMicroseconds: ticksToMicroseconds(record.host_time))
        guard record.flags & UInt32(OFL_STAMP_AUDIO_DROPPED) == 0 else { return }

        if scratch.count < frames { scratch = [Float](repeating: 0, count: frames) }
        let result: ImpactAnalyzer.Output = scratch.withUnsafeMutableBufferPointer { buffer in
            ring.copy(start: record.ring_start, count: frames, into: buffer.baseAddress!)
            return analyzer.process(UnsafeBufferPointer(rebasing: buffer[0..<frames]), startIndex: start)
        }
        processedFrames += Int64(frames)
        // Refit on the stamps up to this buffer before mapping: the periodic refit runs once a
        // second, so in the first second after a start the model would still be the one fitted
        // on the first 0.1 s, hundreds of µs off under real callback jitter (design §9.3).
        if !result.onsets.isEmpty { fit.refit() }
        for onset in result.onsets {
            let snippetFile = context.debugCapture ? CaptureStore.snippetPath(captureId: context.captureId, seq: onset.seq) : nil
            let payload = AudioPayloads.onset(onset, context: context, fit: fit, clock: clock(), snippetFile: snippetFile)
            output.onsets.append(payload)
            lastSeq = onset.seq
            onsetCount += 1
            if context.debugCapture { awaitingSnippet[onset.seq] = payload }
        }
        for snippet in result.snippets {
            guard let payload = awaitingSnippet.removeValue(forKey: snippet.seq) else { continue }
            output.snippets.append(Snippet(seq: snippet.seq, samples: snippet.samples, payload: payload))
        }
        // An onset whose snippet was lost to a gap never gets one; do not hold its payload forever.
        if awaitingSnippet.count > 8, let oldest = awaitingSnippet.keys.min() { awaitingSnippet.removeValue(forKey: oldest) }
    }
}
