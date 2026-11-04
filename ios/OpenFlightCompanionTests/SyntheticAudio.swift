import Foundation
@testable import OpenFlightCompanion

/// Deterministic synthetic signals for the onset tests (design §12.1). Seeded, so a failing seed
/// reproduces exactly.
struct SplitMix64 {
    private var state: UInt64

    init(seed: UInt64) { state = seed &+ 0x9E37_79B9_7F4A_7C15 }

    mutating func next() -> UInt64 {
        state &+= 0x9E37_79B9_7F4A_7C15
        var z = state
        z = (z ^ (z >> 30)) &* 0xBF58_476D_1CE4_E5B9
        z = (z ^ (z >> 27)) &* 0x94D0_49BB_1331_11EB
        return z ^ (z >> 31)
    }

    /// Uniform in [0, 1).
    mutating func uniform() -> Double { Double(next() >> 11) / Double(1 << 53) }

    mutating func uniform(_ range: ClosedRange<Double>) -> Double {
        range.lowerBound + (range.upperBound - range.lowerBound) * uniform()
    }

    mutating func gaussian() -> Double {
        let u1 = max(uniform(), 1e-300), u2 = uniform()
        return (-2 * log(u1)).squareRoot() * cos(2 * .pi * u2)
    }
}

enum SyntheticAudio {
    static func rms(_ x: [Double], _ from: Int = 0, _ to: Int? = nil) -> Double {
        let to = min(to ?? x.count, x.count), from = max(0, from)
        guard to > from else { return 0 }
        var sum = 0.0
        for i in from..<to { sum += x[i] * x[i] }
        return (sum / Double(to - from)).squareRoot()
    }

    static func dbToAmplitude(_ db: Double) -> Double { pow(10, db / 20) }

    /// White plus pink (1/f, Kellet filter) noise in equal power, scaled to `levelDbfs` RMS.
    static func noise(count: Int, levelDbfs: Double = -60, rng: inout SplitMix64) -> [Double] {
        var b0 = 0.0, b1 = 0.0, b2 = 0.0, b3 = 0.0, b4 = 0.0, b5 = 0.0, b6 = 0.0
        var white = [Double](repeating: 0, count: count)
        var pink = [Double](repeating: 0, count: count)
        for i in 0..<count {
            let w = rng.gaussian()
            white[i] = w
            b0 = 0.99886 * b0 + w * 0.0555179
            b1 = 0.99332 * b1 + w * 0.0750759
            b2 = 0.96900 * b2 + w * 0.1538520
            b3 = 0.86650 * b3 + w * 0.3104856
            b4 = 0.55000 * b4 + w * 0.5329522
            b5 = -0.7616 * b5 - w * 0.0168980
            pink[i] = b0 + b1 + b2 + b3 + b4 + b5 + b6 + w * 0.5362
            b6 = w * 0.115926
        }
        let pinkScale = 1 / max(rms(pink), 1e-12)
        var out = [Double](repeating: 0, count: count)
        for i in 0..<count { out[i] = (white[i] + pink[i] * pinkScale) / 2.0.squareRoot() }
        let scale = dbToAmplitude(levelDbfs) / max(rms(out), 1e-12)
        for i in 0..<count { out[i] *= scale }
        return out
    }

    /// The ball crack: 5 damped sinusoids at 2-9 kHz (decays 1-4 ms) behind a 0.1 ms
    /// raised-cosine attack, starting at fractional sample `t0`. Unit peak-free scale: the
    /// caller sets the level.
    static func impactShape(count: Int, t0: Double, sampleRate: Double, rng: inout SplitMix64) -> [Double] {
        var modes: [(f: Double, d: Double, a: Double, p: Double)] = []
        for _ in 0..<5 {
            modes.append((rng.uniform(2_000...9_000), rng.uniform(0.001...0.004), rng.uniform(0.4...1.0), rng.uniform(0...(2 * .pi))))
        }
        let attack = 0.0001
        var out = [Double](repeating: 0, count: count)
        let first = max(0, Int(t0.rounded(.up)))
        for i in first..<count {
            let tau = (Double(i) - t0) / sampleRate
            guard tau >= 0 else { continue }
            let window = tau < attack ? 0.5 * (1 - cos(.pi * tau / attack)) : 1
            var s = 0.0
            for mode in modes { s += mode.a * exp(-tau / mode.d) * sin(2 * .pi * mode.f * tau + mode.p) }
            out[i] = window * s
            if tau > 0.05 { break }
        }
        return out
    }

