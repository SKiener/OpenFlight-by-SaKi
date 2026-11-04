import Foundation

/// The Akaike Information Criterion change-point picker (Maeda 1985), the standard onset picker
/// in seismology and acoustic emission (design §9.5):
/// `AIC(k) = n₁·ln var(x[0…k]) + n₂·ln var(x[k+1…n−1])`, in O(n) from prefix sums.
enum AICPicker {
    struct Pick: Equatable {
        /// Continuous position (window coordinates) of the boundary where the new regime starts:
        /// `k + 0.5 + δ`, where `k` is the last sample of the old regime and `δ` the parabolic
        /// sub-sample correction.
        let position: Double
        /// The minimising `k`.
        let k: Int
        /// AIC of the no-change model minus the minimum: how much better a change point explains
        /// the window. Pure noise gives single digits.
        let depth: Double
        /// Half-width in samples of the contiguous set `{k : AIC(k) ≤ AIC_min + 1}` (1σ for a
        /// likelihood-based criterion), at least the parabola's own resolution.
        let halfWidth: Double
    }

    /// Picks the change point of `x[start..<end]`, with the split searched in `searchFrom…searchTo`
    /// (window-relative indices of the last old-regime sample). nil when the window is too short.
    static func pick(
        _ x: UnsafeBufferPointer<Double>, searchFrom: Int? = nil, searchTo: Int? = nil, margin: Int = 2
    ) -> Pick? {
        let n = x.count
        guard n >= 2 * margin + 3 else { return nil }
        var s1 = [Double](repeating: 0, count: n + 1)
        var s2 = [Double](repeating: 0, count: n + 1)
        for i in 0..<n {
            s1[i + 1] = s1[i] + x[i]
            s2[i + 1] = s2[i] + x[i] * x[i]
        }
        let total = s2[n] / Double(n) - (s1[n] / Double(n)) * (s1[n] / Double(n))
        let epsilon = max(total, 1e-30) * 1e-12 + 1e-36

        func variance(_ from: Int, _ to: Int) -> Double {  // [from, to)
            let count = Double(to - from)
            let mean = (s1[to] - s1[from]) / count
            return max((s2[to] - s2[from]) / count - mean * mean, epsilon)
        }

        let lo = max(margin - 1, searchFrom ?? (margin - 1))
        let hi = min(n - margin - 1, searchTo ?? (n - margin - 1))
        guard hi - lo >= 2 else { return nil }
        var aic = [Double](repeating: 0, count: hi - lo + 1)
        var best = 0
        for k in lo...hi {
            let before = Double(k + 1) * log(variance(0, k + 1))
            let after = Double(n - k - 1) * log(variance(k + 1, n))
            aic[k - lo] = before + after
            if aic[k - lo] < aic[best] { best = k - lo }
        }
        let minAIC = aic[best]
        var delta = 0.0
        if best > 0, best < aic.count - 1 {
            let left = aic[best - 1], right = aic[best + 1]
            let curvature = left - 2 * minAIC + right
            if curvature > 0 { delta = min(max(0.5 * (left - right) / curvature, -0.5), 0.5) }
        }
        var low = best, high = best
        while low > 0, aic[low - 1] <= minAIC + 1 { low -= 1 }
        while high < aic.count - 1, aic[high + 1] <= minAIC + 1 { high += 1 }
        let halfWidth = max(Double(high - low) / 2, 1 / 12.0.squareRoot())
        let none = Double(n) * log(variance(0, n))
        return Pick(position: Double(best + lo) + 0.5 + delta, k: best + lo, depth: none - minAIC, halfWidth: halfWidth)
    }

    static func pick(_ x: [Double], searchFrom: Int? = nil, searchTo: Int? = nil, margin: Int = 2) -> Pick? {
        x.withUnsafeBufferPointer { pick($0, searchFrom: searchFrom, searchTo: searchTo, margin: margin) }
    }
}

