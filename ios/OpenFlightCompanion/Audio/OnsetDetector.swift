import Foundation

/// Every threshold of the onset pipeline in one place (design §9.4-§9.6). They are initial values
/// chosen from the physics in design F19, not fitted to recordings: the debug WAVs exist to tune
/// them. `AudioStatus.thresholds` reports them and `detector_version` names the set.
struct OnsetConfig: Equatable {
    static let detectorVersion = "onset-v1"

    // Coarse detector (§9.4).
    var highPassHz = 1_000.0
    var frameMs = 1.0
    var floorPercentile = 0.20
    var floorWindowMs = 500.0
    /// No trigger until this much floor history exists after a start or a gap.
    var floorWarmupMs = 100.0
    var triggerOverFloorDb = 18.0
    var triggerMinDbfs = -50.0
    var riseDb = 12.0
    var riseFrames = 3
    var refractoryMs = 150.0
    /// Neighbour shots: a frame this much louder than the open event's loudest frame starts a
    /// new event even inside the refractory period, so a quieter impact (a neighbour bay, a
    /// practice tap) cannot hide the next real one. Only after `overrideHoldoffMs`, so the
    /// event's own first frames never re-trigger it. Design §11 puts neighbours 4.6-7.7 dB
    /// below our own ball, so the margin must sit well under 4.6 dB; the event's own later
    /// frames only decay below its peak, and reflections are quieter than the direct sound.
    var refractoryOverrideDb = 3.0
    var overrideHoldoffMs = 10.0
    /// Two accepted onsets closer than this are the same impact (an override trigger and the
    /// event before it can both refine to the same ball crack); the later one is dropped.
    var duplicateWithinMs = 5.0

    // Refinement (§9.5): AIC window around the trigger frame, on the high-passed signal.
    var refineHighPassHz = 2_000.0
    var refineHighPassSections = 2
    var refineBeforeMs = 6.0
    /// Reaches past the trigger frame far enough to catch the ball when the mat fired the trigger.
    var refineAfterMs = 6.0
    /// A later AIC split replaces the first while it lifts the band energy by this much (§9.5).
    var laterStepDb = 10.0
    /// …and only when the stretch before it is dull: its energy in `dullBandHz` minus its energy
    /// in the high band is at least this. A synthetic mat thud scores −7 to +8 dB, white noise
    /// about −13 dB, the weak first samples of a synthetic ball crack mostly −17 to −29 dB but as
    /// high as −3 dB when a mode sits near 2 kHz. −1 dB moved one clean impact in 800 by more
    /// than 0.1 ms (−3 dB: two, one by 0.27 ms) and still keeps the onset on the ball for about
    /// 75 % of 1 ms precursors and 90 % of 3-5 ms ones.
    var precursorDullDb = -1.0
    var dullBandHz = (low: 200.0, high: 1_000.0)
    /// Shortest stretch between two splits that is tested at all.
    var minSplitMs = 0.08
    /// The onset uncertainty is widened by the time the high band takes after the pick to reach
    /// this multiple of the pre-onset noise RMS (see `OnsetRefiner.uncertainty`).
    var uncertaintyNoiseMultiple = 6.0
    /// Filter settling time in front of every analysis window.
    var warmupMs = 10.0

    // Sharpness gate: the confirmation that rejects talking and swells that pass the coarse rise
    // test. High-band energy in [t0, t0+1 ms] over [t0-2.25, t0-0.25 ms], and the 10-90 % rise.
    var sharpnessMinDb = 10.0
    var maxRiseTimeMs = 1.0
    /// Isolation: high-band energy in [t0, t0+1 ms] over the loudest 1 ms frame in
    /// [t0−20 ms, t0−0.5 ms]. Voiced speech is a train of glottal pulses 3-12 ms apart, each as
    /// sharp as an impact; an impact stands well above everything in the 20 ms before it. A mat
    /// precursor is low-band and barely counts here.
    var isolationMinDb = 10.0
    var isolationWindowMs = 20.0
    /// Early peak: no 1 ms high-band frame (hop 0.5 ms) in [t0+1.5, t0+20 ms] may be louder than
    /// [t0, t0+1 ms] by more than this. An impact is loudest right after its onset and then
    /// decays (modes of 1-4 ms; beating between them lifted a later frame by up to 4.3 dB in
    /// 400 synthetic impacts); talking and practice swings keep rising for tens of ms after they
    /// first clear the floor (6.6 dB and more for a 10 ms attack), so they fail here even when
    /// their first rise is steep.
    var laterPeakMaxDb = 5.5
    var laterPeakWindowMs = (from: 1.5, to: 20.0)

    // Features (§9.6).
    var featureWindowMs = 20.0
    var clipLevel: Float = 0.999

    // Fat cue (§9.6): a low-band (0.2-2 kHz) precursor in [t0-6 ms, t0-0.15 ms].
    var fatBandHz = (low: 200.0, high: 2_000.0)
    var fatSearchMs = 6.0
    var fatMinLeadMs = 0.15
    var fatFloorWindowMs = (from: 30.0, to: 8.0)
    var fatMinOverFloorDb = 10.0
    var fatMinAICDepth = 20.0