    /// Damped low-band noise (0.2-1.5 kHz, decay `decayS`) starting at fractional sample `start`:
    /// the mat or turf contact.
    static func thud(count: Int, start: Double, sampleRate: Double, decayS: Double = 0.004, rng: inout SplitMix64) -> [Double] {
        var hp = Biquad.highPass(cutoff: 200, sampleRate: sampleRate)
        var lp = Biquad.lowPass(cutoff: 1_500, sampleRate: sampleRate)
        var out = [Double](repeating: 0, count: count)
        let first = max(0, Int(start.rounded(.up)))
        for i in first..<count {
            let tau = (Double(i) - start) / sampleRate
            if tau > 12 * decayS { break }
            let x = rng.gaussian() * exp(-tau / decayS)
            out[i] = lp.process(hp.process(x))
        }
        return out
    }

    /// Exponentially decaying broadband noise between `from` and `to` (reverb tail).
    static func tail(count: Int, from: Int, to: Int, decayS: Double, sampleRate: Double, rng: inout SplitMix64) -> [Double] {
        var out = [Double](repeating: 0, count: count)
        for i in max(0, from)..<min(count, to) {
            let tau = Double(i - from) / sampleRate
            out[i] = rng.gaussian() * exp(-tau / decayS)
        }
        return out
    }

    /// Speech-like: 150 Hz harmonics weighted by three formants, `attackS` raised-cosine attack,
    /// a hold and a release; F0 jitters a little.
    static func speech(count: Int, start: Int, sampleRate: Double, attackS: Double = 0.03, holdS: Double = 0.2, rng: inout SplitMix64) -> [Double] {
        let formants: [(f: Double, bw: Double)] = [(700, 130), (1_220, 70), (2_600, 160)]
        var out = [Double](repeating: 0, count: count)
        let f0 = rng.uniform(120...180)
        let total = attackS + holdS + attackS
        var phase = 0.0
        for i in max(0, start)..<count {
            let tau = Double(i - start) / sampleRate
            if tau > total { break }
            let envelope: Double
            if tau < attackS { envelope = 0.5 * (1 - cos(.pi * tau / attackS)) }
            else if tau < attackS + holdS { envelope = 1 }
            else { envelope = 0.5 * (1 + cos(.pi * (tau - attackS - holdS) / attackS)) }
            let f = f0 * (1 + 0.02 * sin(2 * .pi * 5 * tau))
            phase += 2 * .pi * f / sampleRate
            var s = 0.0
            var harmonic = 1.0
            while harmonic * f0 < 5_000 {
                let fh = harmonic * f0
                var gain = 0.0
                for formant in formants {
                    let d = (fh - formant.f) / formant.bw
                    gain += 1 / (1 + d * d)
                }
                s += gain / harmonic.squareRoot() * sin(harmonic * phase)
                harmonic += 1
            }
            out[i] = envelope * s
        }
        return out
    }

    /// A slow swell: band noise whose level rises by `riseDb` over `durationS`.
    static func swell(count: Int, start: Int, durationS: Double, riseDb: Double, sampleRate: Double, rng: inout SplitMix64) -> [Double] {
        var out = [Double](repeating: 0, count: count)
        for i in max(0, start)..<count {
            let tau = Double(i - start) / sampleRate
            if tau > durationS { break }
            out[i] = rng.gaussian() * dbToAmplitude(-riseDb * (1 - tau / durationS))
        }
        return out
    }

    /// A hand clap: a 1 ms-attack noise burst decaying over 8 ms.
    static func clap(count: Int, start: Int, sampleRate: Double, rng: inout SplitMix64) -> [Double] {
        var out = [Double](repeating: 0, count: count)
        for i in max(0, start)..<count {
            let tau = Double(i - start) / sampleRate
            if tau > 0.08 { break }
            let attack = min(1, tau / 0.001)
            out[i] = rng.gaussian() * attack * exp(-tau / 0.008)
        }
        return out
    }