/// Mean-square energy helpers on windows, in dB re full scale (1.0).
enum Energy {
    static func meanSquare(_ x: [Double], _ from: Int, _ to: Int) -> Double? {
        let from = max(0, from), to = min(x.count, to)
        guard to > from else { return nil }
        var sum = 0.0
        for i in from..<to { sum += x[i] * x[i] }
        return sum / Double(to - from)
    }

    static func db(_ meanSquare: Double?) -> Double? {
        guard let meanSquare, meanSquare.isFinite else { return nil }
        return 10 * log10(meanSquare + 1e-20)
    }

    static func db(_ x: [Double], _ from: Int, _ to: Int) -> Double? {
        db(meanSquare(x, from, to))
    }
}

/// A window of the capture, copied out of the history for analysis.
struct AnalysisWindow {
    /// Absolute capture index of `samples[0]`.
    let start: Int64
    let samples: [Double]
    let sampleRate: Double

    func samples(ms: Double) -> Int { max(1, Int((ms * sampleRate / 1_000).rounded())) }
    /// Window-relative index of an absolute (fractional) capture position.
    func local(_ absolute: Double) -> Double { absolute - Double(start) }
    func local(_ absolute: Int64) -> Int { Int(absolute - start) }
}

/// The main (ball) onset, refined to a fraction of a sample (§9.5).
struct RefinedOnset: Equatable {
    /// Fractional absolute capture index of the onset.
    let position: Double
    /// 1σ in samples: the AIC interval, never below the quantisation limit `1/√12`.
    let uncertaintySamples: Double
    let aicDepth: Double
    /// 2-12 kHz energy in [t0, t0+1 ms] over [t0−2.25, t0−0.25 ms].
    let sharpnessDb: Double
    /// 10 %→90 % rise of the high-band envelope to its first peak, ms.
    let riseTimeMs: Double?
    /// High-band energy in [t0, t0+1 ms] over the loudest 1 ms in the 20 ms before (see
    /// `OnsetConfig.isolationMinDb`).
    let isolationDb: Double
    /// Loudest 1 ms high-band frame in [t0+1.5, t0+20 ms] minus [t0, t0+1 ms] (see
    /// `OnsetConfig.laterPeakMaxDb`); nil when the window does not reach that far.
    let laterPeakDb: Double?
    let passesGate: Bool
}

enum OnsetRefiner {
    /// Refines a coarse trigger on `window`, which must start at least `refineBeforeMs + warmupMs`
    /// before the trigger frame and reach `refineAfterMs + laterPeakWindowMs.to` past its end
    /// (a window that ends earlier fails the early-peak test).
    static func refine(_ trigger: OnsetTrigger, in window: AnalysisWindow, config: OnsetConfig) -> RefinedOnset? {
        let high = highBand(config, sampleRate: window.sampleRate).filtered(window.samples)
        let dull = BiquadChain.band(low: config.dullBandHz.low, high: config.dullBandHz.high, sampleRate: window.sampleRate)
            .filtered(window.samples)
        let from = window.local(trigger.frameStart) - window.samples(ms: config.refineBeforeMs)
        let to = window.local(trigger.frameEnd) + window.samples(ms: config.refineAfterMs)
        guard from >= 0, to <= high.count, to - from > 16 else { return nil }
        guard let pick = mainPick(high: high, dull: dull, from: from, to: to, sampleRate: window.sampleRate, config: config)
        else { return nil }
        let local = pick.position
        let t0 = Int(local.rounded(.up))

        let oneMs = window.samples(ms: 1)
        let post = Energy.meanSquare(high, t0, t0 + oneMs)
        let pre = Energy.meanSquare(high, t0 - window.samples(ms: 2.25), t0 - window.samples(ms: 0.25))
        let sharpness = (Energy.db(post) ?? -200) - (Energy.db(pre) ?? -200)
        let rise = riseTime(high, from: t0, sampleRate: window.sampleRate)
        let isolation = (Energy.db(post) ?? -200) - loudestFrameDb(
            high, from: t0 - window.samples(ms: config.isolationWindowMs), to: t0 - window.samples(ms: 0.5), frame: oneMs
        )
        let laterTo = t0 + window.samples(ms: config.laterPeakWindowMs.to)
        let laterPeak: Double? = laterTo <= high.count
            ? loudestFrameDb(high, from: t0 + window.samples(ms: config.laterPeakWindowMs.from), to: laterTo, frame: oneMs)
                - (Energy.db(post) ?? -200)
            : nil
        let passes = sharpness >= config.sharpnessMinDb
            && (rise.map { $0 <= config.maxRiseTimeMs } ?? false)
            && isolation >= config.isolationMinDb
            && (laterPeak.map { $0 <= config.laterPeakMaxDb } ?? false)
        return RefinedOnset(
            position: Double(window.start) + local,
            uncertaintySamples: uncertainty(high, onset: local, halfWidth: pick.halfWidth, from: from, sampleRate: window.sampleRate, multiple: config.uncertaintyNoiseMultiple),
            aicDepth: pick.depth,
            sharpnessDb: sharpness,
            riseTimeMs: rise,
            isolationDb: isolation,
            laterPeakDb: laterPeak,
            passesGate: passes
        )
    }

