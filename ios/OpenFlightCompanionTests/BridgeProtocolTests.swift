import XCTest
@testable import OpenFlightCompanion

final class BridgeProtocolTests: XCTestCase {
    // MARK: Request parsing

    func testParsesEveryKnownCommand() throws {
        for command in BridgeCommand.allCases {
            let request = try BridgeRequest.parse(["v": 1, "cmd": command.rawValue, "args": [:]] as [String: Any]).get()
            XCTAssertEqual(request.command, command)
        }
    }

    func testMissingOrNullArgsBecomeEmpty() throws {
        let missing = try BridgeRequest.parse(["v": 1, "cmd": "getSensors"] as [String: Any]).get()
        XCTAssertTrue(missing.arguments.isEmpty)
        let null = try BridgeRequest.parse(["v": 1, "cmd": "getSensors", "args": NSNull()] as [String: Any]).get()
        XCTAssertTrue(null.arguments.isEmpty)
    }

    func testRejectsMalformedBodies() {
        let bodies: [Any?] = [
            nil,
            "getSensors",
            [1, 2],
            ["cmd": "getSensors"],                              // no version
            ["v": 2, "cmd": "getSensors"],                      // wrong version
            ["v": true, "cmd": "getSensors"],                   // bool is not a version
            ["v": "1", "cmd": "getSensors"],
            ["v": 1],                                           // no command
            ["v": 1, "cmd": 7],
            ["v": 1, "cmd": "getSensors", "args": [1]],         // args not an object
            ["v": 1, "cmd": "getSensors", "args": "x"],
        ]
        for body in bodies {
            guard case .failure(let error) = BridgeRequest.parse(body) else {
                return XCTFail("accepted \(String(describing: body))")
            }
            XCTAssertEqual(error.code, .badRequest, "\(String(describing: body))")
        }
    }

    func testUnknownCommandIsBadRequest() {
        guard case .failure(let error) = BridgeRequest.parse(["v": 1, "cmd": "launchRocket"] as [String: Any]) else {
            return XCTFail("unknown command accepted")
        }
        XCTAssertEqual(error.code, .badRequest)
        XCTAssertTrue(error.message.contains("launchRocket"))
    }

    // MARK: Arguments

    func testFetchWeatherArguments() throws {
        let args = try FetchWeatherArguments.parse(["latitude": 48.13743, "longitude": 11.57549]).get()
        XCTAssertEqual(args.latitude, 48.13743)
        XCTAssertEqual(args.longitude, 11.57549)
        XCTAssertEqual(args.timeoutMs, FetchWeatherArguments.defaultTimeoutMs)

        let withTimeout = try FetchWeatherArguments.parse(["latitude": 0, "longitude": 0, "timeout_ms": 5_000]).get()
        XCTAssertEqual(withTimeout.timeoutMs, 5_000)
    }

    func testInvalidCoordinatesAreBadRequest() {
        let cases: [[String: Any]] = [
            [:],
            ["latitude": 48.1],
            ["longitude": 11.5],
            ["latitude": 90.0001, "longitude": 0],
            ["latitude": -91, "longitude": 0],
            ["latitude": 0, "longitude": 180.5],
            ["latitude": "48.1", "longitude": 11.5],
            ["latitude": true, "longitude": 11.5],
            ["latitude": NSNull(), "longitude": 11.5],
            ["latitude": Double.nan, "longitude": 11.5],
            ["latitude": Double.infinity, "longitude": 11.5],
            ["latitude": 48, "longitude": 11, "timeout_ms": 10],          // below the 1 s minimum
            ["latitude": 48, "longitude": 11, "timeout_ms": false],
        ]
        for args in cases {
            guard case .failure(let error) = FetchWeatherArguments.parse(args) else {
                return XCTFail("accepted \(args)")
            }
            XCTAssertEqual(error.code, .badRequest, "\(args)")
        }
    }

    func testGetSensorsArguments() throws {
        let empty = try GetSensorsArguments.parse([:]).get()
        XCTAssertNil(empty.locationMaxAgeS)
        XCTAssertEqual(empty.timeoutMs, GetSensorsArguments.defaultTimeoutMs)
        let full = try GetSensorsArguments.parse(["location_max_age_s": 600, "timeout_ms": 2_000]).get()
        XCTAssertEqual(full.locationMaxAgeS, 600)
        XCTAssertEqual(full.timeoutMs, 2_000)
        XCTAssertNil(try? GetSensorsArguments.parse(["location_max_age_s": -1]).get())
        XCTAssertNil(try? GetSensorsArguments.parse(["location_max_age_s": true]).get())
    }

