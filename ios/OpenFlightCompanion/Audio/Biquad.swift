import Foundation

/// One second-order IIR section (RBJ Audio EQ Cookbook), transposed direct form II, in Double.
/// Pure and causal: a filter that starts from zero state produces nothing before its input
/// does, which is what keeps onset picks on filtered signals unbiased (design §9.5).
struct Biquad {
    private(set) var b0: Double
    private(set) var b1: Double
    private(set) var b2: Double
    private(set) var a1: Double
    private(set) var a2: Double
    private var z1 = 0.0
    private var z2 = 0.0

    /// Butterworth Q for a single 2nd-order section.
    static let butterworthQ = 1 / 2.0.squareRoot()

    private init(b0: Double, b1: Double, b2: Double, a0: Double, a1: Double, a2: Double) {
        self.b0 = b0 / a0
        self.b1 = b1 / a0
        self.b2 = b2 / a0
        self.a1 = a1 / a0
        self.a2 = a2 / a0
    }

    /// Clamps a corner frequency into (0, Nyquist) so a 44.1 kHz route never builds an unstable section.
    private static func omega(_ frequency: Double, _ sampleRate: Double) -> Double {
        let nyquist = sampleRate / 2
        let clamped = min(max(frequency, 1), nyquist * 0.95)
        return 2 * Double.pi * clamped / sampleRate
    }

    static func highPass(cutoff: Double, sampleRate: Double, q: Double = butterworthQ) -> Biquad {
        let w = omega(cutoff, sampleRate)
        let cosw = cos(w)
        let alpha = sin(w) / (2 * q)
        return Biquad(
            b0: (1 + cosw) / 2, b1: -(1 + cosw), b2: (1 + cosw) / 2,
            a0: 1 + alpha, a1: -2 * cosw, a2: 1 - alpha
        )
    }

    static func lowPass(cutoff: Double, sampleRate: Double, q: Double = butterworthQ) -> Biquad {
        let w = omega(cutoff, sampleRate)
        let cosw = cos(w)
        let alpha = sin(w) / (2 * q)
        return Biquad(
            b0: (1 - cosw) / 2, b1: 1 - cosw, b2: (1 - cosw) / 2,
            a0: 1 + alpha, a1: -2 * cosw, a2: 1 - alpha
        )
    }

    @inline(__always)
    mutating func process(_ x: Double) -> Double {
        let y = b0 * x + z1
        z1 = b1 * x - a1 * y + z2
        z2 = b2 * x - a2 * y
        return y
    }

    mutating func reset() {
        z1 = 0
        z2 = 0
    }
}

/// Sections in series: a band is a high-pass followed by a low-pass.
struct BiquadChain {
    private var sections: [Biquad]

    init(_ sections: [Biquad]) {
        self.sections = sections
    }

    /// `low`…`high` Hz as a 2nd-order Butterworth high-pass then low-pass.
    static func band(low: Double, high: Double, sampleRate: Double) -> BiquadChain {
        BiquadChain([
            .highPass(cutoff: low, sampleRate: sampleRate),
            .lowPass(cutoff: high, sampleRate: sampleRate),
        ])
    }

    static func highPass(cutoff: Double, sampleRate: Double) -> BiquadChain {
        BiquadChain([.highPass(cutoff: cutoff, sampleRate: sampleRate)])
    }

    @inline(__always)
    mutating func process(_ x: Double) -> Double {
        var y = x
        for index in sections.indices {
            y = sections[index].process(y)
        }
        return y
    }

    mutating func reset() {
        for index in sections.indices { sections[index].reset() }
    }

    /// Filters a whole window from zero state (fresh copy; `self` is untouched).
    func filtered(_ input: [Double]) -> [Double] {
        var chain = self
        chain.reset()
        return input.map { chain.process($0) }
    }
}