    // Second transient within 20 ms of t0 (re-rise of the 1 kHz high-passed envelope).
    var secondMinDelayMs = 1.0
    var secondMaxDelayMs = 20.0
    var secondSubframeMs = 0.25
    /// Over the mean of the previous 1 ms of sub-frames.
    var secondRiseDb = 9.0
    var secondOverFloorDb = 12.0
    /// Ignored when quieter than the main peak by more than this.
    var secondMaxBelowMainDb = 30.0

    // Debug snippets (brief: ±200 ms around each onset).
    var snippetBeforeMs = 200.0
    var snippetAfterMs = 200.0

    static let v1 = OnsetConfig()

    static func == (lhs: OnsetConfig, rhs: OnsetConfig) -> Bool {
        lhs.thresholds == rhs.thresholds
    }

    /// The `thresholds` object of `audio_status` and `capture.json`.
    var thresholds: JSONValue {
        .object([
            "high_pass_hz": .number(highPassHz),
            "frame_ms": .number(frameMs),
            "floor_percentile": .number(floorPercentile),
            "floor_window_ms": .number(floorWindowMs),
            "trigger_over_floor_db": .number(triggerOverFloorDb),
            "trigger_min_dbfs": .number(triggerMinDbfs),
            "rise_db": .number(riseDb),
            "rise_frames": .integer(riseFrames),
            "refractory_ms": .number(refractoryMs),
            "refractory_override_db": .number(refractoryOverrideDb),
            "refine_high_pass_hz": .number(refineHighPassHz),
            "refine_high_pass_order": .integer(2 * refineHighPassSections),
            "sharpness_min_db": .number(sharpnessMinDb),
            "max_rise_time_ms": .number(maxRiseTimeMs),
            "isolation_min_db": .number(isolationMinDb),
            "later_peak_max_db": .number(laterPeakMaxDb),
            "fat_band_hz": .array([.number(fatBandHz.low), .number(fatBandHz.high)]),
            "fat_min_lead_ms": .number(fatMinLeadMs),
            "fat_min_over_floor_db": .number(fatMinOverFloorDb),
            "second_rise_db": .number(secondRiseDb),
            "snippet_ms": .array([.number(-snippetBeforeMs), .number(snippetAfterMs)]),
        ])
    }

    func samples(ms: Double, sampleRate: Double) -> Int {
        max(1, Int((ms * sampleRate / 1_000).rounded()))
    }
}

/// A coarse trigger: the 1 ms frame whose energy jumped (§9.4).
struct OnsetTrigger: Equatable {
    /// First sample of the trigger frame (absolute capture index).
    let frameStart: Int64
    /// One past the last sample of the trigger frame.
    let frameEnd: Int64
    let energyDbfs: Double
    /// The adaptive noise floor when it fired (frozen from here until the event closes).
    let floorDbfs: Double
    /// Frame energy minus the lowest of the previous `riseFrames` frames.
    let riseDb: Double
}

/// The 20th percentile of recent frame energies, from a 0.25 dB histogram so an update costs a
/// bin scan instead of a sort.
struct NoiseFloorTracker {
    private static let minDb = -160.0
    private static let binDb = 0.25
    private static let binCount = 720  // -160 … +20 dB

    private let window: Int
    private let percentile: Double
    private var counts: [Int32]
    private var ring: [Int16]
    private var head = 0
    private(set) var count = 0
    private var cached: Double?
    private var framesSinceUpdate = 0

    init(windowFrames: Int, percentile: Double) {
        window = max(1, windowFrames)
        self.percentile = percentile
        counts = Array(repeating: 0, count: Self.binCount)
        ring = Array(repeating: 0, count: window)
    }

    private static func bin(_ db: Double) -> Int16 {
        let index = ((db - minDb) / binDb).rounded(.down)
        guard index.isFinite else { return 0 }
        return Int16(min(max(index, 0), Double(binCount - 1)))
    }

    mutating func add(_ db: Double) {
        let bin = Self.bin(db)
        if count == window {
            counts[Int(ring[head])] -= 1
        } else {
            count += 1
        }
        ring[head] = bin
        counts[Int(bin)] += 1
        head = (head + 1) % window
        framesSinceUpdate += 1
        // Recompute every 10 frames (10 ms), and on every frame while warming up.
        if cached == nil || framesSinceUpdate >= 10 || count < window {
            cached = computePercentile()
            framesSinceUpdate = 0
        }
    }

    /// nil until a frame was added.
    var floorDb: Double? { cached }

    private func computePercentile() -> Double? {
        guard count > 0 else { return nil }
        let target = max(1, Int((percentile * Double(count)).rounded(.up)))
        var cumulative = 0
        for bin in 0..<Self.binCount {
            cumulative += Int(counts[bin])
            if cumulative >= target {
                return Self.minDb + (Double(bin) + 0.5) * Self.binDb
            }
        }
        return nil
    }