    func testSetKeepAwakeNeedsABoolean() throws {
        XCTAssertEqual(try SetKeepAwakeArguments.parse(["on": true]).get().on, true)
        XCTAssertEqual(try SetKeepAwakeArguments.parse(["on": NSNumber(value: false)]).get().on, false)
        XCTAssertNil(try? SetKeepAwakeArguments.parse(["on": 1]).get())
        XCTAssertNil(try? SetKeepAwakeArguments.parse([:]).get())
    }

    // MARK: Envelopes and JSON safety

    func testAudioOnsetsArguments() throws {
        XCTAssertEqual(try AudioOnsetsArguments.parse(["capture_id": "c1", "after_seq": 0]).get(), AudioOnsetsArguments(captureId: "c1", afterSeq: 0))
        let bad: [[String: Any]] = [
            [:],
            ["capture_id": "c1"],
            ["capture_id": "", "after_seq": 1],
            ["capture_id": 5, "after_seq": 1],
            ["capture_id": "c1", "after_seq": -1],
            ["capture_id": "c1", "after_seq": 1.5],
            ["capture_id": "c1", "after_seq": true],
            ["capture_id": String(repeating: "x", count: 65), "after_seq": 1],
        ]
        for args in bad {
            XCTAssertThrowsError(try AudioOnsetsArguments.parse(args).get(), "\(args)") { error in
                XCTAssertEqual((error as? BridgeError)?.code, .badRequest)
            }
        }
    }

    func testAudioStartArguments() throws {
        XCTAssertEqual(try AudioStartArguments.parse([:]).get(), AudioStartArguments(debugCapture: nil))
        XCTAssertEqual(try AudioStartArguments.parse(["debug_capture": NSNull()]).get(), AudioStartArguments(debugCapture: nil))
        XCTAssertEqual(try AudioStartArguments.parse(["debug_capture": false]).get(), AudioStartArguments(debugCapture: false))
        XCTAssertThrowsError(try AudioStartArguments.parse(["debug_capture": "yes"]).get())
    }

    func testMotionSamplesArguments() throws {
        XCTAssertEqual(try MotionSamplesArguments.parse(["after_seq": 0]).get(), MotionSamplesArguments(afterSeq: 0))
        XCTAssertEqual(try MotionSamplesArguments.parse(["after_seq": 42]).get(), MotionSamplesArguments(afterSeq: 42))
        for args: [String: Any] in [[:], ["after_seq": -1], ["after_seq": 1.5], ["after_seq": "3"], ["after_seq": true]] {
            XCTAssertThrowsError(try MotionSamplesArguments.parse(args).get(), "\(args)") { error in
                XCTAssertEqual((error as? BridgeError)?.code, .badRequest)
            }
        }
    }

    /// CoreMotion reads z = −1 g face up; the page expects W3C `accelerationIncludingGravity` (+9.81 m/s²).
    func testSpeechSpeakArguments() throws {
        let plain = try SpeechSpeakArguments.parse(["text": "Carry 151 Meter", "lang": "de-DE"]).get()
        XCTAssertEqual(plain, SpeechSpeakArguments(text: "Carry 151 Meter", lang: "de-DE", interrupt: false))
        let test = try SpeechSpeakArguments.parse(["text": "  Carry 165 yards\n", "lang": "en-US", "interrupt": true]).get()
        XCTAssertEqual(test, SpeechSpeakArguments(text: "Carry 165 yards", lang: "en-US", interrupt: true))
        XCTAssertEqual(try SpeechSpeakArguments.parse(["text": "Spin 2800", "lang": "es", "interrupt": NSNull()]).get().interrupt, false)
        XCTAssertEqual(try SpeechSpeakArguments.parse(["text": "x", "lang": "zh-Hans-CN"]).get().lang, "zh-Hans-CN")

        let bad: [[String: Any]] = [
            [:],
            ["lang": "de-DE"],
            ["text": 7, "lang": "de-DE"],
            ["text": "   ", "lang": "de-DE"],
            ["text": String(repeating: "a", count: SpeechSpeakArguments.maxTextLength + 1), "lang": "de-DE"],
            ["text": "Carry", "lang": NSNull()],
            ["text": "Carry", "lang": ""],
            ["text": "Carry", "lang": "deutsch"],
            ["text": "Carry", "lang": "de_DE"],
            ["text": "Carry", "lang": "de-DE;x"],
            ["text": "Carry", "lang": "de-DE", "interrupt": 1],
            ["text": "Carry", "lang": "de-DE", "interrupt": "yes"],
        ]
        for args in bad {
            guard case .failure(let error) = SpeechSpeakArguments.parse(args) else {
                return XCTFail("accepted \(args)")
            }
            XCTAssertEqual(error.code, .badRequest)
        }
        XCTAssertNoThrow(try SpeechSpeakArguments.parse([
            "text": String(repeating: "a", count: SpeechSpeakArguments.maxTextLength), "lang": "de-DE",
        ]).get())
    }

