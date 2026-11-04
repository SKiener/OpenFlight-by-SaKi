import Foundation

/// One way to send the weather request.
protocol WeatherTransport: Sendable {
    var kind: WeatherTransportKind { get }
    func get(_ url: URL, userAgent: String, timeout: TimeInterval) async throws -> HTTPResponse
}

/// Cellular-only `NWConnection` (design §8 step 1).
struct CellularWeatherTransport: WeatherTransport {
    let kind = WeatherTransportKind.cellular
    private let client = CellularHTTPClient()

    func get(_ url: URL, userAgent: String, timeout: TimeInterval) async throws -> HTTPResponse {
        try await client.get(url, userAgent: userAgent, timeout: timeout)
    }
}

/// `URLSession` on the default route (design §8 step 2).
struct DefaultRouteWeatherTransport: WeatherTransport {
    let kind = WeatherTransportKind.default

    func get(_ url: URL, userAgent: String, timeout: TimeInterval) async throws -> HTTPResponse {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = timeout
        configuration.timeoutIntervalForResource = timeout
        configuration.waitsForConnectivity = false
        let session = URLSession(configuration: configuration)
        defer { session.finishTasksAndInvalidate() }
        var request = URLRequest(url: url)
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue(userAgent, forHTTPHeaderField: "User-Agent")
        do {
            let (data, response) = try await session.data(for: request)
            guard let http = response as? HTTPURLResponse else {
                throw WeatherTransportError(kind: .protocolError, message: "not an HTTP response")
            }
            let headers = http.allHeaderFields.compactMap { key, value -> (name: String, value: String)? in
                guard let name = key as? String else { return nil }
                return (name, "\(value)")
            }
            return HTTPResponse(
                status: http.statusCode,
                reason: HTTPURLResponse.localizedString(forStatusCode: http.statusCode),
                headers: headers,
                body: data
            )
        } catch let error as URLError where error.code == .timedOut {
            throw WeatherTransportError(kind: .timeout, message: "default route timed out after \(Int(timeout)) s")
        } catch let error as URLError {
            throw WeatherTransportError(kind: .unreachable, message: "default route failed: \(error.localizedDescription)")
        }
    }
}

/// Cellular first, default route as fallback; maps the answer to a `NativeWeatherReport`.
actor WeatherService {
    struct Dependencies {
        var cellular: WeatherTransport
        var fallback: WeatherTransport
        /// Current state of the cellular-only path monitor.
        var networkState: @Sendable () -> NetworkPaths.State
        var phonePressureHpa: @Sendable () async -> Double?
        var userAgent: String
        var now: @Sendable () -> ClockPair = { ClockPair.capture() }
        var hostNowUs: @Sendable () -> Int64 = { HostClock.nowMicroseconds() }
    }

    private let deps: Dependencies
    /// Concurrent calls for the same coordinates share one request (design §8 step 4).
    private var inFlight: [String: Task<NativeWeatherReport, Error>] = [:]

    init(_ deps: Dependencies) {
        self.deps = deps
    }

    func fetch(_ args: FetchWeatherArguments) async -> Result<JSONValue, BridgeError> {
        let key = OpenMeteo.formatCoordinate(args.latitude) + "," + OpenMeteo.formatCoordinate(args.longitude)
        let task: Task<NativeWeatherReport, Error>
        if let existing = inFlight[key] {
            task = existing
        } else {
            let deps = self.deps
            task = Task { try await Self.run(args, deps: deps) }
            inFlight[key] = task
        }
        defer { if inFlight[key] == task { inFlight[key] = nil } }
        do {
            return .success(try await task.value.json)
        } catch let error as BridgeError {
            return .failure(error)
        } catch {
            return .failure(BridgeError(.internal, "\(error)"))
        }
    }

    /// The strategy, kept static and dependency-injected for tests.
    static func run(_ args: FetchWeatherArguments, deps: Dependencies) async throws -> NativeWeatherReport {
        let url = OpenMeteo.forecastURL(latitude: args.latitude, longitude: args.longitude)
        let timeout = args.timeoutMs / 1_000
        let network = deps.networkState()
        var failures: [String] = []

        var attempts: [WeatherTransport] = []
        if network.cellularAvailable {
            attempts.append(deps.cellular)
        } else {
            failures.append(network.cellularDenied
                ? "cellular: cellular data is switched off for this app"
                : "cellular: no cellular path")
        }
        attempts.append(deps.fallback)

        for transport in attempts {
            let started = deps.hostNowUs()
            let response: HTTPResponse
            do {
                response = try await transport.get(url, userAgent: deps.userAgent, timeout: timeout)
            } catch let error as WeatherTransportError {
                failures.append("\(transport.kind.rawValue): \(error.message)")
                continue
            } catch is CancellationError {
                throw BridgeError(.network, "weather request cancelled")
            } catch {
                failures.append("\(transport.kind.rawValue): \(error.localizedDescription)")
                continue
            }
            // Open-Meteo answered: its verdict stands. Retrying the same server over the other
            // route would only repeat it, so http_error and parse_error never fall back.
            guard (200...299).contains(response.status) else {
                throw BridgeError(.httpError, "Open-Meteo HTTP \(response.status) via \(transport.kind.rawValue)")
            }
            let fetchMs = Double(deps.hostNowUs() - started) / 1_000
            let context = OpenMeteo.Context(
                latitude: args.latitude,
                longitude: args.longitude,
                phonePressureHpa: await deps.phonePressureHpa(),
                transport: transport.kind,
                fetchMs: fetchMs,
                clock: deps.now()
            )
            return try OpenMeteo.parse(response.body, context: context).get()
        }
        let allTimedOut = !failures.isEmpty && failures.allSatisfy { $0.contains("timed out") }
        throw BridgeError(allTimedOut ? .timeout : .network, failures.joined(separator: "; "))
    }
}
