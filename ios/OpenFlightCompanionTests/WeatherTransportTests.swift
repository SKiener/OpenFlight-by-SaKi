import Network
import XCTest
@testable import OpenFlightCompanion

final class CellularRequestBuilderTests: XCTestCase {
    func testRequestBytesAreExact() throws {
        let url = OpenMeteo.forecastURL(latitude: 48.1374, longitude: 11.5755)
        let data = try HTTPRequestBuilder.get(url, userAgent: "OpenFlightCompanion/0.1.0 (iOS)").get()
        XCTAssertEqual(
            String(decoding: data, as: UTF8.self),
            "GET /v1/forecast?latitude=48.13740&longitude=11.57550"
                + "&current=temperature_2m,relative_humidity_2m,surface_pressure,pressure_msl,wind_speed_10m,wind_direction_10m"
                + "&wind_speed_unit=ms&timeformat=unixtime&timezone=GMT HTTP/1.1\r\n"
                + "Host: api.open-meteo.com\r\n"
                + "Accept: application/json\r\n"
                + "Accept-Encoding: identity\r\n"
                + "Connection: close\r\n"
                + "User-Agent: OpenFlightCompanion/0.1.0 (iOS)\r\n"
                + "\r\n"
        )
    }

    func testTargetDefaultsToPort443() throws {
        let target = try HTTPRequestBuilder.target(for: XCTUnwrap(URL(string: "https://api.open-meteo.com/v1/forecast"))).get()
        XCTAssertEqual(target, .init(host: "api.open-meteo.com", port: 443))
    }

    func testNonDefaultPortGoesIntoTheHostHeader() throws {
        let url = try XCTUnwrap(URL(string: "https://example.test:8443/x"))
        XCTAssertEqual(try HTTPRequestBuilder.target(for: url).get().port, 8443)
        let text = String(decoding: try HTTPRequestBuilder.get(url, userAgent: "ua").get(), as: UTF8.self)
        XCTAssertTrue(text.hasPrefix("GET /x HTTP/1.1\r\nHost: example.test:8443\r\n"))
    }

    func testEmptyPathBecomesSlash() throws {
        let url = try XCTUnwrap(URL(string: "https://example.test"))
        let text = String(decoding: try HTTPRequestBuilder.get(url, userAgent: "ua").get(), as: UTF8.self)
        XCTAssertTrue(text.hasPrefix("GET / HTTP/1.1\r\n"))
    }

    func testRejectsPlainHTTPAndHeaderInjection() throws {
        XCTAssertEqual(HTTPRequestBuilder.get(try XCTUnwrap(URL(string: "http://api.open-meteo.com/")), userAgent: "ua"), .failure(.notHTTPS))
        XCTAssertEqual(
            HTTPRequestBuilder.get(try XCTUnwrap(URL(string: "https://api.open-meteo.com/")), userAgent: "ua\r\nX-Evil: 1"),
            .failure(.invalidCharacters)
        )
    }

    func testParametersRequireCellular() {
        let parameters = HTTPRequestBuilder.cellularParameters()
        XCTAssertEqual(parameters.requiredInterfaceType, .cellular)
        XCTAssertFalse(parameters.prohibitExpensivePaths)
        XCTAssertFalse(parameters.prohibitConstrainedPaths)
        // TLS is in the protocol stack (default trust evaluation).
        XCTAssertTrue(parameters.defaultProtocolStack.applicationProtocols.contains { $0 is NWProtocolTLS.Options })
    }
}

/// The cellular-first strategy with fake transports: no network in tests.
final class WeatherServiceStrategyTests: XCTestCase {
    final class FakeTransport: WeatherTransport, @unchecked Sendable {
        let kind: WeatherTransportKind
        let outcome: Result<HTTPResponse, Error>
        private(set) var calls = 0
        private(set) var lastURL: URL?
        private(set) var lastTimeout: TimeInterval?

        init(_ kind: WeatherTransportKind, _ outcome: Result<HTTPResponse, Error>) {
            self.kind = kind
            self.outcome = outcome
        }

        func get(_ url: URL, userAgent: String, timeout: TimeInterval) async throws -> HTTPResponse {
            calls += 1
            lastURL = url
            lastTimeout = timeout
            return try outcome.get()
        }
    }