    func testMotionBufferConvertsToW3CAndCountsOn() {
        var buffer = MotionSampleBuffer(capacity: 3)
        buffer.append(timestamp: 100.5, x: 0, y: -0.02, z: -1)
        let first = buffer.samples[0]
        XCTAssertEqual(first.seq, 1)
        XCTAssertEqual(first.tMs, 100_500, accuracy: 1e-9)
        XCTAssertEqual(first.gravity.y, 0.02 * MotionSampleBuffer.standardGravity, accuracy: 1e-12)
        XCTAssertEqual(first.gravity.z, MotionSampleBuffer.standardGravity, accuracy: 1e-12)
        // Top end raised: W3C pitch atan2(y, z) is positive.
        XCTAssertGreaterThan(atan2(first.gravity.y, first.gravity.z), 0)

        buffer.append(timestamp: .nan, x: 0, y: 0, z: -1)
        XCTAssertEqual(buffer.samples.count, 1, "a non-finite reading is dropped")
        for step in 1...4 { buffer.append(timestamp: 100.5 + Double(step) * 0.02, x: 0, y: 0, z: -1) }
        XCTAssertEqual(buffer.samples.map(\.seq), [3, 4, 5], "keeps the newest `capacity` samples")

        let json = buffer.json(after: 3, running: true)
        XCTAssertEqual(json["running"], .bool(true))
        guard case .array(let newer)? = json["samples"] else { return XCTFail("samples must be an array") }
        XCTAssertEqual(newer.map { $0["seq"] }, [.int(4), .int(5)])
        guard case .array(let gravity)? = newer[0]["gravity_mps2"] else { return XCTFail("gravity_mps2 must be an array") }
        XCTAssertEqual(gravity.count, 3)

        buffer.clear()
        buffer.append(timestamp: 200, x: 0, y: 0, z: -1)
        XCTAssertEqual(buffer.samples.map(\.seq), [6], "seq keeps counting after a clear")
    }

