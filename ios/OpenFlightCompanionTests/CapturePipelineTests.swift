import XCTest
@testable import OpenFlightCompanion

/// Ring → processor → payload, the host-time fit and the debug WAV store (design §12.1 #6, #8, #9).
final class HostTimeFitTests: XCTestCase {
    private let fs = 48_000.0
    private let frames: Int64 = 256

    /// Feeds 12 s of 256-frame stamps of a clock running `ppm` fast, with uniform jitter and outliers.
    private func fit(jitterUs: Double, outlierFraction: Double, ppm: Double, seed: UInt64) -> (HostTimeFit, (Double) -> Double) {
        var fit = HostTimeFit(nominalSampleRate: fs)
        var rng = SplitMix64(seed: seed)
        let origin = 3.6e9  // one hour of uptime, µs
        let truth: (Double) -> Double = { origin + $0 / self.fs * 1e6 * (1 + ppm * 1e-6) }
        var sample: Int64 = 0
        while Double(sample) < 12 * fs {
            var host = truth(Double(sample)) + rng.uniform(-jitterUs...jitterUs)
            if rng.uniform() < outlierFraction { host += rng.uniform(2_000...8_000) }
            fit.add(sample: sample, hostMicroseconds: host)
            sample += frames
        }
        fit.refit()
        return (fit, truth)
    }

    func testRealisticJitterOutliersAndRateError() throws {
        let (fit, truth) = fit(jitterUs: 30, outlierFraction: 0.02, ppm: 40, seed: 1)
        let model = try XCTUnwrap(fit.model)
        for sample in [10.5 * fs, 11.9 * fs, 12.5 * fs] {
            let mapped = try XCTUnwrap(fit.hostMicroseconds(at: sample))
            XCTAssertEqual(mapped, truth(sample), accuracy: 10, "sample \(sample)")
        }
        let trueRate = fs / (1 + 40e-6)
        XCTAssertEqual(model.sampleRateEffectiveHz, trueRate, accuracy: trueRate * 1e-6)
        XCTAssertLessThan(model.rmsMicroseconds, 30)
        XCTAssertGreaterThan(model.stampsDropped, 0, "the outliers must be dropped")
    }

    /// The design's ±300 µs per-buffer jitter. A 10 s least-squares window cannot reach 1 ppm there
    /// (σ ≈ 1.4 ppm), so the rate bound is 5 ppm; the mapped time still stays within 25 µs.
    func testHeavyJitterStillMapsWithinTensOfMicroseconds() throws {
        let (fit, truth) = fit(jitterUs: 300, outlierFraction: 0.02, ppm: 40, seed: 2)
        let model = try XCTUnwrap(fit.model)
        let sample = 11.5 * fs
        XCTAssertEqual(try XCTUnwrap(fit.hostMicroseconds(at: sample)), truth(sample), accuracy: 25)
        let trueRate = fs / (1 + 40e-6)
        XCTAssertEqual(model.sampleRateEffectiveHz, trueRate, accuracy: trueRate * 5e-6)
        XCTAssertGreaterThan(model.rmsMicroseconds, 100)
    }

    func testBeforeAFitTheNominalRateIsUsed() throws {
        var fit = HostTimeFit(nominalSampleRate: fs)
        XCTAssertNil(fit.hostMicroseconds(at: 0))
        fit.add(sample: 0, hostMicroseconds: 1_000)
        XCTAssertNil(fit.model)
        XCTAssertEqual(try XCTUnwrap(fit.hostMicroseconds(at: 480)), 11_000, accuracy: 1e-9)
    }

    func testWindowIsBoundedAndARestartResets() {
        var fit = HostTimeFit(nominalSampleRate: fs)
        for index in 0..<10_000 {
            fit.add(sample: Int64(index) * frames, hostMicroseconds: Double(index) * 5_333.333)
        }
        XCTAssertLessThanOrEqual(fit.count, Int(HostTimeFit.windowSeconds * fs) / Int(frames) + 1)
        fit.add(sample: 0, hostMicroseconds: 1)
        XCTAssertEqual(fit.count, 1)
        XCTAssertNil(fit.model)
    }

