import XCTest
@testable import OpenFlightCompanion

final class ClockAndSnapshotTests: XCTestCase {
    private let berlin = TimeZone(identifier: "Europe/Berlin")!

    // MARK: Clock pair

    func testClockPairArithmeticIsExact() {
        // t_a = 1 000 000 123 456 ns, t_b = t_a + 3 001 ns, wall = 1 761 469 200.123 456 789 s.
        let pair = ClockPair(
            hostBeforeNs: 1_000_000_123_456,
            wallSeconds: 1_761_469_200,
            wallNanoseconds: 123_456_789,
            hostAfterNs: 1_000_000_126_457
        )
        XCTAssertEqual(pair.hostMicroseconds, 1_000_000_124)   // midpoint 1 000 000 124 956 ns → µs
        XCTAssertEqual(pair.bracketMicroseconds, 3)
        XCTAssertEqual(pair.epochMilliseconds, 1_761_469_200_123.456, accuracy: 1e-6)
        XCTAssertEqual(pair.epochMillisecondsInteger, 1_761_469_200_123)

        // Host → wall through the pair: +1 500 µs is +1.5 ms.
        XCTAssertEqual(pair.epochMilliseconds(forHostMicroseconds: 1_000_001_624), 1_761_469_200_124.956, accuracy: 1e-6)
        XCTAssertEqual(pair.epochMilliseconds(forHostMicroseconds: pair.hostMicroseconds), pair.epochMilliseconds)
    }

    func testClockPairNeverGoesNegativeOnABackwardsRead() {
        let pair = ClockPair(hostBeforeNs: 5_000_000, wallSeconds: 1, wallNanoseconds: 0, hostAfterNs: 4_000_000)
        XCTAssertEqual(pair.bracketMicroseconds, 0)
        XCTAssertEqual(pair.hostMicroseconds, 5_000)
    }

    func testCapturedPairIsSane() {
        let pair = ClockPair.capture()
        XCTAssertGreaterThan(pair.hostMicroseconds, 0)
        XCTAssertLessThan(pair.hostMicroseconds, JSONValue.maxSafeInteger)
        XCTAssertLessThan(pair.bracketMicroseconds, 1_000_000)
        XCTAssertEqual(pair.epochMilliseconds, Date().timeIntervalSince1970 * 1_000, accuracy: 5_000)
    }

    func testUptimeSecondsToHostMicroseconds() {
        XCTAssertEqual(HostClock.microseconds(fromUptimeSeconds: 12.000_001_4), 12_000_001)
        XCTAssertNil(HostClock.microseconds(fromUptimeSeconds: -1))
        XCTAssertNil(HostClock.microseconds(fromUptimeSeconds: .nan))
    }

    func testClockFieldsMatchClientClockPayload() {
        // 2026-10-26 is after the CEST→CET switch (UTC+1); 2026-07-01 is CEST (UTC+2).
        let winter = ClockPair(hostMicroseconds: 1, epochMilliseconds: 1_793_000_000_000.5, bracketMicroseconds: 2)
        let fields = ClockFields.fields(winter, timeZone: berlin)
        XCTAssertEqual(fields["epoch_ms"], .int(1_793_000_000_000))
        XCTAssertEqual(fields["tz_offset_min"], .int(60))
        XCTAssertEqual(fields["clock"], .object(["host_us": .int(1), "epoch_ms": .double(1_793_000_000_000.5), "bracket_us": .int(2)]))
        let summer = ClockFields.tzOffsetMinutes(at: 1_782_864_000_000, timeZone: berlin)
        XCTAssertEqual(summer, 120)
    }

    // MARK: Pressure units

    func testBarometerKilopascalsBecomeHectopascals() {
        XCTAssertEqual(Pressure.hectopascals(fromKilopascals: 101.325), 1_013.25)
        XCTAssertEqual(Pressure.hectopascals(fromKilopascals: 95.123)!, 951.23, accuracy: 1e-9)
        XCTAssertEqual(Pressure.hectopascals(fromPascals: 101_325), 1_013.25)
        XCTAssertNil(Pressure.hectopascals(fromKilopascals: .nan))
    }

    // MARK: Snapshot golden