    func testAudioStatusGoldenKeysAndTypes() throws {
        var status = AudioStatus()
        status.state = .running
        status.permission = .granted
        status.captureId = "c1"
        status.restarted = true
        status.startedEpochMs = 1_793_000_000_000.5
        status.session = .init(sampleRateHz: 48_000, ioBufferMs: 5.33, inputLatencyMs: 1.2, inputPort: "MicrophoneBuiltIn",
                               inputDataSource: nil, category: "AVAudioSessionCategoryPlayAndRecord", mode: "AVAudioSessionModeMeasurement")
        status.onsets = 3
        status.lastSeq = 3
        status.droppedFrames = 0
        status.deviceModel = "iPhone18,2"
        status.clock = ClockPair(hostMicroseconds: 5_000_000, epochMilliseconds: 1_793_000_000_123.456, bracketMicroseconds: 2)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(status.json.jsonString().utf8)) as? [String: Any])
        XCTAssertEqual(Set(object.keys), [
            "type", "v", "state", "permission", "capture_id", "restarted", "started_epoch_ms", "sample_rate_hz",
            "io_buffer_ms", "input_latency_ms", "input_port", "input_data_source", "session_category", "session_mode",
            "debug_capture", "onsets", "last_seq", "dropped_frames", "thresholds", "detector_version", "device_model",
            "error", "epoch_ms", "tz_offset_min", "clock",
        ])
        XCTAssertEqual(object["type"] as? String, "audio_status")
        XCTAssertEqual(object["state"] as? String, "running")
        XCTAssertEqual(object["restarted"] as? Bool, true)
        XCTAssertTrue(object["input_data_source"] is NSNull)
        XCTAssertTrue(object["error"] is NSNull)
        XCTAssertEqual(object["epoch_ms"] as? Int64, 1_793_000_000_123)
        let thresholds = try XCTUnwrap(object["thresholds"] as? [String: Any])
        XCTAssertEqual(thresholds["trigger_over_floor_db"] as? Double, 18)
        XCTAssertEqual(thresholds["refractory_ms"] as? Double, 150)
        XCTAssertEqual(object["detector_version"] as? String, "onset-v1")

        status.state = .error
        status.permission = .denied
        status.error = BridgeError(.permissionDenied, "denied")
        XCTAssertEqual(status.json["error"], .object(["code": .string("permission_denied"), "message": .string("denied")]))
        XCTAssertEqual(status.summary, SensorSnapshot.Audio(state: "error", captureId: "c1", lastSeq: 3))
    }

    func testAudioIndicatorFollowsTheState() {
        var status = AudioStatus()
        XCTAssertNil(AudioIndicator(status: status))
        status.state = .running
        XCTAssertEqual(AudioIndicator(status: status)?.symbol, "mic.fill")
        status.state = .interrupted
        XCTAssertEqual(AudioIndicator(status: status)?.symbol, "mic.slash")
        status.state = .error
        status.permission = .denied
        XCTAssertEqual(AudioIndicator(status: status)?.label, "Kein Mikrofonzugriff")
        XCTAssertEqual(AudioIndicator.describe(status, enabled: false), "Aus.")
    }

    func testEnvelopes() {
        XCTAssertEqual(
            BridgeEnvelope.success(.object(["a": .int(1)])).jsonString(),
            #"{"ok":true,"result":{"a":1}}"#
        )
        XCTAssertEqual(
            BridgeEnvelope.failure(BridgeError(.httpError, "Open-Meteo HTTP 500")).jsonString(),
            #"{"error":{"code":"http_error","message":"Open-Meteo HTTP 500"},"ok":false}"#
        )
    }

    func testErrorCodesMatchTheDesign() {
        XCTAssertEqual(
            Set(BridgeErrorCode.allCases.map(\.rawValue)),
            ["bad_request", "forbidden", "permission_denied", "unavailable", "timeout", "network",
             "http_error", "parse_error", "busy", "internal"]
        )
    }

    func testNonFiniteNumbersBecomeNull() {
        XCTAssertEqual(JSONValue.number(.nan), .null)
        XCTAssertEqual(JSONValue.number(.infinity), .null)
        XCTAssertEqual(JSONValue.number(-.infinity), .null)
        XCTAssertEqual(JSONValue.number(nil), .null)
        XCTAssertEqual(JSONValue.number(1.5), .double(1.5))
        // Even a hand-built .double(NaN) never reaches JSON as NaN.
        XCTAssertEqual(JSONValue.array([.double(.nan)]).jsonString(), "[null]")
    }

    func testIntegersStayInTheExactJSRange() {
        XCTAssertEqual(JSONValue.integer(JSONValue.maxSafeInteger), .int(9_007_199_254_740_991))
        XCTAssertEqual(JSONValue.integer(JSONValue.maxSafeInteger + 1), .null)
        XCTAssertEqual(JSONValue.integer(-JSONValue.maxSafeInteger - 1), .null)
        XCTAssertEqual(JSONValue.integer(nil as Int64?), .null)
    }

    func testFoundationObjectRoundTrip() throws {
        let value: JSONValue = .object([
            "n": .null, "b": .bool(false), "i": .int(42), "d": .double(0.25), "s": .string("ä/€"),
            "a": .array([.int(1), .string("x")]),
        ])
        let data = try JSONSerialization.data(withJSONObject: value.foundationObject, options: [.sortedKeys])
        XCTAssertEqual(
            String(decoding: data, as: UTF8.self),
            #"{"a":[1,"x"],"b":false,"d":0.25,"i":42,"n":null,"s":"ä\/€"}"#
        )
        // Booleans stay booleans, not 0/1.
        let object = value.foundationObject as? [String: Any]
        XCTAssertTrue(FoundationReader.isBoolean(object?["b"] as Any))
        XCTAssertFalse(FoundationReader.isBoolean(object?["i"] as Any))
    }

    // MARK: Config / shim

    func testConfigJSON() {
        let config = BridgeConfig(appVersion: "0.1.0", capabilities: .init(
            barometer: true, absoluteAltitude: false, location: true, weather: true,
            audio: false, keepAwake: true, debugCapture: false, motion: true, speech: true
        ))
        XCTAssertEqual(
            config.json.jsonString(),
            #"{"app_version":"0.1.0","capabilities":{"absolute_altitude":false,"audio":false,"barometer":true,"debug_capture":false,"keep_awake":true,"location":true,"motion":true,"speech":true,"weather":true},"event":"openflight-native","handler":"openflight","platform":"ios","version":1}"#
        )
    }

    func testShimIsTemplated() throws {
        let config = BridgeConfig(appVersion: "9.9.9", capabilities: .init(
            barometer: false, absoluteAltitude: false, location: true, weather: true,
            audio: false, keepAwake: true, debugCapture: false, motion: true, speech: true
        ))
        let source = try XCTUnwrap(NativeBridge.shimSource(config: config, bundle: Bundle(for: NativeBridge.self)))
        XCTAssertFalse(source.contains("__OPENFLIGHT_NATIVE_CONFIG__"))
        XCTAssertTrue(source.contains(#""app_version":"9.9.9""#))
        XCTAssertTrue(source.contains("openflightNative"))
    }

    func testEventScript() {
        XCTAssertEqual(
            NativeBridge.eventScript(.object(["type": .string("audio_status"), "v": .int(1)])),
            #"window.dispatchEvent(new CustomEvent("openflight-native", { detail: {"type":"audio_status","v":1} }));"#
        )
    }

    // MARK: Origin policy

    func testOriginPolicy() throws {
        let base = try XCTUnwrap(URL(string: "http://openflight.local:8080/"))
        XCTAssertTrue(OriginPolicy.matches(scheme: "http", host: "openflight.local", port: 8080, base: base))
        XCTAssertTrue(OriginPolicy.matches(scheme: "HTTP", host: "OpenFlight.LOCAL", port: 8080, base: base))
        XCTAssertFalse(OriginPolicy.matches(scheme: "https", host: "openflight.local", port: 8080, base: base))
        XCTAssertFalse(OriginPolicy.matches(scheme: "http", host: "openflight.local", port: 8081, base: base))
        XCTAssertFalse(OriginPolicy.matches(scheme: "http", host: "openflight.local", port: 0, base: base))
        XCTAssertFalse(OriginPolicy.matches(scheme: "http", host: "evil.example", port: 8080, base: base))
        XCTAssertFalse(OriginPolicy.matches(scheme: "http", host: "openflight.local.evil.example", port: 8080, base: base))

        // Default ports: WKSecurityOrigin reports 0 for them.
        let plain = try XCTUnwrap(URL(string: "http://10.42.0.1/"))
        XCTAssertTrue(OriginPolicy.matches(scheme: "http", host: "10.42.0.1", port: 0, base: plain))
        XCTAssertTrue(OriginPolicy.matches(scheme: "http", host: "10.42.0.1", port: 80, base: plain))
        XCTAssertTrue(OriginPolicy.matches(url: try XCTUnwrap(URL(string: "http://10.42.0.1/pair#code=1")), base: plain))
        XCTAssertFalse(OriginPolicy.matches(url: try XCTUnwrap(URL(string: "about:blank")), base: plain))
    }

    // MARK: Router (no WebKit)

    @MainActor
    func testRouterRepliesWithEnvelopes() async throws {
        let services = FakeServices()
        let bridge = NativeBridge(services: services, config: FakeServices.config, baseURL: URL(string: "http://openflight.local:8080/")!)

        let forbidden = await bridge.handle(body: ["v": 1, "cmd": "getSensors"], originAllowed: false)
        XCTAssertEqual(forbidden["error"]?["code"], .string("forbidden"))
        XCTAssertEqual(services.sensorCalls, 0, "a forbidden origin must not reach the sensors")

        let sensors = await bridge.handle(body: ["v": 1, "cmd": "getSensors", "args": ["location_max_age_s": 60]], originAllowed: true)
        XCTAssertEqual(sensors["ok"], .bool(true))
        XCTAssertEqual(services.lastSensorArgs?.locationMaxAgeS, 60)

        let weather = await bridge.handle(body: ["v": 1, "cmd": "fetchWeather", "args": ["latitude": 48.1, "longitude": 11.5]], originAllowed: true)
        XCTAssertEqual(weather["error"]?["code"], .string("network"))

        let badWeather = await bridge.handle(body: ["v": 1, "cmd": "fetchWeather", "args": ["latitude": 200, "longitude": 11.5]], originAllowed: true)
        XCTAssertEqual(badWeather["error"]?["code"], .string("bad_request"))
        XCTAssertEqual(services.weatherCalls, 1)

        let awake = await bridge.handle(body: ["v": 1, "cmd": "setKeepAwake", "args": ["on": false]], originAllowed: true)
        XCTAssertEqual(awake["result"]?["keep_awake"], .bool(false))

        let audio = await bridge.handle(body: ["v": 1, "cmd": "audio.start"], originAllowed: true)
        XCTAssertEqual(audio["error"]?["code"], .string("unavailable"))
        XCTAssertEqual(services.lastAudioStart, AudioStartArguments(debugCapture: nil))

        let debugStart = await bridge.handle(body: ["v": 1, "cmd": "audio.start", "args": ["debug_capture": true]], originAllowed: true)
        XCTAssertEqual(debugStart["ok"], .bool(false))
        XCTAssertEqual(services.lastAudioStart, AudioStartArguments(debugCapture: true))

        let badStart = await bridge.handle(body: ["v": 1, "cmd": "audio.start", "args": ["debug_capture": 1]], originAllowed: true)
        XCTAssertEqual(badStart["error"]?["code"], .string("bad_request"))

        let stop = await bridge.handle(body: ["v": 1, "cmd": "audio.stop"], originAllowed: true)
        XCTAssertEqual(stop["result"]?["state"], .string("idle"))
        let status = await bridge.handle(body: ["v": 1, "cmd": "audio.status"], originAllowed: true)
        XCTAssertEqual(status["result"]?["type"], .string("audio_status"))

        let onsets = await bridge.handle(body: ["v": 1, "cmd": "audio.onsets", "args": ["capture_id": "abc", "after_seq": 4]], originAllowed: true)
        XCTAssertEqual(onsets["result"], .array([.object(["type": .string("audio_onset"), "seq": .int(5)])]))
        XCTAssertEqual(services.lastAudioOnsets, AudioOnsetsArguments(captureId: "abc", afterSeq: 4))

        let export = await bridge.handle(body: ["v": 1, "cmd": "audio.exportCaptures"], originAllowed: true)
        XCTAssertEqual(export["result"]?["files"], .int(0))
        XCTAssertEqual(services.audioCalls, ["start", "start", "stop", "status", "onsets", "export"])

        let forbiddenAudio = await bridge.handle(body: ["v": 1, "cmd": "audio.start"], originAllowed: false)
        XCTAssertEqual(forbiddenAudio["error"]?["code"], .string("forbidden"))
        XCTAssertEqual(services.audioCalls.count, 6, "a forbidden origin must not reach the microphone")

        let motionStart = await bridge.handle(body: ["v": 1, "cmd": "motion.start"], originAllowed: true)
        XCTAssertEqual(motionStart["result"]?["running"], .bool(true))
        let motionSamples = await bridge.handle(body: ["v": 1, "cmd": "motion.samples", "args": ["after_seq": 7]], originAllowed: true)
        XCTAssertEqual(motionSamples["ok"], .bool(true))
        XCTAssertEqual(services.lastMotionSamples, MotionSamplesArguments(afterSeq: 7))
        let badSamples = await bridge.handle(body: ["v": 1, "cmd": "motion.samples"], originAllowed: true)
        XCTAssertEqual(badSamples["error"]?["code"], .string("bad_request"))
        let motionStop = await bridge.handle(body: ["v": 1, "cmd": "motion.stop"], originAllowed: true)
        XCTAssertEqual(motionStop["result"]?["running"], .bool(false))
        let forbiddenMotion = await bridge.handle(body: ["v": 1, "cmd": "motion.start"], originAllowed: false)
        XCTAssertEqual(forbiddenMotion["error"]?["code"], .string("forbidden"))
        XCTAssertEqual(services.motionCalls, ["start", "samples", "stop"], "a forbidden origin must not reach the accelerometer")

        let speak = await bridge.handle(body: ["v": 1, "cmd": "speech.speak", "args": ["text": "Carry 151 Meter", "lang": "de-DE"]], originAllowed: true)
        XCTAssertEqual(speak["result"]?["speaking"], .bool(true))
        XCTAssertEqual(services.spoken, [SpeechSpeakArguments(text: "Carry 151 Meter", lang: "de-DE", interrupt: false)])
        let badSpeak = await bridge.handle(body: ["v": 1, "cmd": "speech.speak", "args": ["text": "Carry"]], originAllowed: true)
        XCTAssertEqual(badSpeak["error"]?["code"], .string("bad_request"))
        let cancel = await bridge.handle(body: ["v": 1, "cmd": "speech.cancel"], originAllowed: true)
        XCTAssertEqual(cancel["result"]?["speaking"], .bool(false))
        let forbiddenSpeak = await bridge.handle(body: ["v": 1, "cmd": "speech.speak", "args": ["text": "x", "lang": "en-US"]], originAllowed: false)
        XCTAssertEqual(forbiddenSpeak["error"]?["code"], .string("forbidden"))
        XCTAssertEqual(services.spoken.count, 1, "neither a bad call nor a forbidden origin reaches the synthesizer")
        XCTAssertEqual(services.speechCancels, 1)

        let unknown = await bridge.handle(body: ["v": 1, "cmd": "nope"], originAllowed: true)
        XCTAssertEqual(unknown["error"]?["code"], .string("bad_request"))
    }
}

/// Bridge services with canned answers.
@MainActor
final class FakeServices: BridgeServices {
    static let config = BridgeConfig(appVersion: "test", capabilities: .init(
        barometer: false, absoluteAltitude: false, location: true, weather: true,
        audio: false, keepAwake: true, debugCapture: false, motion: true, speech: true
    ))

    var sensorCalls = 0
    var weatherCalls = 0
    var lastSensorArgs: GetSensorsArguments?
    var keepAwake = true
    var weatherResult: Result<JSONValue, BridgeError> = .failure(BridgeError(.network, "offline in tests"))

    func getSensors(_ args: GetSensorsArguments) async -> JSONValue {
        sensorCalls += 1
        lastSensorArgs = args
        return .object(["type": .string("sensors"), "barometer": .null])
    }

    func fetchWeather(_ args: FetchWeatherArguments) async -> Result<JSONValue, BridgeError> {
        weatherCalls += 1
        return weatherResult
    }

    func setKeepAwake(_ on: Bool) -> JSONValue {
        keepAwake = on
        return .object(["keep_awake": .bool(on)])
    }

    var audioCalls: [String] = []
    var lastAudioStart: AudioStartArguments?
    var lastAudioOnsets: AudioOnsetsArguments?
    var audioStartResult: Result<JSONValue, BridgeError> = .failure(BridgeError(.unavailable, "no microphone in tests"))

    func audioStart(_ args: AudioStartArguments) async -> Result<JSONValue, BridgeError> {
        audioCalls.append("start")
        lastAudioStart = args
        return audioStartResult
    }

    func audioStop() -> JSONValue {
        audioCalls.append("stop")
        return .object(["type": .string("audio_status"), "state": .string("idle")])
    }

    func audioStatus() -> JSONValue {
        audioCalls.append("status")
        return .object(["type": .string("audio_status"), "state": .string("idle")])
    }

    func audioOnsets(_ args: AudioOnsetsArguments) -> JSONValue {
        audioCalls.append("onsets")
        lastAudioOnsets = args
        return .array([.object(["type": .string("audio_onset"), "seq": .int(Int64(args.afterSeq + 1))])])
    }

    func audioExportCaptures() async -> Result<JSONValue, BridgeError> {
        audioCalls.append("export")
        return .success(.object(["shared": .bool(false), "files": .int(0), "bytes": .int(0)]))
    }

    var motionCalls: [String] = []
    var lastMotionSamples: MotionSamplesArguments?

    func motionStart() -> Result<JSONValue, BridgeError> {
        motionCalls.append("start")
        return .success(.object(["running": .bool(true), "interval_ms": .double(20)]))
    }

    func motionSamples(_ args: MotionSamplesArguments) -> JSONValue {
        motionCalls.append("samples")
        lastMotionSamples = args
        return .object(["running": .bool(true), "samples": .array([])])
    }

    func motionStop() -> JSONValue {
        motionCalls.append("stop")
        return .object(["running": .bool(false)])
    }

    var spoken: [SpeechSpeakArguments] = []
    var speechCancels = 0

    func speechSpeak(_ args: SpeechSpeakArguments) -> Result<JSONValue, BridgeError> {
        spoken.append(args)
        return .success(.object(["speaking": .bool(true)]))
    }

    func speechCancel() -> JSONValue {
        speechCancels += 1
        return .object(["speaking": .bool(false)])
    }
}
