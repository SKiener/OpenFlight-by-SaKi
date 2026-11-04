import Foundation

/// Maps capture sample indices to phone host time (design §9.3).
///
/// Every input buffer contributes one `(first sample index, host µs)` stamp. The fit keeps the
/// stamps of the last `windowSeconds`, fits `host_us = a + b · sample` by least squares, drops
/// stamps whose residual is further than `outlierMADs` scaled MADs from the median residual and
/// fits again (two rounds), once per `refitSeconds`. An onset's host time always comes from the fit, never from a single buffer
/// stamp, so callback jitter cancels. Pure value type.
struct HostTimeFit {
    struct Stamp: Equatable {
        let sample: Int64
        let hostMicroseconds: Double
    }

    /// `host = hostOrigin + slope · (sample − sampleOrigin)`; `slope` is µs per sample.
    struct Model: Equatable {
        let sampleOrigin: Int64
        let hostOrigin: Double
        let slope: Double
        /// RMS residual of the stamps the final fit kept, µs.
        let rmsMicroseconds: Double
        let stampsUsed: Int
        let stampsDropped: Int

        func hostMicroseconds(at sample: Double) -> Double {
            hostOrigin + slope * (sample - Double(sampleOrigin))
        }

        /// The audio clock's rate measured on the host clock.
        var sampleRateEffectiveHz: Double { 1e6 / slope }
    }

    static let windowSeconds = 10.0
    static let refitSeconds = 1.0
    static let outlierMADs = 3.0
    /// A fit needs this many stamps spanning at least `minimumSpanSeconds`.
    static let minimumStamps = 8
    static let minimumSpanSeconds = 0.1
    /// A fitted rate further than this from nominal is rejected (a broken stamp stream).
    static let maximumRateError = 0.01

    let nominalSampleRate: Double
    private let windowSamples: Int64
    private let refitSamples: Int64
    private var stamps: [Stamp] = []
    private var head = 0
    private(set) var model: Model?
    private var lastFitSample = Int64.min

    init(nominalSampleRate: Double) {
        self.nominalSampleRate = nominalSampleRate
        windowSamples = Int64((Self.windowSeconds * nominalSampleRate).rounded())
        refitSamples = Int64((Self.refitSeconds * nominalSampleRate).rounded())
        stamps.reserveCapacity(4_096)
    }

    /// Stamps currently held (bounded by the window).
    var count: Int { stamps.count - head }

    mutating func reset() {
        stamps.removeAll(keepingCapacity: true)
        head = 0
        model = nil
        lastFitSample = .min
    }

    /// Adds one buffer's stamp; refits when a second has passed since the last fit.
    mutating func add(sample: Int64, hostMicroseconds: Double) {
        guard hostMicroseconds.isFinite else { return }
        if let last = stamps.last, sample <= last.sample {
            // The stream restarted below the newest stamp: start the fit over.
            reset()
        }
        stamps.append(Stamp(sample: sample, hostMicroseconds: hostMicroseconds))
        while head < stamps.count, sample - stamps[head].sample > windowSamples { head += 1 }
        if head > 2_048, head * 2 > stamps.count {
            stamps.removeFirst(head)
            head = 0
        }
        if model == nil || sample - lastFitSample >= refitSamples { refit() }
    }

    /// Host µs of a (fractional) sample index: the fit, or the newest stamp at the nominal rate
    /// until there is enough data for a fit. nil before the first stamp.
    func hostMicroseconds(at sample: Double) -> Double? {
        if let model { return model.hostMicroseconds(at: sample) }
        guard let last = stamps.last else { return nil }
        return last.hostMicroseconds + (sample - Double(last.sample)) * 1e6 / nominalSampleRate
    }

