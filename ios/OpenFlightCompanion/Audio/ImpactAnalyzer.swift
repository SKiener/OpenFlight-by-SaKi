import Foundation

/// One accepted impact, before it is put on the host clock.
struct DetectedOnset: Equatable {
    /// 1, 2, … within the capture.
    let seq: Int
    let trigger: OnsetTrigger
    let refined: RefinedOnset
    let features: ImpactFeatures
}

/// The debug snippet around an onset (§9.7): raw samples from `snippetBeforeMs` before to
/// `snippetAfterMs` after the onset.
struct OnsetSnippet: Equatable {
    let seq: Int
    /// Absolute capture index of `samples[0]`.
    let startIndex: Int64
    let samples: [Float]
}

/// The streaming onset pipeline (design §9.4-§9.7), pure and single-threaded: coarse detector,
/// sample history, refinement, sharpness gate, features and snippets. It works sample by sample,
/// so its output does not depend on how the stream was cut into buffers, and it knows nothing of
/// clocks: `CaptureProcessor` maps its sample positions to host time.
final class ImpactAnalyzer {
    struct Output {
        var onsets: [DetectedOnset] = []
        var snippets: [OnsetSnippet] = []
    }

    private struct Pending {
        let trigger: OnsetTrigger
        /// Set once the onset was refined and emitted; the event then waits for its snippet.
        var onset: DetectedOnset?
    }

    let sampleRate: Double
    let config: OnsetConfig
    /// Whether accepted onsets wait for and return a snippet (debug capture on).
    var snippetsEnabled = false

    private var detector: OnsetDetector
    private var history: SampleHistory
    private var pending: [Pending] = []
    private var nextSeq = 1
    private let windowBefore: Int64
    private let analysisAfter: Int64
    private let snippetBefore: Int64
    private let snippetAfter: Int64

    /// Coarse triggers the sharpness gate (or a too-short history) turned down.
    private(set) var rejectedTriggers = 0
    /// Triggers that refined to an onset already emitted (see `OnsetConfig.duplicateWithinMs`).
    private(set) var duplicateTriggers = 0
    private var lastOnsetPosition = -Double.infinity
    private let duplicateWithin: Double
    /// Events lost to a gap in the stream before they could be analysed.
    private(set) var abandonedEvents = 0
    private(set) var onsetCount = 0

    init(sampleRate: Double, config: OnsetConfig = .v1) {
        self.sampleRate = sampleRate
        self.config = config
        detector = OnsetDetector(sampleRate: sampleRate, config: config)
        func n(_ ms: Double) -> Int64 { Int64(config.samples(ms: ms, sampleRate: sampleRate)) }
        // Refinement and features need 6 + 10 ms (AIC + filter warm-up) and the 30 ms fat floor
        // plus warm-up before the onset, and the ball can be `refineAfterMs` after the trigger.
        windowBefore = n(config.fatFloorWindowMs.from + config.warmupMs + config.refineBeforeMs)
        analysisAfter = n(config.refineAfterMs + config.featureWindowMs + 1)
        snippetBefore = n(config.snippetBeforeMs)
        snippetAfter = n(config.snippetAfterMs)
        duplicateWithin = config.duplicateWithinMs * sampleRate / 1_000
        let keep = max(n(1_000), snippetBefore + snippetAfter + windowBefore + analysisAfter + n(250))
        history = SampleHistory(capacity: Int(keep))
    }

    /// Events still waiting for samples (bounded: at most one per refractory period in flight).
    var pendingCount: Int { pending.count }
    var historyCapacity: Int { history.capacity }
    var floorDbfs: Double? { detector.floorDbfs }

    /// Starts over at `index`: the capture start, or the first sample after a gap. Pending events
    /// that would need samples from across the gap are abandoned.
    func reset(at index: Int64) {
        abandonedEvents += pending.filter { $0.onset == nil }.count
        pending.removeAll()
        history.reset(at: index)
        detector.reset(at: index)
        lastOnsetPosition = -.infinity
    }

    /// Feeds samples that start at absolute index `startIndex`, which must follow the previous
    /// call without a gap (call `reset(at:)` first otherwise). Non-finite samples count as 0.
    func process(_ samples: UnsafeBufferPointer<Float>, startIndex: Int64) -> Output {
        if startIndex != history.endIndex { reset(at: startIndex) }
        var output = Output()
        var index = startIndex
        for raw in samples {
            let value: Float = raw.isFinite ? raw : 0
            history.append(value)
            if let trigger = detector.process(Double(value), index: index) {
                pending.append(Pending(trigger: trigger))
            }
            index += 1
            if !pending.isEmpty { advancePending(at: index, into: &output) }
        }
        return output
    }

    func process(_ samples: [Float], startIndex: Int64) -> Output {
        samples.withUnsafeBufferPointer { process($0, startIndex: startIndex) }
    }

    /// Runs every pending event whose samples are now all in the history. `available` is one past
    /// the newest sample.
    private func advancePending(at available: Int64, into output: inout Output) {
        var index = 0
        while index < pending.count {
            var event = pending[index]
            if event.onset == nil {
                guard available >= event.trigger.frameEnd + analysisAfter else { index += 1; continue }
                let analysis = analyze(event.trigger)
                guard case .accepted(let onset) = analysis else {
                    if case .duplicate = analysis {
                        duplicateTriggers += 1
                    } else {
                        rejectedTriggers += 1
                        detector.endRefractory(at: available)
                    }
                    pending.remove(at: index)
                    continue
                }
                output.onsets.append(onset)
                onsetCount += 1
                guard snippetsEnabled else { pending.remove(at: index); continue }
                event.onset = onset
                pending[index] = event
            }
            if let onset = event.onset {
                let center = Int64(onset.refined.position.rounded(.down))
                guard available >= center + snippetAfter else { index += 1; continue }
                if let samples = history.copy(from: center - snippetBefore, to: center + snippetAfter) {
                    output.snippets.append(OnsetSnippet(seq: onset.seq, startIndex: center - snippetBefore, samples: samples))
                }
                pending.remove(at: index)
                continue
            }
            index += 1
        }
    }

    private enum Analysis {
        case accepted(DetectedOnset)
        case rejected
        case duplicate
    }

    private func analyze(_ trigger: OnsetTrigger) -> Analysis {
        let from = max(history.startIndex, trigger.frameStart - windowBefore)
        let to = trigger.frameEnd + analysisAfter
        guard let raw = history.copy(from: from, to: to) else { return .rejected }
        let window = AnalysisWindow(start: from, samples: raw.map(Double.init), sampleRate: sampleRate)
        guard let refined = OnsetRefiner.refine(trigger, in: window, config: config), refined.passesGate else { return .rejected }
        guard abs(refined.position - lastOnsetPosition) >= duplicateWithin else { return .duplicate }
        lastOnsetPosition = refined.position
        let features = ImpactFeatures.compute(
            window: window, rawFloat: raw, onset: refined.position, refined: refined, trigger: trigger, config: config
        )
        defer { nextSeq += 1 }
        return .accepted(DetectedOnset(seq: nextSeq, trigger: trigger, refined: refined, features: features))
    }
}
