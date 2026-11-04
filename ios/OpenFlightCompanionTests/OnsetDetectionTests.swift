import XCTest
@testable import OpenFlightCompanion

/// The onset pipeline on synthetic signals (design §12.1 "Synthetic signals", assertions 1-5, 7).
/// Every scene is seeded, so a failure names a seed that reproduces exactly.
final class OnsetDetectionTests: XCTestCase {
    /// Feeds `x` to a fresh analyzer in `chunk`-sized buffers, as the drain loop does.
    private func analyze(_ x: [Double], sampleRate: Double = 48_000, chunk: Int = 256, config: OnsetConfig = .v1) -> [DetectedOnset] {
        let analyzer = ImpactAnalyzer(sampleRate: sampleRate, config: config)
        let samples = x.map(Float.init)
        var onsets: [DetectedOnset] = []
        var start = 0
        while start < samples.count {
            let end = min(samples.count, start + chunk)
            onsets += analyzer.process(Array(samples[start..<end]), startIndex: Int64(start)).onsets
            start = end
        }
        return onsets
    }

    private func errorUs(_ onset: DetectedOnset, _ t0: Double, _ sampleRate: Double) -> Double {
        abs(onset.refined.position - t0) / sampleRate * 1e6
    }

    /// A scene with the impact at a random fractional sample between 0.35 s and 0.35 s + 300 samples.
    private func scene(seed: UInt64, sampleRate: Double, snrDb: Double) -> SyntheticAudio.Scene {
        var scene = SyntheticAudio.Scene()
        scene.sampleRate = sampleRate
        scene.duration = 0.6
        scene.seed = seed + 7
        var rng = SplitMix64(seed: seed)
        scene.t0 = 0.35 * sampleRate + rng.uniform(0...1) * 300
        scene.snrDb = snrDb
        return scene
    }

    // MARK: 1. Timing

    func testImpactInNoiseIsTimedWithinATenthOfAMillisecond() {
        for sampleRate in [44_100.0, 48_000.0] {
            for seed in UInt64(0)..<60 {
                let scene = scene(seed: seed, sampleRate: sampleRate, snrDb: 35)
                let chunk = [256, 480, 1_024, 4_096][Int(seed % 4)]
                let onsets = analyze(SyntheticAudio.render(scene), sampleRate: sampleRate, chunk: chunk)
                XCTAssertEqual(onsets.count, 1, "fs \(sampleRate) seed \(seed)")
                guard let onset = onsets.first else { continue }
                XCTAssertLessThanOrEqual(errorUs(onset, scene.t0, sampleRate), 100, "fs \(sampleRate) seed \(seed)")
            }
        }
    }

    func testTimingDegradesGracefullyAtLowerSNR() {
        for (snr, p90Limit, maxLimit) in [(30.0, 60.0, 150.0), (20.0, 120.0, 300.0)] {
            var errors: [Double] = []
            for seed in UInt64(0)..<60 {
                let scene = scene(seed: seed + 500, sampleRate: 48_000, snrDb: snr)
                let onsets = analyze(SyntheticAudio.render(scene))
                XCTAssertEqual(onsets.count, 1, "snr \(snr) seed \(seed)")
                if let onset = onsets.first { errors.append(errorUs(onset, scene.t0, 48_000)) }
            }
            errors.sort()
            XCTAssertLessThanOrEqual(errors[Int(0.9 * Double(errors.count))], p90Limit, "snr \(snr) p90")
            XCTAssertLessThanOrEqual(errors.last ?? .infinity, maxLimit, "snr \(snr) max")
        }
    }

    func testUncertaintyCoversTheTrueErrorMostOfTheTime() {
        var covered = 0
        let seeds = 60
        for seed in UInt64(0)..<UInt64(seeds) {
            let scene = scene(seed: seed + 900, sampleRate: 48_000, snrDb: 30)
            guard let onset = analyze(SyntheticAudio.render(scene)).first else { XCTFail("seed \(seed)"); continue }
            if abs(onset.refined.position - scene.t0) <= onset.refined.uncertaintySamples { covered += 1 }
            XCTAssertGreaterThanOrEqual(onset.refined.uncertaintySamples, 1 / 12.0.squareRoot())
        }
        XCTAssertGreaterThanOrEqual(Double(covered) / Double(seeds), 0.6)
    }