    func testMachTicksConvertToMicroseconds() {
        let ticks = HostTicks.now()
        let viaClock = Double(HostClock.nowNanoseconds()) / 1_000
        XCTAssertEqual(HostTicks.microseconds(ticks), viaClock, accuracy: 10_000)
    }
}

final class CaptureProcessorTests: XCTestCase {
    private let fs = 48_000.0
    private let frames = 256
    private let hostOriginNs: UInt64 = 7_200_000_000_000  // two hours of uptime
    private let clock = ClockPair(hostMicroseconds: 7_200_000_000, epochMilliseconds: 1_793_000_000_000.25, bracketMicroseconds: 3)

    private func processor(debug: Bool = false, ring: CaptureRing? = nil) throws -> CaptureProcessor {
        let ring = try XCTUnwrap(ring ?? CaptureRing.forCapture(sampleRate: fs))
        let context = CaptureContext(captureId: "test-capture", sampleRate: fs, inputLatencyMicroseconds: 1_250, debugCapture: debug)
        return CaptureProcessor(context: context, ring: ring, clock: { self.clock }, ticksToMicroseconds: { Double($0) / 1_000 })
    }

    /// Writes `x` in 256-frame buffers stamped on an exact 48 kHz host clock; drains every
    /// `drainEvery` buffers (the 10 ms timer) and collects the output.
    @discardableResult
    private func run(_ x: [Double], through processor: CaptureProcessor, sampleTimeOffset: (at: Int, jump: Double)? = nil, drainEvery: Int = 2) -> CaptureProcessor.Output {
        var output = CaptureProcessor.Output()
        let samples = x.map(Float.init)
        var start = 0
        var buffer = 0
        var hardwareOffset = 0.0
        while start < samples.count {
            let end = min(samples.count, start + frames)
            if let jump = sampleTimeOffset, start == jump.at { hardwareOffset += jump.jump }
            let sampleTime = Double(start) + hardwareOffset
            let host = hostOriginNs + UInt64((sampleTime / fs * 1e9).rounded())
            processor.ring.write(Array(samples[start..<end]), sampleTime: sampleTime, hostTime: host,
                                 flags: CaptureRing.sampleTimeValid | CaptureRing.hostTimeValid)
            buffer += 1
            if buffer % drainEvery == 0 {
                let out = processor.drain()
                output.onsets += out.onsets
                output.snippets += out.snippets
            }
            start = end
        }
        let out = processor.drain()
        output.onsets += out.onsets
        output.snippets += out.snippets
        return output
    }

    private func hostUs(atSample sample: Double) -> Double {
        Double(hostOriginNs) / 1_000 + sample / fs * 1e6
    }

    func testOnsetPayloadCarriesTheHostTimeOfTheImpact() throws {
        var scene = SyntheticAudio.Scene()
        scene.seed = 21
        let processor = try processor()
        let output = run(SyntheticAudio.render(scene), through: processor)
        XCTAssertEqual(output.onsets.count, 1)
        let payload = try XCTUnwrap(output.onsets.first)

        guard case .int(let raw)? = payload["onset_host_us_raw"], case .int(let host)? = payload["onset_host_us"],
              case .double(let epoch)? = payload["onset_epoch_ms"] else { return XCTFail("missing host times: \(payload.jsonString())") }
        XCTAssertEqual(Double(raw), hostUs(atSample: scene.t0), accuracy: 100)
        XCTAssertEqual(host, raw - 1_250)
        XCTAssertEqual(epoch, clock.epochMilliseconds(forHostMicroseconds: host))
        XCTAssertEqual(payload["input_latency_us"], .int(1_250))
        guard case .double(let rate)? = payload["sample_rate_eff_hz"], case .double(let rms)? = payload["host_fit_rms_us"] else {
            return XCTFail("missing fit fields")
        }
        XCTAssertEqual(rate, fs, accuracy: 0.01)
        XCTAssertLessThan(rms, 1)
        XCTAssertEqual(payload["capture_id"], .string("test-capture"))
        XCTAssertEqual(payload["seq"], .int(1))
        XCTAssertEqual(payload["snippet_file"], .null)
        XCTAssertEqual(processor.lastSeq, 1)
        XCTAssertEqual(processor.droppedFrames, 0)
    }