    /// The loudest `frame`-long stretch (hop of half a frame) of `x[from..<to]`, in dB; −200 when empty.
    static func loudestFrameDb(_ x: [Double], from: Int, to: Int, frame: Int) -> Double {
        let from = max(0, from), to = min(x.count, to)
        var loudest = -200.0
        var start = from
        let hop = max(1, frame / 2)
        while start + frame <= to {
            loudest = max(loudest, Energy.db(x, start, start + frame) ?? -200)
            start += hop
        }
        return loudest
    }

    /// The band the ball onset is picked on: `refineHighPassSections` Butterworth high-pass
    /// sections at `refineHighPassHz`. No low-pass: a 12 kHz low-pass delayed the pick by about a
    /// sample, and a causal high-pass leaves the onset where it is.
    static func highBand(_ config: OnsetConfig, sampleRate: Double) -> BiquadChain {
        BiquadChain(Array(
            repeating: .highPass(cutoff: config.refineHighPassHz, sampleRate: sampleRate),
            count: max(1, config.refineHighPassSections)
        ))
    }

    /// The ball onset in `high[from..<to]` (window-relative position).
    ///
    /// The AIC assumes two stationary segments, so the window ends just after the band signal's
    /// peak (the ball crack, the loudest thing in the window), not in the decay. On a fat strike
    /// a single split can land on the mat instead, the first variance step. The ball is the last
    /// big step up, so a later split replaces the current one when the stretch in between lasts
    /// at least `minSplitMs`, is `laterStepDb` quieter in the high band than what follows, and
    /// is dull: more energy in `dullBandHz` (0.2-1 kHz, below the ball's modes) than in the high
    /// band by `precursorDullDb`. The last test keeps a ball whose own first fraction of a
    /// millisecond is weak from being cut in two.
    ///
    /// Known limit (synthetic tests, design §12.1): a precursor that leads the ball by less than
    /// about 1 ms is often too short for the dullness test, and the pick then stays on the mat.
    /// Spectral alternatives (high-band vs full-band step, RMS frequency of the stretch) did worse
    /// on short stretches, because filter transients and the thud's leakage above 1.5 kHz look
    /// like the ball's own first samples.
    static func mainPick(
        high: [Double], dull: [Double], from: Int, to: Int, sampleRate: Double, config: OnsetConfig
    ) -> AICPicker.Pick? {
        func pick(_ lo: Int, _ hi: Int) -> AICPicker.Pick? {
            high.withUnsafeBufferPointer { buffer in AICPicker.pick(UnsafeBufferPointer(rebasing: buffer[lo..<hi])) }
                .map { AICPicker.Pick(position: $0.position + Double(lo), k: $0.k + lo, depth: $0.depth, halfWidth: $0.halfWidth) }
        }
        let halfMs = max(4, Int((0.5 * sampleRate / 1_000).rounded()))
        var peak = from
        var peakValue = 0.0
        for i in from..<to where abs(high[i]) > peakValue {
            peakValue = abs(high[i])
            peak = i
        }
        let end = min(to, peak + halfMs)
        guard var current = pick(from, end) else { return nil }
        let minLead = max(2, Int((config.minSplitMs * sampleRate / 1_000).rounded()))
        for _ in 0..<3 {
            let start = current.k + 1
            guard end - start >= 2 * minLead + 4, let later = pick(start, end) else { break }
            let boundary = later.k + 1
            guard boundary - start >= minLead else { break }
            let between = Energy.db(high, start, boundary) ?? 0
            let after = Energy.db(high, boundary, min(end, boundary + halfMs)) ?? -200
            let dullness = (Energy.db(dull, start, boundary) ?? -200) - between
            guard after - between >= config.laterStepDb, dullness >= config.precursorDullDb else { break }
            current = AICPicker.Pick(position: later.position, k: later.k, depth: current.depth, halfWidth: later.halfWidth)
        }
        return current
    }

