import Accelerate
import Foundation

/// Real FFT helpers (Accelerate), only run around an onset.
enum Spectrum {
    /// `|X_k|²` of the unscaled DFT for k = 0…n/2 of `input` zero-padded (or cut) to `fftSize`,
    /// a power of two ≥ 4.
    static func power(_ input: [Double], fftSize: Int) -> [Double] {
        precondition(fftSize >= 4 && fftSize & (fftSize - 1) == 0, "fftSize must be a power of two")
        let log2n = vDSP_Length(fftSize.trailingZeroBitCount)
        guard let setup = vDSP_create_fftsetupD(log2n, FFTRadix(kFFTRadix2)) else { return [] }
        defer { vDSP_destroy_fftsetupD(setup) }
        let half = fftSize / 2
        var padded = [Double](repeating: 0, count: fftSize)
        for i in 0..<min(input.count, fftSize) { padded[i] = input[i] }
        var real = [Double](repeating: 0, count: half)
        var imag = [Double](repeating: 0, count: half)
        real.withUnsafeMutableBufferPointer { realBuffer in
            imag.withUnsafeMutableBufferPointer { imagBuffer in
                var split = DSPDoubleSplitComplex(realp: realBuffer.baseAddress!, imagp: imagBuffer.baseAddress!)
                padded.withUnsafeBufferPointer { paddedBuffer in
                    paddedBuffer.baseAddress!.withMemoryRebound(to: DSPDoubleComplex.self, capacity: half) {
                        vDSP_ctozD($0, 2, &split, 1, vDSP_Length(half))
                    }
                }
                vDSP_fft_zripD(setup, &split, 1, log2n, FFTDirection(FFT_FORWARD))
            }
        }
        // vDSP's real FFT returns 2·X; DC sits in real[0] and Nyquist in imag[0].
        var power = [Double](repeating: 0, count: half + 1)
        power[0] = (real[0] / 2) * (real[0] / 2)
        power[half] = (imag[0] / 2) * (imag[0] / 2)
        for k in 1..<half { power[k] = (real[k] * real[k] + imag[k] * imag[k]) / 4 }
        return power
    }

    static func fftSize(atLeast count: Int) -> Int {
        var size = 4
        while size < count { size <<= 1 }
        return size
    }

    static func hann(_ x: [Double]) -> [Double] {
        let n = x.count
        guard n > 1 else { return x }
        return x.enumerated().map { index, value in
            value * 0.5 * (1 - cos(2 * Double.pi * Double(index) / Double(n - 1)))
        }
    }

    /// Mean square of `x` that falls in `low..<high` Hz (rectangular window, Parseval).
    static func bandMeanSquare(_ x: [Double], sampleRate: Double, low: Double, high: Double) -> Double? {
        guard !x.isEmpty else { return nil }
        let size = fftSize(atLeast: x.count)
        let power = power(x, fftSize: size)
        let binHz = sampleRate / Double(size)
        var sum = 0.0
        for k in 0...(size / 2) {
            let f = Double(k) * binHz
            guard f >= low, f < high else { continue }
            sum += (k == 0 || k == size / 2 ? 1 : 2) * power[k]
        }
        return sum / (Double(size) * Double(x.count))
    }

    /// Magnitude-weighted spectral centroid (Hz) of a Hann-windowed `x`, DC excluded.
    static func centroid(_ x: [Double], sampleRate: Double, fftSize requested: Int? = nil) -> Double? {
        guard x.count >= 4 else { return nil }
        let size = max(requested ?? 0, fftSize(atLeast: x.count))
        let power = power(hann(x), fftSize: size)
        let binHz = sampleRate / Double(size)
        var weighted = 0.0, total = 0.0
        for k in 1...(size / 2) {
            let magnitude = power[k].squareRoot()
            weighted += Double(k) * binHz * magnitude
            total += magnitude
        }
        return total > 0 ? weighted / total : nil
    }
}