    func testPayloadHasEveryDesignKeyWithTheRightType() throws {
        var scene = SyntheticAudio.Scene()
        scene.seed = 22
        scene.precursor = (3, -6)
        let processor = try processor(debug: true)
        let payload = try XCTUnwrap(run(SyntheticAudio.render(scene), through: processor).onsets.first)
        let text = payload.jsonString()
        XCTAssertFalse(text.contains("NaN") || text.contains("Infinity"))

        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(text.utf8)) as? [String: Any])
        let golden: [String: String] = [
            "type": "string", "v": "int", "capture_id": "string", "seq": "int",
            "onset_host_us": "int", "onset_host_us_raw": "int", "input_latency_us": "int",
            "onset_epoch_ms": "number", "onset_sample": "number", "onset_uncertainty_us": "int",
            "sample_rate_hz": "number", "sample_rate_eff_hz": "number", "host_fit_rms_us": "number",
            "clock": "object", "epoch_ms": "int", "tz_offset_min": "int",
            "detection": "object", "features": "object", "fat_cue": "object",
            "snippet_file": "string", "detector_version": "string",
        ]
        XCTAssertEqual(Set(object.keys), Set(golden.keys))
        for (key, kind) in golden { assertType(object[key], kind, key) }
        XCTAssertEqual(object["type"] as? String, "audio_onset")
        XCTAssertEqual(object["detector_version"] as? String, "onset-v1")
        XCTAssertEqual(object["snippet_file"] as? String, "AudioCaptures/test-capture/onset-0001.wav")

        let detection = try XCTUnwrap(object["detection"] as? [String: Any])
        XCTAssertEqual(Set(detection.keys), ["snr_db", "peak_dbfs", "clipped", "rise_time_ms", "crest_db"])
        assertType(detection["clipped"], "bool", "clipped")
        let features = try XCTUnwrap(object["features"] as? [String: Any])
        XCTAssertEqual(Set(features.keys), [
            "band_db", "centroid_hz_0_2ms", "centroid_hz_2_20ms", "decay_ms_20db", "post_low_db", "second_transient",
        ])
        XCTAssertEqual(Set(try XCTUnwrap(features["band_db"] as? [String: Any]).keys), ["low", "mid", "high"])
        XCTAssertEqual(Set(try XCTUnwrap(features["second_transient"] as? [String: Any]).keys), ["detected", "delay_ms", "rel_db"])
        let fat = try XCTUnwrap(object["fat_cue"] as? [String: Any])
        XCTAssertEqual(Set(fat.keys), [
            "detected", "lead_ms", "precursor_host_us", "precursor_snr_db", "precursor_rel_db",
            "precursor_centroid_hz", "pre_low_db_1_10ms",
        ])
        XCTAssertEqual(fat["detected"] as? Bool, true)
        assertType(fat["precursor_host_us"], "int", "precursor_host_us")
        let lead = try XCTUnwrap(fat["lead_ms"] as? Double)
        let host = try XCTUnwrap(object["onset_host_us"] as? Int64)
        let precursor = try XCTUnwrap(fat["precursor_host_us"] as? Int64)
        XCTAssertEqual(Double(host - precursor) / 1_000, lead, accuracy: 0.002)
    }

    private func assertType(_ value: Any?, _ kind: String, _ key: String, file: StaticString = #filePath, line: UInt = #line) {
        guard let value else { return XCTFail("\(key) missing", file: file, line: line) }
        let number = value as? NSNumber
        let isBool = number.map { CFGetTypeID($0) == CFBooleanGetTypeID() } ?? false
        switch kind {
        case "string": XCTAssertTrue(value is String, key, file: file, line: line)
        case "object": XCTAssertTrue(value is [String: Any], key, file: file, line: line)
        case "bool": XCTAssertTrue(isBool, key, file: file, line: line)
        case "int":
            XCTAssertTrue(number != nil && !isBool && number!.doubleValue == number!.doubleValue.rounded(), key, file: file, line: line)
            XCTAssertLessThanOrEqual(abs(number?.doubleValue ?? 0), 9_007_199_254_740_991, key, file: file, line: line)
        case "number": XCTAssertTrue(number != nil && !isBool, key, file: file, line: line)
        default: XCTFail("unknown kind \(kind)", file: file, line: line)
        }
    }

    func testDebugCaptureCutsSnippetsOfTwoHundredMillisecondsEachSide() throws {
        var scene = SyntheticAudio.Scene()
        scene.seed = 23
        let processor = try processor(debug: true)
        let x = SyntheticAudio.render(scene)
        let output = run(x, through: processor)
        let snippet = try XCTUnwrap(output.snippets.first)
        XCTAssertEqual(snippet.seq, 1)
        XCTAssertEqual(snippet.samples.count, 2 * 9_600)
        guard case .double(let onsetSample)? = output.onsets.first?["onset_sample"] else { return XCTFail("no onset") }
        let center = Int(onsetSample.rounded(.down))
        XCTAssertEqual(snippet.samples[9_600 + 100], Float(x[center + 100]))
        XCTAssertEqual(snippet.payload["seq"], .int(1))
    }

    func testOverrunCountsDroppedFramesAndGivesNoFalseOnset() throws {
        // A 1 s ring that is not drained for 3 s: two thirds of the frames are dropped.
        let ring = try XCTUnwrap(CaptureRing(sampleCapacity: 48_000, stampCapacity: 4_096))
        let processor = try processor(ring: ring)
        var rng = SplitMix64(seed: 5)
        let noise = SyntheticAudio.noise(count: 3 * 48_000, rng: &rng)
        XCTAssertEqual(run(noise, through: processor, drainEvery: 100_000).onsets.count, 0)
        XCTAssertGreaterThan(processor.droppedFrames, 90_000)

        // After the gap the detector starts over and still finds the next impact.
        var scene = SyntheticAudio.Scene()
        scene.seed = 24
        let after = SyntheticAudio.render(scene)
        var onsets: [JSONValue] = []
        var start = 0
        let base = 3 * 48_000
        while start < after.count {
            let end = min(after.count, start + frames)
            let sampleTime = Double(base + start)
            ring.write(after[start..<end].map(Float.init), sampleTime: sampleTime,
                       hostTime: hostOriginNs + UInt64(sampleTime / fs * 1e9), flags: CaptureRing.sampleTimeValid | CaptureRing.hostTimeValid)
            onsets += processor.drain().onsets
            start = end
        }
        XCTAssertEqual(onsets.count, 1)
        if case .double(let sample)? = onsets.first?["onset_sample"] {
            XCTAssertEqual(sample, Double(base) + scene.t0, accuracy: 4.8)
        } else {
            XCTFail("onset_sample missing")
        }
    }

    func testHardwareSampleClockJumpIsAGapOnTheHostClock() throws {
        var scene = SyntheticAudio.Scene()
        scene.seed = 25
        scene.t0 = 36_000.5
        let processor = try processor()
        // At 0.5 s the hardware skips 1000 frames: the host clock moves on, the samples do not.
        let output = run(SyntheticAudio.render(scene), through: processor, sampleTimeOffset: (at: 24_064, jump: 1_000))
        XCTAssertEqual(processor.droppedFrames, 1_000)
        let payload = try XCTUnwrap(output.onsets.first)
        guard case .int(let raw)? = payload["onset_host_us_raw"] else { return XCTFail("no host time") }
        XCTAssertEqual(Double(raw), hostUs(atSample: scene.t0 + 1_000), accuracy: 100)
    }

    /// Frames lost to a full stamp ring already move the next record's `produced_start`; the
    /// matching jump of the hardware sample time must not be counted a second time.
    func testLostStampRecordsAreCountedOnce() throws {
        let ring = try XCTUnwrap(CaptureRing(sampleCapacity: 48_000 * 8, stampCapacity: 4))
        let processor = try processor(ring: ring)
        var scene = SyntheticAudio.Scene()
        scene.seed = 31
        scene.duration = 2
        scene.t0 = 1.5 * fs + 0.37
        let x = SyntheticAudio.render(scene).map(Float.init)
        var onsets: [JSONValue] = []
        var start = 0
        var buffer = 0
        while start < x.count {
            let end = min(x.count, start + frames)
            let sampleTime = Double(start)
            ring.write(Array(x[start..<end]), sampleTime: sampleTime, hostTime: hostOriginNs + UInt64((sampleTime / fs * 1e9).rounded()),
                       flags: CaptureRing.sampleTimeValid | CaptureRing.hostTimeValid)
            buffer += 1
            // Not drained for buffers 10...15: the 4-record stamp ring overflows.
            if buffer % 2 == 0, !(10...15).contains(buffer) { onsets += processor.drain().onsets }
            start = end
        }
        onsets += processor.drain().onsets
        XCTAssertGreaterThan(ring.droppedFrames, 0)
        XCTAssertEqual(processor.droppedFrames, Int64(ring.droppedFrames), "lost frames counted twice")
        XCTAssertEqual(onsets.count, 1)
        guard case .double(let sample)? = onsets.first?["onset_sample"], case .int(let raw)? = onsets.first?["onset_host_us_raw"] else {
            return XCTFail("onset fields missing")
        }
        XCTAssertEqual(sample, scene.t0, accuracy: 4.8)
        XCTAssertEqual(Double(raw), hostUs(atSample: scene.t0), accuracy: 100)
    }

    /// An onset soon after a (re)start is mapped with a fit that includes the stamps up to the
    /// onset, not with the first 0.1 s of them (design §11: the timing budget is 0.35 ms, the
    /// target 0.1 ms), under the design's ±300 µs per-buffer jitter and a 20 ppm rate error.
    func testOnsetsInTheFirstSecondAfterAStartAreMappedWithinATenthOfAMillisecond() throws {
        let truthUs: (Double) -> Double = { sample in Double(self.hostOriginNs) / 1_000 + sample / self.fs * 1e6 * (1 + 20e-6) }
        for at in [0.3, 0.5, 0.8, 1.05] {
            for seed in UInt64(0)..<10 {
                var scene = SyntheticAudio.Scene()
                scene.seed = 400 + seed
                scene.duration = at + 0.2
                scene.t0 = at * fs + 0.37
                let x = SyntheticAudio.render(scene).map(Float.init)
                let processor = try processor()
                var rng = SplitMix64(seed: 900 + seed)
                var onsets: [JSONValue] = []
                var start = 0
                while start < x.count {
                    let end = min(x.count, start + 240)  // 5 ms IO buffers
                    let hostNs = (truthUs(Double(start)) + rng.uniform(-300...300)) * 1_000
                    processor.ring.write(Array(x[start..<end]), sampleTime: Double(start), hostTime: UInt64(hostNs.rounded()),
                                         flags: CaptureRing.sampleTimeValid | CaptureRing.hostTimeValid)
                    onsets += processor.drain().onsets
                    start = end
                }
                XCTAssertEqual(onsets.count, 1, "onset at \(at) s seed \(seed)")
                guard case .int(let raw)? = onsets.first?["onset_host_us_raw"] else { continue }
                XCTAssertEqual(Double(raw), truthUs(scene.t0), accuracy: 100, "onset at \(at) s seed \(seed)")
            }
        }
    }

    /// 120 s of play with an impact every 0.4 s: all 300 are reported and nothing grows.
    func testLongRunMemoryStaysBounded() throws {
        let processor = try processor(debug: true)
        let historyCapacity = processor.analyzer.historyCapacity
        var rng = SplitMix64(seed: 9)
        let block = 48_000 * 4
        var onsets = 0, snippets = 0
        var maxPending = 0
        var sampleBase = 0
        for _ in 0..<30 {
            var x = SyntheticAudio.noise(count: block, rng: &rng)
            for k in 0..<10 {
                let t0 = Double(k) * 0.4 * fs + 4_800 + rng.uniform(0...1)
                let shape = SyntheticAudio.impactShape(count: block, t0: t0, sampleRate: fs, rng: &rng)
                SyntheticAudio.add(&x, SyntheticAudio.scaled(shape, t0: t0, snrDb: 40, noiseRms: 1e-3, sampleRate: fs))
            }
            var start = 0
            while start < block {
                let sampleTime = Double(sampleBase + start)
                processor.ring.write(x[start..<(start + frames)].map(Float.init), sampleTime: sampleTime,
                                     hostTime: hostOriginNs + UInt64(sampleTime / fs * 1e9),
                                     flags: CaptureRing.sampleTimeValid | CaptureRing.hostTimeValid)
                if (start / frames) % 2 == 1 {
                    let out = processor.drain()
                    onsets += out.onsets.count
                    snippets += out.snippets.count
                    maxPending = max(maxPending, processor.analyzer.pendingCount)
                }
                start += frames
            }
            sampleBase += block
        }
        let out = processor.drain()
        onsets += out.onsets.count
        snippets += out.snippets.count
        XCTAssertEqual(onsets, 300)
        XCTAssertGreaterThanOrEqual(snippets, 299, "the last snippet may still wait for its 200 ms")
        XCTAssertLessThanOrEqual(maxPending, 2)
        XCTAssertEqual(processor.analyzer.historyCapacity, historyCapacity)
        XCTAssertLessThanOrEqual(processor.fit.count, Int(HostTimeFit.windowSeconds * fs) / frames + 1)
        XCTAssertLessThanOrEqual(processor.awaitingSnippetCount, 8)
        XCTAssertEqual(processor.droppedFrames, 0)
    }

    /// Pipeline cost (ring drain, detector, refinement, features, payloads) against the design's
    /// budget of 2 % of one core (§9.9), measured on the Simulator host.
    func testProcessingCostIsSmall() throws {
        let processor = try processor()
        var rng = SplitMix64(seed: 13)
        var x = SyntheticAudio.noise(count: 48_000 * 10, rng: &rng)
        for k in 0..<20 {
            let t0 = Double(k) * 0.5 * fs + 9_000.3
            let shape = SyntheticAudio.impactShape(count: x.count, t0: t0, sampleRate: fs, rng: &rng)
            SyntheticAudio.add(&x, SyntheticAudio.scaled(shape, t0: t0, snrDb: 40, noiseRms: 1e-3, sampleRate: fs))
        }
        let started = Date()
        let output = run(x, through: processor)
        let elapsed = Date().timeIntervalSince(started)
        XCTAssertEqual(output.onsets.count, 20)
        let percent = elapsed / 10 * 100
        print("[cpu] 10 s of 48 kHz audio with 20 impacts in \(String(format: "%.3f", elapsed)) s = \(String(format: "%.2f", percent)) % of one core")
        XCTAssertLessThan(percent, 2)
    }
}

