import CoreMotion
import Foundation

/// Pressure conversions. `CMAltitudeData.pressure` is in kilopascals (design F1); every
/// payload carries hPa, the unit `weather.py` and Open-Meteo use.
enum Pressure {
    static func hectopascals(fromKilopascals kPa: Double) -> Double? {
        guard kPa.isFinite else { return nil }
        return kPa * 10
    }

    static func hectopascals(fromPascals pa: Double) -> Double? {
        guard pa.isFinite else { return nil }
        return pa / 100
    }
}

/// One barometer reading.
struct BarometerSample: Equatable {
    let pressureHpa: Double
    let relativeAltitudeM: Double?
    /// `CMLogItem.timestamp` as host µs.
    let sampleHostUs: Int64?
}

struct AbsoluteAltitudeSample: Equatable {
    let altitudeM: Double
    let accuracyM: Double
    let precisionM: Double
    let sampleHostUs: Int64?
}

enum MotionAuthorization {
    static func name(_ status: CMAuthorizationStatus) -> String {
        switch status {
        case .authorized: return "authorized"
        case .denied: return "denied"
        case .restricted: return "restricted"
        case .notDetermined: return "not_determined"
        @unknown default: return "not_determined"
        }
    }
}

/// `CMAltimeter` relative updates (≈1 Hz, pressure) and, where the hardware has it, absolute
/// altitude. Runs only while the app is in the foreground and only after the page first asked
/// for sensors, so the motion prompt appears on the first `getSensors()` (design §7.3).
final class BarometerProvider {
    static var isAvailable: Bool { CMAltimeter.isRelativeAltitudeAvailable() }
    static var isAbsoluteAvailable: Bool { CMAltimeter.isAbsoluteAltitudeAvailable() }

    private let altimeter = CMAltimeter()
    private let queue: OperationQueue = {
        let queue = OperationQueue()
        queue.name = "openflight.barometer"
        queue.maxConcurrentOperationCount = 1
        queue.qualityOfService = .utility
        return queue
    }()
    private let lock = NSLock()
    private var latest: BarometerSample?
    private var latestAbsolute: AbsoluteAltitudeSample?
    private var wanted = false
    private var running = false
    private var foreground = true

    var authorization: String { MotionAuthorization.name(CMAltimeter.authorizationStatus()) }

    var sample: BarometerSample? { lock.withLock { latest } }
    var absoluteSample: AbsoluteAltitudeSample? { lock.withLock { latestAbsolute } }

    /// The page asked for sensors: start (and keep running while in the foreground).
    func activate() {
        lock.withLock { wanted = true }
        updateRunning()
    }

    func setForeground(_ active: Bool) {
        lock.withLock { foreground = active }
        updateRunning()
    }

    private func updateRunning() {
        let (shouldRun, isRunning) = lock.withLock { (wanted && foreground, running) }
        guard Self.isAvailable, shouldRun != isRunning else { return }
        if shouldRun {
            start()
        } else {
            altimeter.stopRelativeAltitudeUpdates()
            if Self.isAbsoluteAvailable { altimeter.stopAbsoluteAltitudeUpdates() }
            lock.withLock { running = false }
        }
    }

    private func start() {
        lock.withLock { running = true }
        altimeter.startRelativeAltitudeUpdates(to: queue) { [weak self] data, _ in
            guard let self, let data, let hpa = Pressure.hectopascals(fromKilopascals: data.pressure.doubleValue)
            else { return }
            let sample = BarometerSample(
                pressureHpa: hpa,
                relativeAltitudeM: data.relativeAltitude.doubleValue,
                sampleHostUs: HostClock.microseconds(fromUptimeSeconds: data.timestamp)
            )
            self.lock.withLock { self.latest = sample }
        }
        guard Self.isAbsoluteAvailable else { return }
        altimeter.startAbsoluteAltitudeUpdates(to: queue) { [weak self] data, _ in
            guard let self, let data else { return }
            let sample = AbsoluteAltitudeSample(
                altitudeM: data.altitude,
                accuracyM: data.accuracy,
                precisionM: data.precision,
                sampleHostUs: HostClock.microseconds(fromUptimeSeconds: data.timestamp)
            )
            self.lock.withLock { self.latestAbsolute = sample }
        }
    }
}