/// Everything `audio_onset` reports about one impact besides its time (design §9.6).
struct ImpactFeatures: Equatable {
    struct FatCue: Equatable {
        var detected: Bool
        /// t0 − tp in ms; nil when no precursor candidate exists at all.
        var leadMs: Double?
        /// Fractional absolute capture index of the candidate precursor onset.
        var precursorPosition: Double?
        /// Low-band (0.2-2 kHz) energy in [tp, t0) over the low-band pre-event floor.
        var precursorSnrDb: Double?
        /// Full-band energy in [tp, t0) relative to [t0, t0+2 ms].
        var precursorRelDb: Double?
        var precursorCentroidHz: Double?
        var aicDepth: Double?
    }

    struct SecondTransient: Equatable {
        var detected: Bool
        var delayMs: Double?
        /// Its envelope peak relative to the main peak.
        var relDb: Double?
    }

    var peakDbfs: Double?
    var clipped: Bool
    var snrDb: Double?
    var riseTimeMs: Double?
    var crestDb: Double?
    var bandLowDb: Double?
    var bandMidDb: Double?
    var bandHighDb: Double?
    var centroidHz0to2ms: Double?
    var centroidHz2to20ms: Double?
    var decayMs20dB: Double?
    var postLowDb: Double?
    /// Low-band energy in [t0−10, t0−1 ms] over the low-band floor in [t0−30, t0−10 ms]: mat or
    /// turf noise just before the strike, whether or not it forms a precursor onset.
    var preLowRelDb1to10ms: Double?
    var fatCue: FatCue
    var secondTransient: SecondTransient

    /// Computes the features of the onset at `onset` (fractional absolute index).
    ///
    /// `window` must hold `rawWindow` raw samples from at least 40 ms before to 20 ms after the
    /// onset; `rawFloat` is the same window before conversion, so clipping is judged on the values
    /// the microphone delivered.
    static func compute(
        window: AnalysisWindow, rawFloat: [Float], onset: Double, refined: RefinedOnset,
        trigger: OnsetTrigger, config: OnsetConfig
    ) -> ImpactFeatures {
        let fs = window.sampleRate
        let x = window.samples
        let local = window.local(onset)
        let i0 = Int(local.rounded(.up))
        func n(_ ms: Double) -> Int { window.samples(ms: ms) }
        func slice(_ from: Int, _ to: Int) -> [Double] {
            let from = max(0, from), to = min(x.count, to)
            return to > from ? Array(x[from..<to]) : []
        }

        let hp = BiquadChain.highPass(cutoff: config.highPassHz, sampleRate: fs).filtered(x)
        let high = OnsetRefiner.highBand(config, sampleRate: fs).filtered(x)
        let low = BiquadChain.band(low: config.fatBandHz.low, high: config.fatBandHz.high, sampleRate: fs).filtered(x)

        // Level, clipping, crest.
        var peak = 0.0
        var clipped = false
        for i in max(0, i0)..<min(x.count, i0 + n(5)) {
            peak = max(peak, abs(x[i]))
            if i < rawFloat.count, abs(rawFloat[i]) >= config.clipLevel { clipped = true }
        }
        let peakDbfs = peak > 0 ? 20 * log10(peak) : nil
        let rms10 = (Energy.meanSquare(x, i0, i0 + n(10)) ?? 0).squareRoot()
        var peak10 = 0.0
        for i in max(0, i0)..<min(x.count, i0 + n(10)) { peak10 = max(peak10, abs(x[i])) }
        let crest = rms10 > 0 && peak10 > 0 ? 20 * log10(peak10 / rms10) : nil

        let snr = Energy.db(hp, i0, i0 + n(2)).map { $0 - trigger.floorDbfs }

        // Bands and centroids.
        let attack = slice(i0, i0 + n(10))
        func band(_ lo: Double, _ hi: Double) -> Double? {
            Energy.db(Spectrum.bandMeanSquare(attack, sampleRate: fs, low: lo, high: hi))
        }
        let centroidAttack = Spectrum.centroid(slice(i0, i0 + n(2)), sampleRate: fs, fftSize: 128)
        let centroidTail = Spectrum.centroid(slice(i0 + n(2), i0 + n(20)), sampleRate: fs, fftSize: 1_024)

        let decay = decayTime(hp, from: i0, peakWithin: n(5), end: min(hp.count, i0 + n(config.featureWindowMs)), sampleRate: fs)
        let postLow: Double? = {
            guard let lowDb = Energy.db(low, i0 + n(0.5), i0 + n(8)),
                  let highDb = Energy.db(high, i0 + n(0.5), i0 + n(8)) else { return nil }
            return lowDb - highDb
        }()
        let lowFloor = Energy.db(low, i0 - n(config.fatFloorWindowMs.from), i0 - n(config.fatFloorWindowMs.to))
        let preLow: Double? = {
            guard let pre = Energy.db(low, i0 - n(10), i0 - n(1)),
                  let floor = Energy.db(low, i0 - n(30), i0 - n(10)) else { return nil }
            return pre - floor
        }()

        return ImpactFeatures(
            peakDbfs: peakDbfs,
            clipped: clipped,
            snrDb: snr,
            riseTimeMs: refined.riseTimeMs,
            crestDb: crest,
            bandLowDb: band(200, 1_000),
            bandMidDb: band(1_000, 4_000),
            bandHighDb: band(4_000, 12_000),
            centroidHz0to2ms: centroidAttack,
            centroidHz2to20ms: centroidTail,
            decayMs20dB: decay,
            postLowDb: postLow,
            preLowRelDb1to10ms: preLow,
            fatCue: fatCue(window: window, low: low, onset: local, i0: i0, lowFloorDb: lowFloor, config: config),
            secondTransient: secondTransient(hp, i0: i0, onset: local, floorDbfs: trigger.floorDbfs, sampleRate: fs, config: config)
        )
    }