    private func snapshot(full: Bool) -> SensorSnapshot {
        let clock = ClockPair(hostMicroseconds: 2_000_000_000, epochMilliseconds: 1_793_000_000_000.25, bracketMicroseconds: 4)
        return SensorSnapshot(
            clock: clock,
            timeZone: berlin,
            barometer: full ? .init(authorization: "authorized", sample: BarometerSample(
                pressureHpa: 951.23, relativeAltitudeM: 0.4, sampleHostUs: 1_999_500_000
            )) : nil,
            absoluteAltitude: full ? .init(sample: AbsoluteAltitudeSample(
                altitudeM: 520, accuracyM: 3, precisionM: 0.5, sampleHostUs: 1_999_000_000
            )) : nil,
            location: full ? .init(authorization: "when_in_use", accuracyAuthorization: "full", fix: LocationFix(
                latitude: 48.13743, longitude: 11.57549, horizontalAccuracyM: 35, altitudeM: 519,
                verticalAccuracyM: 8, fixEpochMs: 1_792_999_990_000.25
            )) : nil,
            network: .init(wifi: true, cellularAvailable: false, cellularDenied: false),
            audio: .idle,
            batteryPct: full ? 87 : nil,
            batteryState: "unplugged",
            keepAwake: true
        )
    }

    /// Every §7.3 key with its JSON type.
    func testSnapshotGoldenKeysAndTypes() throws {
        let json = snapshot(full: true).json
        let expected = try XCTUnwrap(
            JSONSerialization.jsonObject(with: Data(Self.goldenSnapshot.utf8)) as? [String: Any]
        )
        let actual = try XCTUnwrap(
            JSONSerialization.jsonObject(with: Data(json.jsonString().utf8)) as? [String: Any]
        )
        XCTAssertEqual(NSDictionary(dictionary: actual), NSDictionary(dictionary: expected))
    }

    func testSnapshotWithoutHardwareUsesNulls() throws {
        let json = snapshot(full: false).json
        XCTAssertEqual(json["barometer"], .null)
        XCTAssertEqual(json["absolute_altitude"], .null)
        XCTAssertEqual(json["location"], .null)
        XCTAssertEqual(json["battery_pct"], .null)
        XCTAssertEqual(json["type"], .string("sensors"))
        XCTAssertNotNil(json["network"]?["wifi"])
    }

    func testSnapshotBeforeTheFirstSampleHasNullValues() {
        var value = snapshot(full: false)
        value.barometer = .init(authorization: "not_determined", sample: nil)
        value.location = .init(authorization: "not_determined", accuracyAuthorization: "full", fix: nil)
        let json = value.json
        XCTAssertEqual(json["barometer"]?["authorization"], .string("not_determined"))
        XCTAssertEqual(json["barometer"]?["pressure_hpa"], .null)
        XCTAssertEqual(json["barometer"]?["age_s"], .null)
        XCTAssertEqual(json["location"]?["latitude"], .null)
        XCTAssertEqual(json["location"]?["age_s"], .null)
    }

    func testBatteryHelpers() {
        XCTAssertNil(SensorSnapshot.batteryPercent(-1))
        XCTAssertEqual(SensorSnapshot.batteryPercent(0.874), 87)
        XCTAssertEqual(SensorSnapshot.batteryPercent(1), 100)
        XCTAssertEqual(SensorSnapshot.batteryStateName(.charging), "charging")
        XCTAssertEqual(SensorSnapshot.batteryStateName(.unknown), "unknown")
    }

    static let goldenSnapshot = """
    {
      "type": "sensors",
      "epoch_ms": 1793000000000,
      "tz_offset_min": 60,
      "clock": {"host_us": 2000000000, "epoch_ms": 1793000000000.25, "bracket_us": 4},
      "barometer": {
        "authorization": "authorized", "pressure_hpa": 951.23, "relative_altitude_m": 0.4,
        "sample_host_us": 1999500000, "age_s": 0.5
      },
      "absolute_altitude": {"altitude_m": 520, "accuracy_m": 3, "precision_m": 0.5, "age_s": 1},
      "location": {
        "authorization": "when_in_use", "accuracy_authorization": "full",
        "latitude": 48.13743, "longitude": 11.57549, "horizontal_accuracy_m": 35,
        "altitude_m": 519, "vertical_accuracy_m": 8,
        "fix_epoch_ms": 1792999990000.25, "age_s": 10
      },
      "network": {"wifi": true, "cellular_available": false},
      "audio": {"state": "idle", "capture_id": null, "last_seq": 0},
      "battery_pct": 87,
      "battery_state": "unplugged",
      "keep_awake": true
    }
    """
}