    mutating func refit() {
        let window = stamps[head...]
        guard window.count >= Self.minimumStamps, let first = window.first, let last = window.last,
              Double(last.sample - first.sample) >= Self.minimumSpanSeconds * nominalSampleRate
        else { return }
        lastFitSample = last.sample
        let origin = first
        let xs = window.map { Double($0.sample - origin.sample) }
        let ys = window.map { $0.hostMicroseconds - origin.hostMicroseconds }
        guard var line = Self.leastSquares(xs, ys, keep: nil) else { return }
        var keep = [Bool](repeating: true, count: xs.count)
        // Drop stamps beyond `outlierMADs` scaled MADs of the median residual and fit again; a
        // second round catches outliers the first, still contaminated, fit had hidden.
        for _ in 0..<2 {
            let residuals = xs.indices.map { ys[$0] - (line.a + line.b * xs[$0]) }
            let kept = xs.indices.filter { keep[$0] }.map { residuals[$0] }
            let center = Self.median(kept)
            let scaledMAD = 1.4826 * Self.median(kept.map { abs($0 - center) })
            // A floor keeps perfect (synthetic) stamps from dropping everything over rounding noise.
            let limit = max(Self.outlierMADs * scaledMAD, 0.5)
            let next = residuals.map { abs($0 - center) <= limit }
            guard next.filter({ $0 }).count >= Self.minimumStamps,
                  let refined = Self.leastSquares(xs, ys, keep: next) else { break }
            keep = next
            line = refined
        }
        let nominalSlope = 1e6 / nominalSampleRate
        guard abs(line.b / nominalSlope - 1) <= Self.maximumRateError else { return }
        var sum = 0.0
        var used = 0
        for index in xs.indices where keep[index] {
            let r = ys[index] - (line.a + line.b * xs[index])
            sum += r * r
            used += 1
        }
        model = Model(
            sampleOrigin: origin.sample,
            hostOrigin: origin.hostMicroseconds + line.a,
            slope: line.b,
            rmsMicroseconds: (sum / Double(max(used, 1))).squareRoot(),
            stampsUsed: used,
            stampsDropped: xs.count - used
        )
    }

    private static func leastSquares(_ xs: [Double], _ ys: [Double], keep: [Bool]?) -> (a: Double, b: Double)? {
        var n = 0.0, sx = 0.0, sy = 0.0
        for index in xs.indices where keep?[index] ?? true {
            n += 1
            sx += xs[index]
            sy += ys[index]
        }
        guard n >= 2 else { return nil }
        let mx = sx / n, my = sy / n
        var sxx = 0.0, sxy = 0.0
        for index in xs.indices where keep?[index] ?? true {
            let dx = xs[index] - mx
            sxx += dx * dx
            sxy += dx * (ys[index] - my)
        }
        guard sxx > 0 else { return nil }
        let b = sxy / sxx
        return (my - b * mx, b)
    }

    private static func median(_ values: [Double]) -> Double {
        guard !values.isEmpty else { return 0 }
        let sorted = values.sorted()
        let middle = sorted.count / 2
        return sorted.count % 2 == 0 ? (sorted[middle - 1] + sorted[middle]) / 2 : sorted[middle]
    }
}

/// `mach_absolute_time()` ticks → µs (the `CLOCK_UPTIME_RAW` base, design F14).
enum HostTicks {
    private static let timebase: (numer: UInt64, denom: UInt64) = {
        var info = mach_timebase_info_data_t()
        mach_timebase_info(&info)
        return (UInt64(max(info.numer, 1)), UInt64(max(info.denom, 1)))
    }()

    static func microseconds(_ ticks: UInt64) -> Double {
        // Split to avoid overflowing `ticks · numer` for long uptimes.
        let (q, r) = ticks.quotientAndRemainder(dividingBy: timebase.denom)
        let nanoseconds = Double(q) * Double(timebase.numer) + Double(r * timebase.numer) / Double(timebase.denom)
        return nanoseconds / 1_000
    }

    static func now() -> UInt64 { mach_absolute_time() }
}