    /// ms from the 0.5 ms-RMS envelope peak (within `peakWithin` of `from`) until it is 20 dB
    /// below that peak; nil when that does not happen before `end`.
    static func decayTime(_ x: [Double], from: Int, peakWithin: Int, end: Int, sampleRate: Double) -> Double? {
        let span = max(4, Int((0.5 * sampleRate / 1_000).rounded()))
        guard from >= 0, end > from + span else { return nil }
        var envelope = [Double](repeating: 0, count: end - from)
        var sum = 0.0
        for i in from..<end {
            sum += x[i] * x[i]
            if i - span >= from { sum -= x[i - span] * x[i - span] }
            envelope[i - from] = max(sum, 0) / Double(min(span, i - from + 1))
        }
        var peakIndex = 0
        for i in 0..<min(envelope.count, peakWithin) where envelope[i] > envelope[peakIndex] { peakIndex = i }
        let target = envelope[peakIndex] * 0.01  // −20 dB in energy terms
        guard envelope[peakIndex] > 0 else { return nil }
        for i in (peakIndex + 1)..<envelope.count where envelope[i] <= target {
            return Double(i - peakIndex) * 1_000 / sampleRate
        }
        return nil
    }

    /// The fat-strike cue (§9.6): an AIC pick on the low band in [t0−6 ms, t0−0.15 ms], accepted
    /// when the low band in [tp, t0−0.1 ms) clears the pre-event floor and the dip is deep.
    static func fatCue(
        window: AnalysisWindow, low: [Double], onset: Double, i0: Int, lowFloorDb: Double?, config: OnsetConfig
    ) -> FatCue {
        let fs = window.sampleRate
        func n(_ ms: Double) -> Int { window.samples(ms: ms) }
        let from = i0 - n(config.fatSearchMs)
        let to = i0 - n(config.fatMinLeadMs)
        var cue = FatCue(detected: false)
        guard from >= 0, to - from > 16 else { return cue }
        let pick = low.withUnsafeBufferPointer { AICPicker.pick(UnsafeBufferPointer(rebasing: $0[from..<to])) }
        guard let pick else { return cue }
        let tp = Double(from) + pick.position
        let start = Int(tp.rounded(.up))
        let end = i0 - max(1, n(0.1))
        let lead = (onset - tp) * 1_000 / fs
        cue.leadMs = lead
        cue.precursorPosition = Double(window.start) + tp
        cue.aicDepth = pick.depth
        let precursorDb = end - start >= 2 ? Energy.db(low, start, end) : nil
        if let precursorDb, let lowFloorDb { cue.precursorSnrDb = precursorDb - lowFloorDb }
        if let pre = Energy.db(window.samples, start, i0), let ball = Energy.db(window.samples, i0, i0 + n(2)) {
            cue.precursorRelDb = pre - ball
        }
        if i0 - start >= 4 {
            cue.precursorCentroidHz = Spectrum.centroid(
                Array(window.samples[start..<min(i0, start + 128)]), sampleRate: fs, fftSize: 128
            )
        }
        cue.detected = (cue.precursorSnrDb ?? -.infinity) >= config.fatMinOverFloorDb
            && pick.depth >= config.fatMinAICDepth
            && lead >= config.fatMinLeadMs
        return cue
    }