final class CaptureStoreTests: XCTestCase {
    private var root: URL!

    override func setUpWithError() throws {
        root = FileManager.default.temporaryDirectory.appendingPathComponent("CaptureStoreTests-\(UUID().uuidString)", isDirectory: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: root)
    }

    func testWAVRoundTripIsBitExact() throws {
        let samples: [Float] = [0, -0.0, 1, -1, 0.5, -0.25, .leastNonzeroMagnitude, .greatestFiniteMagnitude, 1e-7, 0.999_999]
        let data = WAVWriter.encode(samples, sampleRate: 48_000)
        XCTAssertEqual(String(decoding: data[0..<4], as: UTF8.self), "RIFF")
        XCTAssertEqual(String(decoding: data[8..<12], as: UTF8.self), "WAVE")
        XCTAssertEqual(data.count, 12 + 26 + 12 + 8 + samples.count * 4)
        let decoded = try XCTUnwrap(WAVWriter.decode(data))
        XCTAssertEqual(decoded.formatTag, 3)
        XCTAssertEqual(decoded.channels, 1)
        XCTAssertEqual(decoded.sampleRate, 48_000)
        XCTAssertEqual(decoded.bitsPerSample, 32)
        XCTAssertEqual(decoded.samples.map(\.bitPattern), samples.map(\.bitPattern))
        XCTAssertNil(WAVWriter.decode(data.prefix(data.count - 3)))
    }