    static let ok = HTTPResponse(status: 200, reason: "OK", headers: [], body: Data(OpenMeteoTests.fixture.utf8))
    static let args = FetchWeatherArguments(latitude: 48.1374, longitude: 11.5755, timeoutMs: 4_000)

    private func deps(
        cellular: FakeTransport, fallback: FakeTransport,
        network: NetworkPaths.State = .init(wifi: true, cellularAvailable: true, cellularDenied: false)
    ) -> WeatherService.Dependencies {
        WeatherService.Dependencies(
            cellular: cellular, fallback: fallback,
            networkState: { network },
            phonePressureHpa: { 950.5 },
            userAgent: "test",
            now: { ClockPair(hostMicroseconds: 1, epochMilliseconds: 1_793_001_700_000, bracketMicroseconds: 1) },
            hostNowUs: { 0 }
        )
    }

    func testCellularAnswerIsUsed() async throws {
        let cellular = FakeTransport(.cellular, .success(Self.ok))
        let fallback = FakeTransport(.default, .success(Self.ok))
        let report = try await WeatherService.run(Self.args, deps: deps(cellular: cellular, fallback: fallback))
        XCTAssertEqual(report.transport, .cellular)
        XCTAssertEqual(report.phonePressureHpa, 950.5)
        XCTAssertEqual(cellular.calls, 1)
        XCTAssertEqual(fallback.calls, 0)
        XCTAssertEqual(cellular.lastTimeout, 4)
        XCTAssertEqual(cellular.lastURL, OpenMeteo.forecastURL(latitude: 48.1374, longitude: 11.5755))
    }

    func testNoCellularPathGoesStraightToTheDefaultRoute() async throws {
        let cellular = FakeTransport(.cellular, .success(Self.ok))
        let fallback = FakeTransport(.default, .success(Self.ok))
        let report = try await WeatherService.run(Self.args, deps: deps(
            cellular: cellular, fallback: fallback,
            network: .init(wifi: true, cellularAvailable: false, cellularDenied: false)
        ))
        XCTAssertEqual(report.transport, .default)
        XCTAssertEqual(cellular.calls, 0)
    }

    func testCellularTransportFailureFallsBack() async throws {
        let cellular = FakeTransport(.cellular, .failure(WeatherTransportError(kind: .unreachable, message: "no route")))
        let fallback = FakeTransport(.default, .success(Self.ok))
        let report = try await WeatherService.run(Self.args, deps: deps(cellular: cellular, fallback: fallback))
        XCTAssertEqual(report.transport, .default)
        XCTAssertEqual(fallback.calls, 1)
    }

    func testHTTPErrorDoesNotFallBack() async {
        let failing = HTTPResponse(status: 503, reason: "Service Unavailable", headers: [], body: Data())
        let cellular = FakeTransport(.cellular, .success(failing))
        let fallback = FakeTransport(.default, .success(Self.ok))
        do {
            _ = try await WeatherService.run(Self.args, deps: deps(cellular: cellular, fallback: fallback))
            XCTFail("503 accepted")
        } catch let error as BridgeError {
            XCTAssertEqual(error.code, .httpError)
            XCTAssertTrue(error.message.contains("503"))
            XCTAssertEqual(fallback.calls, 0)
        } catch {
            XCTFail("unexpected \(error)")
        }
    }

    func testBadBodyIsParseError() async {
        let cellular = FakeTransport(.cellular, .success(HTTPResponse(status: 200, reason: "OK", headers: [], body: Data("{}".utf8))))
        let fallback = FakeTransport(.default, .success(Self.ok))
        do {
            _ = try await WeatherService.run(Self.args, deps: deps(cellular: cellular, fallback: fallback))
            XCTFail("empty body accepted")
        } catch let error as BridgeError {
            XCTAssertEqual(error.code, .parseError)
        } catch {
            XCTFail("unexpected \(error)")
        }
    }

    func testBothFailingNamesBothCauses() async {
        let cellular = FakeTransport(.cellular, .failure(WeatherTransportError(kind: .unreachable, message: "cellular connection failed")))
        let fallback = FakeTransport(.default, .failure(WeatherTransportError(kind: .unreachable, message: "default route failed: offline")))
        do {
            _ = try await WeatherService.run(Self.args, deps: deps(cellular: cellular, fallback: fallback))
            XCTFail("no transport worked")
        } catch let error as BridgeError {
            XCTAssertEqual(error.code, .network)
            XCTAssertTrue(error.message.contains("cellular: cellular connection failed"))
            XCTAssertTrue(error.message.contains("default: default route failed: offline"))
        } catch {
            XCTFail("unexpected \(error)")
        }
    }