    mutating func reset() {
        counts = Array(repeating: 0, count: Self.binCount)
        head = 0
        count = 0
        cached = nil
        framesSinceUpdate = 0
    }
}

/// The streaming coarse detector (§9.4): 1 kHz high-pass, 1 ms frame energies, an adaptive floor,
/// and a trigger on a fast rise well above it. Pure value type; it sees one sample at a time, so
/// its output does not depend on how the stream was cut into buffers.
struct OnsetDetector {
    let config: OnsetConfig
    let sampleRate: Double
    let frameLength: Int
    private let refractorySamples: Int64
    private let warmupFrames: Int

    private var highPass: Biquad
    private var frameSum = 0.0
    private var frameFill = 0
    private var frameStart: Int64 = 0
    private var recent: [Double]
    private var recentHead = 0
    private var recentCount = 0
    private var floor: NoiseFloorTracker
    private var refractoryUntil = Int64.min
    private let holdoffSamples: Int64
    /// The open event: where its trigger frame started and its loudest frame so far.
    private var eventStart = Int64.min
    private var eventPeakDb = -Double.infinity
    private var eventFloorDb = 0.0

    init(sampleRate: Double, config: OnsetConfig = .v1) {
        self.config = config
        self.sampleRate = sampleRate
        frameLength = config.samples(ms: config.frameMs, sampleRate: sampleRate)
        refractorySamples = Int64(config.samples(ms: config.refractoryMs, sampleRate: sampleRate))
        holdoffSamples = Int64(config.samples(ms: config.overrideHoldoffMs, sampleRate: sampleRate))
        let frameMsActual = Double(frameLength) * 1_000 / sampleRate
        warmupFrames = Int((config.floorWarmupMs / frameMsActual).rounded(.up))
        highPass = .highPass(cutoff: config.highPassHz, sampleRate: sampleRate)
        recent = Array(repeating: 0, count: max(1, config.riseFrames))
        floor = NoiseFloorTracker(
            windowFrames: Int((config.floorWindowMs / frameMsActual).rounded()),
            percentile: config.floorPercentile
        )
    }

    /// The current noise floor in dBFS (frame mean square, re 1.0), nil while warming up.
    var floorDbfs: Double? { floor.floorDb }

    /// Starts over at `index` (capture start, or the first sample after a gap).
    mutating func reset(at index: Int64) {
        highPass.reset()
        frameSum = 0
        frameFill = 0
        frameStart = index
        recentHead = 0
        recentCount = 0
        floor.reset()
        refractoryUntil = Int64.min
        eventStart = Int64.min
        eventPeakDb = -.infinity
    }

    /// Re-arms early: a trigger the sharpness gate rejected must not hide the next impact.
    mutating func endRefractory(at index: Int64) {
        refractoryUntil = min(refractoryUntil, index)
    }

    /// Feeds one sample (absolute index `index`); returns a trigger when a frame completes and fires.
    @inline(__always)
    mutating func process(_ x: Double, index: Int64) -> OnsetTrigger? {
        if frameFill == 0 { frameStart = index }
        let y = highPass.process(x)
        frameSum += y * y
        frameFill += 1
        guard frameFill == frameLength else { return nil }
        let energy = 10 * log10(frameSum / Double(frameLength) + 1e-20)
        frameSum = 0
        frameFill = 0
        return closeFrame(energy: energy, start: frameStart, end: index + 1)
    }

    private mutating func closeFrame(energy: Double, start: Int64, end: Int64) -> OnsetTrigger? {
        defer {
            recent[recentHead] = energy
            recentHead = (recentHead + 1) % recent.count
            recentCount = min(recentCount + 1, recent.count)
        }
        let previousMin = recent.count == recentCount ? (recent.min() ?? energy) : energy
        let rise = energy - previousMin
        if start < refractoryUntil {
            // Floor frozen while an event is open. Only a much louder impact may start a new one.
            defer { eventPeakDb = max(eventPeakDb, energy) }
            guard start - eventStart >= holdoffSamples,
                  energy - eventPeakDb >= config.refractoryOverrideDb,
                  energy >= config.triggerMinDbfs,
                  rise >= config.riseDb
            else { return nil }
            return open(start: start, end: end, energy: energy, floorDb: eventFloorDb, rise: rise)
        }

        if floor.count >= warmupFrames, recentCount == recent.count, let floorDb = floor.floorDb {
            if energy - floorDb >= config.triggerOverFloorDb,
               energy >= config.triggerMinDbfs,
               rise >= config.riseDb {
                return open(start: start, end: end, energy: energy, floorDb: floorDb, rise: rise)
            }
        }
        floor.add(energy)
        return nil
    }

    private mutating func open(start: Int64, end: Int64, energy: Double, floorDb: Double, rise: Double) -> OnsetTrigger {
        refractoryUntil = start + refractorySamples
        eventStart = start
        eventPeakDb = energy
        eventFloorDb = floorDb
        return OnsetTrigger(frameStart: start, frameEnd: end, energyDbfs: energy, floorDbfs: floorDb, riseDb: rise)
    }
}
