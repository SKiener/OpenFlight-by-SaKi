import Foundation
import UIKit

/// The `getSensors()` result (design §7.3). Pure value: assembled by `SensorHub`, serialised
/// here, so the field set can be tested without hardware.
struct SensorSnapshot: JSONRepresentable {
    struct Barometer: Equatable {
        var authorization: String
        var sample: BarometerSample?
    }

    struct AbsoluteAltitude: Equatable {
        var sample: AbsoluteAltitudeSample?
    }

    struct Location: Equatable {
        var authorization: String
        var accuracyAuthorization: String
        var fix: LocationFix?
    }

    struct Audio: Equatable {
        var state: String
        var captureId: String?
        var lastSeq: Int
        static let idle = Audio(state: "idle", captureId: nil, lastSeq: 0)
    }

    var clock: ClockPair
    var timeZone: TimeZone = .current
    /// nil: no barometer hardware.
    var barometer: Barometer?
    /// nil: no absolute-altitude hardware (iPhone 12+ only).
    var absoluteAltitude: AbsoluteAltitude?
    /// nil: location services are off.
    var location: Location?
    var network: NetworkPaths.State
    var audio: Audio
    /// 0…100, nil when unknown (Simulator, monitoring off).
    var batteryPct: Int?
    var batteryState: String
    var keepAwake: Bool

    static func batteryStateName(_ state: UIDevice.BatteryState) -> String {
        switch state {
        case .charging: return "charging"
        case .full: return "full"
        case .unplugged: return "unplugged"
        case .unknown: return "unknown"
        @unknown default: return "unknown"
        }
    }

    /// `UIDevice.batteryLevel` is 0…1, or −1 when unknown.
    static func batteryPercent(_ level: Float) -> Int? {
        guard level.isFinite, level >= 0 else { return nil }
        return Int((Double(level) * 100).rounded())
    }

    private func ageS(sampleHostUs: Int64?) -> JSONValue {
        guard let sampleHostUs else { return .null }
        return .number(max(0, Double(clock.hostMicroseconds - sampleHostUs) / 1_000_000))
    }

    var json: JSONValue {
        var fields = ClockFields.fields(clock, timeZone: timeZone)
        fields["type"] = .string("sensors")

        fields["barometer"] = barometer.map { barometer -> JSONValue in
            .object([
                "authorization": .string(barometer.authorization),
                "pressure_hpa": .number(barometer.sample?.pressureHpa),
                "relative_altitude_m": .number(barometer.sample?.relativeAltitudeM),
                "sample_host_us": .integer(barometer.sample?.sampleHostUs),
                "age_s": ageS(sampleHostUs: barometer.sample?.sampleHostUs),
            ])
        } ?? .null

        fields["absolute_altitude"] = absoluteAltitude.map { altitude -> JSONValue in
            .object([
                "altitude_m": .number(altitude.sample?.altitudeM),
                "accuracy_m": .number(altitude.sample?.accuracyM),
                "precision_m": .number(altitude.sample?.precisionM),
                "age_s": ageS(sampleHostUs: altitude.sample?.sampleHostUs),
            ])
        } ?? .null

        fields["location"] = location.map { location -> JSONValue in
            let fix = location.fix
            return .object([
                "authorization": .string(location.authorization),
                "accuracy_authorization": .string(location.accuracyAuthorization),
                "latitude": .number(fix?.latitude),
                "longitude": .number(fix?.longitude),
                "horizontal_accuracy_m": .number(fix?.horizontalAccuracyM),
                "altitude_m": .number(fix?.altitudeM),
                "vertical_accuracy_m": .number(fix?.verticalAccuracyM),
                "fix_epoch_ms": .number(fix.map { ($0.fixEpochMs * 1_000).rounded() / 1_000 }),
                "age_s": .number(fix?.ageS(nowEpochMs: clock.epochMilliseconds)),
            ])
        } ?? .null

        fields["network"] = .object([
            "wifi": .bool(network.wifi),
            "cellular_available": .bool(network.cellularAvailable),
        ])
        fields["audio"] = .object([
            "state": .string(audio.state),
            "capture_id": .text(audio.captureId),
            "last_seq": .integer(audio.lastSeq),
        ])
        fields["battery_pct"] = .integer(batteryPct)
        fields["battery_state"] = .string(batteryState)
        fields["keep_awake"] = .bool(keepAwake)
        return .object(fields)
    }
}

/// Owns the sensor providers and assembles snapshots.
@MainActor
final class SensorHub {
    let barometer = BarometerProvider()
    let location = LocationProvider()
    let network: NetworkPaths
    private let keepAwake: KeepAwake
    private let audioSummary: @MainActor () -> SensorSnapshot.Audio
    private var locationServicesEnabled: Bool?

    init(network: NetworkPaths, keepAwake: KeepAwake, audioSummary: @escaping @MainActor () -> SensorSnapshot.Audio = { .idle }) {
        self.network = network
        self.keepAwake = keepAwake
        self.audioSummary = audioSummary
        UIDevice.current.isBatteryMonitoringEnabled = true
    }

    func setForeground(_ active: Bool) {
        barometer.setForeground(active)
        if active { locationServicesEnabled = nil }  // re-check: the user may have changed it
    }

    /// Latest barometer pressure in hPa (for `phone_pressure_hpa` in the weather report).
    var phonePressureHpa: Double? { barometer.sample?.pressureHpa }

    func snapshot(_ args: GetSensorsArguments) async -> SensorSnapshot {
        if BarometerProvider.isAvailable { barometer.activate() }

        let servicesOn: Bool
        if let cached = locationServicesEnabled {
            servicesOn = cached
        } else {
            servicesOn = await LocationProvider.servicesEnabled()
            locationServicesEnabled = servicesOn
        }
        if servicesOn, let maxAge = args.locationMaxAgeS {
            await location.refreshIfOlder(than: maxAge, timeoutS: args.timeoutMs / 1_000)
        }

        let device = UIDevice.current
        return SensorSnapshot(
            clock: ClockPair.capture(),
            barometer: BarometerProvider.isAvailable
                ? .init(authorization: barometer.authorization, sample: barometer.sample) : nil,
            absoluteAltitude: BarometerProvider.isAbsoluteAvailable
                ? .init(sample: barometer.absoluteSample) : nil,
            location: servicesOn
                ? .init(
                    authorization: location.authorization,
                    accuracyAuthorization: location.accuracyAuthorization,
                    fix: location.lastFix
                ) : nil,
            network: network.state,
            audio: audioSummary(),
            batteryPct: SensorSnapshot.batteryPercent(device.batteryLevel),
            batteryState: SensorSnapshot.batteryStateName(device.batteryState),
            keepAwake: keepAwake.isEngaged
        )
    }
}