    private func write(_ store: CaptureStore, capture: String, seq: Int) throws {
        try store.writeSnippet(
            captureId: capture, seq: seq, samples: [Float](repeating: 0.1, count: 1_000), sampleRate: 48_000,
            payload: .object(["seq": .int(Int64(seq))])
        )
        // Distinct modification dates, so "oldest" is well defined.
        let wav = root.appendingPathComponent(capture).appendingPathComponent(CaptureStore.snippetName(seq: seq) + ".wav")
        try FileManager.default.setAttributes([.modificationDate: Date(timeIntervalSince1970: TimeInterval(1_000 + seq + (capture == "b" ? 100 : 0)))], ofItemAtPath: wav.path)
    }

    func testRetentionKeepsTheNewestFilesAndDropsEmptyCaptures() throws {
        let store = CaptureStore(root: root, maxFiles: 3)
        store.activeCaptureId = "b"
        try write(store, capture: "a", seq: 1)
        try write(store, capture: "a", seq: 2)
        try write(store, capture: "b", seq: 1)
        try write(store, capture: "b", seq: 2)
        try write(store, capture: "b", seq: 3)
        store.enforceRetention()
        XCTAssertEqual(store.summary().files, 3)
        let fm = FileManager.default
        XCTAssertFalse(fm.fileExists(atPath: root.appendingPathComponent("a").path), "capture a lost all snippets")
        for seq in 1...3 {
            let base = root.appendingPathComponent("b").appendingPathComponent(CaptureStore.snippetName(seq: seq))
            XCTAssertTrue(fm.fileExists(atPath: base.appendingPathExtension("wav").path))
            XCTAssertTrue(fm.fileExists(atPath: base.appendingPathExtension("json").path))
        }
        XCTAssertEqual(CaptureStore.snippetPath(captureId: "b", seq: 3), "AudioCaptures/b/onset-0003.wav")
    }