    func testBufferSplitsDoNotChangeTheResult() {
        var scene = SyntheticAudio.Scene()
        scene.seed = 7
        // The onset sits on a 256-frame buffer boundary.
        scene.t0 = 24_064.0
        let x = SyntheticAudio.render(scene)
        let reference = analyze(x, chunk: 256)
        XCTAssertEqual(reference.count, 1)
        for chunk in [37, 480, 1_024, 4_096, 48_000] {
            XCTAssertEqual(analyze(x, chunk: chunk).map(\.refined.position), reference.map(\.refined.position), "chunk \(chunk)")
        }
        XCTAssertLessThanOrEqual(abs((reference.first?.refined.position ?? 0) - scene.t0), 4.8)
    }

    // MARK: 2. Negatives

    func testTalkingGivesNoOnset() {
        for seed in UInt64(0)..<30 {
            var rng = SplitMix64(seed: seed)
            var x = SyntheticAudio.noise(count: 96_000, rng: &rng)
            let attack = [0.01, 0.02, 0.03][Int(seed % 3)]
            let level = SyntheticAudio.dbToAmplitude(-20 + Double(seed % 3) * 6) / 3
            for word in 0..<4 {
                let burst = SyntheticAudio.speech(count: x.count, start: 12_000 + word * 18_000, sampleRate: 48_000, attackS: attack, rng: &rng)
                SyntheticAudio.add(&x, burst, gain: level)
            }
            XCTAssertEqual(analyze(x).count, 0, "seed \(seed) attack \(attack)")
        }
    }

    /// Voiceless stops ("t", "k"): a sub-ms burst of > 2 kHz noise after the closure's silence,
    /// 3 ms decay and 20 ms aspiration, then the vowel 40 ms later. Levels are the burst's first
    /// 2 ms re the vowel RMS. Pins what the detector does with them (design §12.1, assertion 2):
    /// a burst as sharp and isolated as an impact is an impact to the onset gate, like the clap.
    /// At -15 dB and below no burst fires; at -10 dB some do, and each such onset sits on a
    /// burst, never inside the vowel.
    func testConsonantBurstsInSpeech() {
        let fs = 48_000.0
        for relDb in [-10.0, -15.0, -20.0, -25.0] {
            var onsets = 0
            for seed in UInt64(0)..<20 {
                var rng = SplitMix64(seed: seed + 4_242)
                let count = 96_000
                var x = SyntheticAudio.noise(count: count, levelDbfs: -60, rng: &rng)
                let vowelGain = SyntheticAudio.dbToAmplitude(-20) / 3
                var bursts: [Int] = []
                for word in 0..<4 {
                    let burstAt = 12_000 + word * 18_000
                    bursts.append(burstAt)
                    let vowelAt = burstAt + Int(0.04 * fs)
                    let vowel = SyntheticAudio.speech(count: count, start: vowelAt, sampleRate: fs, attackS: 0.02, rng: &rng)
                    let vowelRms = SyntheticAudio.rms(vowel, vowelAt + Int(0.03 * fs), vowelAt + Int(0.1 * fs)) * vowelGain
                    SyntheticAudio.add(&x, vowel, gain: vowelGain)
                    let burst = SyntheticAudio.plosive(count: count, start: burstAt, sampleRate: fs, rng: &rng)
                    let burstRms = SyntheticAudio.rms(burst, burstAt, burstAt + Int(0.002 * fs))
                    SyntheticAudio.add(&x, burst, gain: vowelRms * SyntheticAudio.dbToAmplitude(relDb) / max(burstRms, 1e-15))
                }
                let found = analyze(x)
                onsets += found.count
                for onset in found {
                    let nearest = bursts.map { abs(onset.refined.position - Double($0)) }.min() ?? .infinity
                    XCTAssertLessThan(nearest, 0.002 * fs, "burst \(relDb) dB seed \(seed): onset off the burst")
                }
            }
            if relDb <= -15 { XCTAssertEqual(onsets, 0, "burst \(relDb) dB re vowel RMS") }
        }
    }