    /// A second transient within 20 ms (§9.6, brief): a 0.25 ms sub-frame of the 1 kHz
    /// high-passed signal that is `secondRiseDb` louder than the mean of the 1 ms before it,
    /// `secondOverFloorDb` above the floor and within `secondMaxBelowMainDb` of the main peak.
    static func secondTransient(
        _ hp: [Double], i0: Int, onset: Double, floorDbfs: Double, sampleRate: Double, config: OnsetConfig
    ) -> SecondTransient {
        let sub = max(2, Int((config.secondSubframeMs * sampleRate / 1_000).rounded()))
        let first = i0 + Int((config.secondMinDelayMs * sampleRate / 1_000).rounded())
        let last = min(hp.count, i0 + Int((config.secondMaxDelayMs * sampleRate / 1_000).rounded()))
        guard i0 >= 0, last - i0 > 8 * sub else { return SecondTransient(detected: false) }
        var energies: [Double] = []  // mean square per sub-frame
        var starts: [Int] = []
        var position = i0
        while position + sub <= last {
            energies.append(Energy.meanSquare(hp, position, position + sub) ?? 0)
            starts.append(position)
            position += sub
        }
        func db(_ value: Double) -> Double { 10 * log10(value + 1e-20) }
        let mainPeak = db(energies.prefix(max(1, (first - i0) / sub)).max() ?? 0)
        for j in 4..<energies.count where starts[j] >= first {
            let baseline = (energies[j - 4] + energies[j - 3] + energies[j - 2] + energies[j - 1]) / 4
            let e = db(energies[j])
            guard e - db(baseline) >= config.secondRiseDb,
                  e - floorDbfs >= config.secondOverFloorDb,
                  mainPeak - e <= config.secondMaxBelowMainDb else { continue }
            let from = max(i0 + 1, starts[j] - 4 * sub)
            let to = min(hp.count, starts[j] + 2 * sub)
            let pick = hp.withUnsafeBufferPointer { AICPicker.pick(UnsafeBufferPointer(rebasing: $0[from..<to])) }
            let t2 = pick.map { Double(from) + $0.position } ?? Double(starts[j])
            let peakAfter = db(energies[j..<min(energies.count, j + 4)].max() ?? energies[j])
            return SecondTransient(detected: true, delayMs: (t2 - onset) * 1_000 / sampleRate, relDb: peakAfter - mainPeak)
        }
        return SecondTransient(detected: false)
    }
}