    /// A voiceless stop's release ("t", "k"): > 2 kHz noise with a 0.3 ms attack, a 3 ms decay
    /// and 20 ms of weaker aspiration (35 ms in all). Unscaled: the caller sets the level.
    static func plosive(count: Int, start: Int, sampleRate: Double, rng: inout SplitMix64) -> [Double] {
        var hp1 = Biquad.highPass(cutoff: 2_000, sampleRate: sampleRate)
        var hp2 = Biquad.highPass(cutoff: 2_000, sampleRate: sampleRate)
        var out = [Double](repeating: 0, count: count)
        for i in max(0, start)..<min(count, start + Int(0.035 * sampleRate)) {
            let tau = Double(i - start) / sampleRate
            let envelope = min(1, tau / 0.0003) * (exp(-tau / 0.003) + 0.15 * exp(-tau / 0.02))
            out[i] = hp2.process(hp1.process(rng.gaussian())) * envelope
        }
        return out
    }

    static func add(_ a: inout [Double], _ b: [Double], gain: Double = 1) {
        for i in 0..<min(a.count, b.count) { a[i] += gain * b[i] }
    }

    /// Scales `shape` so its RMS over [t0, t0 + 2 ms] is `snrDb` above `noiseRms`.
    static func scaled(_ shape: [Double], t0: Double, snrDb: Double, noiseRms: Double, sampleRate: Double) -> [Double] {
        let from = Int(t0.rounded(.up)), to = from + Int(0.002 * sampleRate)
        let gain = noiseRms * dbToAmplitude(snrDb) / max(rms(shape, from, to), 1e-15)
        return shape.map { $0 * gain }
    }

    struct Scene {
        var sampleRate = 48_000.0
        var duration = 1.0
        var noiseDbfs = -60.0
        var t0 = 24_000.37
        var snrDb = 35.0
        /// Fat precursor: lead (ms) and level relative to the ball impact (dB, over its first 2 ms).
        var precursor: (leadMs: Double, relDb: Double)?
        /// Reverb tail level relative to the impact.
        var tailRelDb: Double?
        /// A second impact `ms` later at `relDb`.
        var echo: (ms: Double, relDb: Double)?
        /// Clip at ±1 after scaling the whole impact by this much (amplitude factor).
        var clipGain: Double?
        var seed: UInt64 = 1
    }

    /// Builds a scene and returns the samples plus the impact's true onset.
    static func render(_ scene: Scene) -> [Double] {
        var rng = SplitMix64(seed: scene.seed)
        let count = Int(scene.duration * scene.sampleRate)
        var signal = noise(count: count, levelDbfs: scene.noiseDbfs, rng: &rng)
        let noiseRms = dbToAmplitude(scene.noiseDbfs)
        let shape = impactShape(count: count, t0: scene.t0, sampleRate: scene.sampleRate, rng: &rng)
        var impact = scaled(shape, t0: scene.t0, snrDb: scene.snrDb, noiseRms: noiseRms, sampleRate: scene.sampleRate)
        let ballRms = rms(impact, Int(scene.t0.rounded(.up)), Int(scene.t0.rounded(.up)) + Int(0.002 * scene.sampleRate))
        if let echo = scene.echo {
            let t1 = scene.t0 + echo.ms * scene.sampleRate / 1_000
            let second = impactShape(count: count, t0: t1, sampleRate: scene.sampleRate, rng: &rng)
            add(&impact, scaled(second, t0: t1, snrDb: scene.snrDb + echo.relDb, noiseRms: noiseRms, sampleRate: scene.sampleRate))
        }
        if let tailRel = scene.tailRelDb {
            let from = Int(scene.t0) + Int(0.003 * scene.sampleRate)
            let raw = tail(count: count, from: from, to: Int(scene.t0) + Int(0.06 * scene.sampleRate), decayS: 0.012, sampleRate: scene.sampleRate, rng: &rng)
            add(&impact, raw, gain: ballRms * dbToAmplitude(tailRel))
        }
        if let precursor = scene.precursor {
            let start = scene.t0 - precursor.leadMs * scene.sampleRate / 1_000
            let raw = thud(count: count, start: start, sampleRate: scene.sampleRate, rng: &rng)
            let from = Int(start.rounded(.up))
            let span = max(4, min(Int(scene.t0) - from, Int(0.001 * scene.sampleRate)))
            let level = rms(raw, from, from + span)
            add(&impact, raw, gain: ballRms * dbToAmplitude(precursor.relDb) / max(level, 1e-15))
        }
        if let clipGain = scene.clipGain {
            let peak = impact.map(abs).max() ?? 1
            let gain = clipGain / max(peak, 1e-12)
            for i in impact.indices { impact[i] *= gain }
        }
        add(&signal, impact)
        if scene.clipGain != nil {
            for i in signal.indices { signal[i] = min(1, max(-1, signal[i])) }
        }
        return signal
    }
}