    func testSwellsAndWagglesGiveNoOnset() {
        for seed in UInt64(0)..<20 {
            var rng = SplitMix64(seed: seed + 50)
            var x = SyntheticAudio.noise(count: 96_000, rng: &rng)
            // A practice swing's whoosh: broadband noise swelling by 40 dB over 0.3 s.
            SyntheticAudio.add(&x, SyntheticAudio.swell(count: x.count, start: 12_000, durationS: 0.3, riseDb: 40, sampleRate: 48_000, rng: &rng), gain: 0.05)
            // A club tapped on the mat while waggling: a dull thud.
            SyntheticAudio.add(&x, SyntheticAudio.thud(count: x.count, start: 60_000.5, sampleRate: 48_000, rng: &rng), gain: 0.01)
            XCTAssertEqual(analyze(x).count, 0, "seed \(seed)")
        }
    }

    func testNoiseAloneGivesNoOnset() {
        var rng = SplitMix64(seed: 3)
        XCTAssertEqual(analyze(SyntheticAudio.noise(count: 48_000 * 5, rng: &rng)).count, 0)
    }

    // MARK: 3. Refractory, two shots, neighbours

    func testTwoShotsFourHundredMillisecondsApartGiveTwoOnsets() {
        for seed in UInt64(0)..<20 {
            let (x, own) = shots(seed: seed, first: (0.3, 0), second: (0.7, 0))
            let onsets = analyze(x)
            XCTAssertEqual(onsets.count, 2, "seed \(seed)")
            for (onset, t0) in zip(onsets, own) {
                XCTAssertLessThanOrEqual(errorUs(onset, t0, 48_000), 100, "seed \(seed)")
            }
            XCTAssertEqual(onsets.map(\.seq), [1, 2])
        }
    }

    func testReflectionsInsideTheRefractoryPeriodGiveOneOnset() {
        for seed in UInt64(0)..<20 {
            let (x, _) = shots(seed: seed, first: (0.3, 0), second: (0.4, -3))
            XCTAssertEqual(analyze(x).count, 1, "seed \(seed)")
        }
    }

    func testNeighbourShotTwentyDecibelsQuieterDoesNotHideOrMoveOurs() {
        // (neighbour offset from our shot in s): before and after, inside and outside the refractory period.
        for offset in [-0.3, -0.06, 0.3] {
            for seed in UInt64(0)..<20 {
                let (x, times) = shots(seed: seed + 200, first: (0.6, 0), second: (0.6 + offset, -20))
                let ownT0 = times[0]
                let onsets = analyze(x)
                let own = onsets.first { abs($0.refined.position - ownT0) < 48 }
                XCTAssertNotNil(own, "offset \(offset) seed \(seed)")
                if let own { XCTAssertLessThanOrEqual(errorUs(own, ownT0, 48_000), 100, "offset \(offset) seed \(seed)") }
                // Outside the refractory period the neighbour is an onset of its own (the server labels the bay).
                if abs(offset) > 0.15 {
                    XCTAssertEqual(onsets.count, 2, "offset \(offset) seed \(seed)")
                    let neighbour = onsets.first { abs($0.refined.position - times[1]) < 48 }
                    if let neighbour, let own, let a = own.features.peakDbfs, let b = neighbour.features.peakDbfs {
                        XCTAssertEqual(a - b, 20, accuracy: 6, "offset \(offset) seed \(seed)")
                    } else {
                        XCTFail("neighbour missing, offset \(offset) seed \(seed)")
                    }
                }
            }
        }
    }