    func testCellularDeniedIsNamed() async {
        let cellular = FakeTransport(.cellular, .success(Self.ok))
        let fallback = FakeTransport(.default, .failure(WeatherTransportError(kind: .unreachable, message: "offline")))
        do {
            _ = try await WeatherService.run(Self.args, deps: deps(
                cellular: cellular, fallback: fallback,
                network: .init(wifi: true, cellularAvailable: false, cellularDenied: true)
            ))
            XCTFail("no transport worked")
        } catch let error as BridgeError {
            XCTAssertEqual(error.code, .network)
            XCTAssertTrue(error.message.contains("cellular data is switched off for this app"))
        } catch {
            XCTFail("unexpected \(error)")
        }
    }

    func testBothTimingOutIsTimeout() async {
        let cellular = FakeTransport(.cellular, .failure(WeatherTransportError(kind: .timeout, message: "cellular request timed out after 4 s")))
        let fallback = FakeTransport(.default, .failure(WeatherTransportError(kind: .timeout, message: "default route timed out after 4 s")))
        do {
            _ = try await WeatherService.run(Self.args, deps: deps(cellular: cellular, fallback: fallback))
            XCTFail("timeouts accepted")
        } catch let error as BridgeError {
            XCTAssertEqual(error.code, .timeout)
        } catch {
            XCTFail("unexpected \(error)")
        }
    }

    func testServiceReturnsTheReportJSON() async {
        let service = WeatherService(deps(
            cellular: FakeTransport(.cellular, .success(Self.ok)),
            fallback: FakeTransport(.default, .success(Self.ok))
        ))
        let result = await service.fetch(Self.args)
        guard case .success(let json) = result else { return XCTFail("\(result)") }
        XCTAssertEqual(json["source"], .string("open-meteo"))
        XCTAssertEqual(json["transport"], .string("cellular"))
        XCTAssertEqual(json["latitude"], .double(48.1374))
    }
}

final class AppSettingsTests: XCTestCase {
    func testNormalisesUserInputToAnOrigin() {
        let cases: [(String, String?)] = [
            ("http://openflight.local:8080", "http://openflight.local:8080/"),
            ("openflight.local:8080", "http://openflight.local:8080/"),
            ("  http://OpenFlight.local:8080/pair#code=ABC  ", "http://openflight.local:8080/"),
            ("http://10.42.0.1:8080/companion?x=1", "http://10.42.0.1:8080/"),
            ("http://10.42.0.1:80", "http://10.42.0.1/"),
            ("https://pi.example.test", "https://pi.example.test/"),
            ("http://127.0.0.1:8361", "http://127.0.0.1:8361/"),
            ("", nil),
            ("   ", nil),
            ("ftp://openflight.local", nil),
            ("http://", nil),
            ("http://user:pw@openflight.local:8080", nil),
            ("javascript:alert(1)", nil),
        ]
        for (input, expected) in cases {
            XCTAssertEqual(AppSettings.normalizedBaseURL(input)?.absoluteString, expected, input)
        }
    }

    @MainActor
    func testStoresAndResetsTheOverride() throws {
        let suite = "openflight.tests.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }

        let settings = AppSettings(defaults: defaults)
        XCTAssertEqual(settings.baseURL, AppSettings.defaultBaseURL)
        XCTAssertEqual(AppSettings.defaultBaseURL.absoluteString, "http://openflight.local:8080/")
        XCTAssertEqual(settings.startURL.absoluteString, "http://openflight.local:8080/")

        XCTAssertFalse(settings.setBaseURL(text: "not a url at all ://"))
        XCTAssertTrue(settings.setBaseURL(text: "10.42.0.1:8080"))
        XCTAssertEqual(AppSettings(defaults: defaults).baseURL.absoluteString, "http://10.42.0.1:8080/")

        settings.resetBaseURL()
        XCTAssertEqual(settings.baseURL.absoluteString, "http://openflight.local:8080/")
        XCTAssertEqual(AppSettings(defaults: defaults).baseURL.absoluteString, "http://openflight.local:8080/")
    }
}