    func testByteCapAlsoApplies() throws {
        let store = CaptureStore(root: root, maxFiles: 100, maxBytes: 10_000)
        for seq in 1...5 { try write(store, capture: "c", seq: seq) }
        store.enforceRetention()
        let summary = store.summary()
        XCTAssertLessThanOrEqual(summary.bytes, 10_000)
        XCTAssertEqual(summary.files, 2)
    }

    func testCaptureInfoAndExportZip() throws {
        let store = CaptureStore(root: root)
        XCTAssertNil(try store.exportZip(to: root.deletingLastPathComponent()))
        try store.writeCaptureInfo(captureId: "d", info: .object(["detector_version": .string("onset-v1")]))
        try write(store, capture: "d", seq: 1)
        let info = try Data(contentsOf: root.appendingPathComponent("d/capture.json"))
        XCTAssertEqual(String(decoding: info, as: UTF8.self), #"{"detector_version":"onset-v1"}"#)
        let zip = try XCTUnwrap(try store.exportZip(to: root.deletingLastPathComponent()))
        defer { try? FileManager.default.removeItem(at: zip) }
        let data = try Data(contentsOf: zip)
        XCTAssertGreaterThan(data.count, 100)
        XCTAssertEqual(Array(data.prefix(2)), Array("PK".utf8))
    }
}

@MainActor
final class AudioSettingsTests: XCTestCase {
    func testDefaultsAndPersistence() throws {
        let suite = "AudioSettingsTests-\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let settings = AppSettings(defaults: defaults)
        XCTAssertTrue(settings.audioCaptureEnabled, "capture starts automatically by default")
        XCTAssertFalse(settings.debugCaptureEnabled, "debug WAVs are off by default")
        settings.audioCaptureEnabled = false
        settings.debugCaptureEnabled = true
        let reloaded = AppSettings(defaults: defaults)
        XCTAssertFalse(reloaded.audioCaptureEnabled)
        XCTAssertTrue(reloaded.debugCaptureEnabled)
    }
}