    /// Design §11: spherical spreading puts neighbours 4.6-7.7 dB quieter than our own ball. A
    /// neighbour that fires 30-140 ms before ours (inside its refractory period) must not hide
    /// ours. At 4.6 dB the frame energies of two random impacts overlap now and then, so a few
    /// misses are allowed there; from 7.7 dB down none.
    func testQuieterNeighbourJustBeforeOursDoesNotHideOurs() {
        for lead in [0.03, 0.05, 0.1, 0.14] {
            for (rel, minimumFound) in [(-4.6, 16), (-7.7, 20), (-10.0, 20), (-12.0, 20)] {
                var found = 0
                for seed in UInt64(0)..<20 {
                    let (x, times) = shots(seed: seed + 777, first: (0.6, 0), second: (0.6 - lead, rel))
                    let ownT0 = times[0]
                    if let own = analyze(x).first(where: { abs($0.refined.position - ownT0) < 48 }) {
                        found += 1
                        XCTAssertLessThanOrEqual(errorUs(own, ownT0, 48_000), 100, "lead \(lead) rel \(rel) seed \(seed)")
                    }
                }
                XCTAssertGreaterThanOrEqual(found, minimumFound, "lead \(lead) s, neighbour \(rel) dB: ours found \(found)/20")
            }
        }
    }

    /// Two impacts in -60 dBFS noise at 40 dB SNR plus `relDb`; returns the samples and both true onsets.
    private func shots(seed: UInt64, first: (Double, Double), second: (Double, Double)) -> ([Double], [Double]) {
        let fs = 48_000.0
        var rng = SplitMix64(seed: seed)
        let count = Int(1.2 * fs)
        var x = SyntheticAudio.noise(count: count, levelDbfs: -60, rng: &rng)
        let noise = SyntheticAudio.dbToAmplitude(-60)
        var times: [Double] = []
        for (at, rel) in [first, second] {
            let t0 = at * fs + rng.uniform(0...1)
            let shape = SyntheticAudio.impactShape(count: count, t0: t0, sampleRate: fs, rng: &rng)
            SyntheticAudio.add(&x, SyntheticAudio.scaled(shape, t0: t0, snrDb: 40 + rel, noiseRms: noise, sampleRate: fs))
            times.append(t0)
        }
        return (x, times)
    }

    // MARK: 4. Fat cue

    /// Leads of 1-5 ms (3-20 cm behind the ball at 30-40 m/s). Shorter precursors are not
    /// separated reliably yet (design §9.6, "Stage 2 deviations"): the pick then stays on the mat.
    func testMatThenBallGivesAFatCueAndKeepsTheBallOnset() {
        for lead in [1.0, 3.0, 5.0] {
            let minimum = lead < 2 ? 0.7 : 0.85
            for rel in [-6.0, -12.0] {
                var detected = 0, onBall = 0, leadErrors: [Double] = []
                let seeds = 20
                for seed in UInt64(0)..<UInt64(seeds) {
                    var scene = SyntheticAudio.Scene()
                    scene.seed = seed + 1_000
                    scene.snrDb = 40
                    scene.precursor = (lead, rel)
                    guard let onset = analyze(SyntheticAudio.render(scene)).first else { continue }
                    if errorUs(onset, scene.t0, 48_000) <= 100 { onBall += 1 }
                    let cue = onset.features.fatCue
                    if cue.detected, let measured = cue.leadMs {
                        detected += 1
                        leadErrors.append(abs(measured - lead))
                    }
                }
                XCTAssertGreaterThanOrEqual(detected, Int(minimum * Double(seeds)), "lead \(lead) rel \(rel)")
                XCTAssertGreaterThanOrEqual(onBall, Int(minimum * Double(seeds)), "lead \(lead) rel \(rel): onset must stay on the ball")
                leadErrors.sort()
                XCTAssertLessThanOrEqual(leadErrors[leadErrors.count / 2], 0.15, "lead \(lead) rel \(rel) median lead error")
            }
        }
    }