    /// 1σ onset uncertainty in samples. The AIC interval alone is far too narrow (the criterion
    /// assumes a sudden step, while a real attack ramps up), so it is widened by the time the
    /// band signal takes after the pick to clear three times the pre-onset noise RMS.
    static func uncertainty(_ x: [Double], onset: Double, halfWidth: Double, from: Int, sampleRate: Double, multiple: Double) -> Double {
        let t0 = Int(onset.rounded(.up))
        let guardSamples = max(2, Int((0.25 * sampleRate / 1_000).rounded()))
        let noise = (Energy.meanSquare(x, from, t0 - guardSamples) ?? 0).squareRoot()
        var emerge = 0.0
        if noise > 0 {
            var i = max(from, t0 - guardSamples)
            let limit = min(x.count, t0 + Int((1 * sampleRate / 1_000).rounded()))
            while i < limit, abs(x[i]) < multiple * noise { i += 1 }
            emerge = abs(Double(i) - onset)
        }
        return max((halfWidth * halfWidth + emerge * emerge).squareRoot(), 1 / 12.0.squareRoot())
    }

    /// 10 %→90 % rise time (ms) of a short RMS envelope to its first peak in [t0, t0+5 ms].
    /// The first peak is the first local maximum reaching half the window's maximum.
    static func riseTime(_ x: [Double], from t0: Int, sampleRate: Double) -> Double? {
        let span = max(4, Int((0.1 * sampleRate / 1_000).rounded()))  // 0.1 ms envelope
        let end = min(x.count, t0 + Int((5 * sampleRate / 1_000).rounded()))
        let start = max(0, t0 - span)
        guard end - start > 2 * span else { return nil }
        var envelope = [Double](repeating: 0, count: end - start)
        var sum = 0.0
        for i in start..<end {
            sum += x[i] * x[i]
            if i - span >= start { sum -= x[i - span] * x[i - span] }
            envelope[i - start] = (max(sum, 0) / Double(span)).squareRoot()
        }
        guard let maximum = envelope.max(), maximum > 0 else { return nil }
        var peakIndex = envelope.count - 1
        for i in 1..<(envelope.count - 1) where envelope[i] >= 0.5 * maximum {
            if envelope[i] >= envelope[i - 1], envelope[i] >= envelope[i + 1] {
                peakIndex = i
                break
            }
        }
        let peak = envelope[peakIndex]
        func crossing(_ level: Double) -> Double? {
            // Last crossing of `level` before the peak, linearly interpolated.
            var i = peakIndex
            while i > 0, envelope[i - 1] >= level { i -= 1 }
            guard i > 0 else { return Double(i) }
            let a = envelope[i - 1], b = envelope[i]
            return Double(i - 1) + (b > a ? (level - a) / (b - a) : 0)
        }
        guard let low = crossing(0.1 * peak), let high = crossing(0.9 * peak) else { return nil }
        return max(0, high - low) * 1_000 / sampleRate
    }
}
