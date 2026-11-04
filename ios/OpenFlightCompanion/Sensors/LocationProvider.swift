import CoreLocation
import Foundation

/// A location fix as the bridge reports it.
struct LocationFix: Equatable {
    let latitude: Double
    let longitude: Double
    let horizontalAccuracyM: Double?
    let altitudeM: Double?
    let verticalAccuracyM: Double?
    let fixEpochMs: Double

    init(_ location: CLLocation) {
        latitude = location.coordinate.latitude
        longitude = location.coordinate.longitude
        // Core Location marks invalid accuracies with negative values.
        horizontalAccuracyM = location.horizontalAccuracy >= 0 ? location.horizontalAccuracy : nil
        let altitudeValid = location.verticalAccuracy >= 0
        altitudeM = altitudeValid ? location.altitude : nil
        verticalAccuracyM = altitudeValid ? location.verticalAccuracy : nil
        fixEpochMs = location.timestamp.timeIntervalSince1970 * 1_000
    }

    init(latitude: Double, longitude: Double, horizontalAccuracyM: Double?, altitudeM: Double?,
         verticalAccuracyM: Double?, fixEpochMs: Double) {
        self.latitude = latitude
        self.longitude = longitude
        self.horizontalAccuracyM = horizontalAccuracyM
        self.altitudeM = altitudeM
        self.verticalAccuracyM = verticalAccuracyM
        self.fixEpochMs = fixEpochMs
    }

    func ageS(nowEpochMs: Double) -> Double {
        max(0, (nowEpochMs - fixEpochMs) / 1_000)
    }
}

enum LocationAuthorization {
    static func name(_ status: CLAuthorizationStatus) -> String {
        switch status {
        case .authorizedWhenInUse, .authorizedAlways: return "when_in_use"
        case .denied: return "denied"
        case .restricted: return "restricted"
        case .notDetermined: return "not_determined"
        @unknown default: return "not_determined"
        }
    }

    static func accuracyName(_ accuracy: CLAccuracyAuthorization) -> String {
        switch accuracy {
        case .fullAccuracy: return "full"
        case .reducedAccuracy: return "reduced"
        @unknown default: return "reduced"
        }
    }

    static func isAuthorized(_ status: CLAuthorizationStatus) -> Bool {
        status == .authorizedWhenInUse || status == .authorizedAlways
    }
}

/// One-shot location fixes on demand (design F3): GPS is never left running.
@MainActor
final class LocationProvider: NSObject, CLLocationManagerDelegate {
    private let manager = CLLocationManager()
    private(set) var lastFix: LocationFix?
    private var waiters: [CheckedContinuation<Void, Never>] = []
    private var authorizationWaiters: [CheckedContinuation<Void, Never>] = []
    private var requesting = false

    override init() {
        super.init()
        manager.delegate = self
        // The weather grid is ~10 km; 100 m keeps the fix quick and cheap (design §9.9).
        manager.desiredAccuracy = kCLLocationAccuracyHundredMeters
    }

    var authorization: String { LocationAuthorization.name(manager.authorizationStatus) }
    var accuracyAuthorization: String { LocationAuthorization.accuracyName(manager.accuracyAuthorization) }

    /// Location services are switched on for the device. Apple warns against calling this on
    /// the main thread, so it runs on a background queue.
    nonisolated static func servicesEnabled() async -> Bool {
        await withCheckedContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                continuation.resume(returning: CLLocationManager.locationServicesEnabled())
            }
        }
    }

    /// Returns once the cached fix is at most `maxAgeS` old, a new fix arrived, the request
    /// failed, or `timeoutS` passed. Prompts for when-in-use permission the first time.
    func refreshIfOlder(than maxAgeS: Double, timeoutS: Double) async {
        let nowMs = Date().timeIntervalSince1970 * 1_000
        if let fix = lastFix, fix.ageS(nowEpochMs: nowMs) <= maxAgeS { return }
        guard timeoutS > 0 else { return }

        let deadline = ContinuousClock.now + .milliseconds(Int(timeoutS * 1_000))
        if manager.authorizationStatus == .notDetermined {
            await waitForAuthorization(until: deadline)
        }
        guard LocationAuthorization.isAuthorized(manager.authorizationStatus) else { return }

        await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
            waiters.append(continuation)
            if !requesting {
                requesting = true
                manager.requestLocation()
            }
            scheduleTimeout(at: deadline)
        }
    }

    private func waitForAuthorization(until deadline: ContinuousClock.Instant) async {
        await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
            authorizationWaiters.append(continuation)
            manager.requestWhenInUseAuthorization()
            Task { @MainActor [weak self] in
                try? await Task.sleep(until: deadline, clock: .continuous)
                self?.resumeAuthorizationWaiters()
            }
        }
    }

    private func scheduleTimeout(at deadline: ContinuousClock.Instant) {
        Task { @MainActor [weak self] in
            try? await Task.sleep(until: deadline, clock: .continuous)
            self?.finishRequest()
        }
    }

    /// Resumes everyone waiting for a fix. The timeout of an earlier caller also releases later
    /// callers; they then see the cached state, which getSensors reports as-is.
    private func finishRequest() {
        requesting = false
        let pending = waiters
        waiters.removeAll()
        pending.forEach { $0.resume() }
    }

    private func resumeAuthorizationWaiters() {
        let pending = authorizationWaiters
        authorizationWaiters.removeAll()
        pending.forEach { $0.resume() }
    }

    nonisolated func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        Task { @MainActor in
            if manager.authorizationStatus != .notDetermined { self.resumeAuthorizationWaiters() }
        }
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        guard let location = locations.last else { return }
        let fix = LocationFix(location)
        Task { @MainActor in
            self.lastFix = fix
            self.finishRequest()
        }
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
        Task { @MainActor in self.finishRequest() }
    }
}