    func testCleanImpactsWithReverbHaveNoFatCue() {
        for seed in UInt64(0)..<60 {
            var scene = SyntheticAudio.Scene()
            scene.seed = seed
            scene.tailRelDb = -20
            guard let onset = analyze(SyntheticAudio.render(scene)).first else { XCTFail("seed \(seed)"); continue }
            XCTAssertFalse(onset.features.fatCue.detected, "seed \(seed)")
            XCTAssertFalse(onset.features.secondTransient.detected, "seed \(seed)")
        }
    }

    func testPrecursorTooCloseToSeparateGivesNoCue() {
        var cues = 0
        for seed in UInt64(0)..<30 {
            var scene = SyntheticAudio.Scene()
            scene.seed = seed + 3_000
            scene.snrDb = 40
            scene.precursor = (0.1, -6)
            if analyze(SyntheticAudio.render(scene)).first?.features.fatCue.detected == true { cues += 1 }
        }
        XCTAssertLessThanOrEqual(cues, 2)
    }

    func testFeaturesReportEnergyBeforeTheStrike() throws {
        var fat = SyntheticAudio.Scene()
        fat.seed = 1_005
        fat.snrDb = 40
        fat.precursor = (5, -6)
        var clean = fat
        clean.precursor = nil
        let withMat = try XCTUnwrap(analyze(SyntheticAudio.render(fat)).first?.features.preLowRelDb1to10ms)
        let without = try XCTUnwrap(analyze(SyntheticAudio.render(clean)).first?.features.preLowRelDb1to10ms)
        XCTAssertGreaterThan(withMat, without + 10)
    }

    func testSecondTransientWithinTwentyMilliseconds() {
        for seed in UInt64(0)..<20 {
            var scene = SyntheticAudio.Scene()
            scene.seed = seed + 2_000
            scene.snrDb = 40
            scene.echo = (8, -6)
            let second = analyze(SyntheticAudio.render(scene)).first?.features.secondTransient
            XCTAssertEqual(second?.detected, true, "seed \(seed)")
            XCTAssertEqual(second?.delayMs ?? 0, 8, accuracy: 0.5, "seed \(seed)")
        }
    }

    // MARK: 5. Clipping

    func testClippingIsFlaggedAndDoesNotMoveTheOnset() {
        for seed in UInt64(0)..<20 {
            var scene = SyntheticAudio.Scene()
            scene.seed = seed + 4_000
            scene.snrDb = 40
            scene.clipGain = 3
            let x = SyntheticAudio.render(scene)
            XCTAssertTrue(x.contains { abs($0) >= 1 }, "seed \(seed): the scene must clip")
            guard let onset = analyze(x).first else { XCTFail("seed \(seed)"); continue }
            XCTAssertTrue(onset.features.clipped, "seed \(seed)")
            XCTAssertLessThanOrEqual(errorUs(onset, scene.t0, 48_000), 100, "seed \(seed)")

            scene.clipGain = 0.5
            guard let unclipped = analyze(SyntheticAudio.render(scene)).first else { XCTFail("seed \(seed)"); continue }
            XCTAssertFalse(unclipped.features.clipped, "seed \(seed)")
        }
    }

    // MARK: Features sanity

    func testFeatureValuesAreFiniteAndPlausible() throws {
        var scene = SyntheticAudio.Scene()
        scene.seed = 11
        let onset = try XCTUnwrap(analyze(SyntheticAudio.render(scene)).first)
        let f = onset.features
        let peak = try XCTUnwrap(f.peakDbfs)
        XCTAssertTrue((-40.0...0.0).contains(peak), "peak \(peak)")
        XCTAssertGreaterThan(try XCTUnwrap(f.snrDb), 25)
        XCTAssertLessThan(try XCTUnwrap(f.riseTimeMs), 1)
        let centroid = try XCTUnwrap(f.centroidHz0to2ms)
        XCTAssertTrue((2_000.0...12_000.0).contains(centroid), "centroid \(centroid)")
        XCTAssertNotNil(f.centroidHz2to20ms)
        XCTAssertNotNil(f.decayMs20dB)
        XCTAssertGreaterThan(try XCTUnwrap(f.bandHighDb), try XCTUnwrap(f.bandLowDb))
    }
}
