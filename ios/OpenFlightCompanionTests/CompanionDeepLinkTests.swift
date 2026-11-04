import XCTest
@testable import OpenFlightCompanion

/// `openflight://open?url=…` (CompanionDeepLink): only launch-monitor pages are accepted.
@MainActor
final class CompanionDeepLinkTests: XCTestCase {
    private static let base = URL(string: "http://openflight.local:8080/")!

    private func link(_ target: String) -> URL {
        var components = URLComponents()
        components.scheme = "openflight"
        components.host = "open"
        components.queryItems = [URLQueryItem(name: "url", value: target)]
        return components.url!
    }

    private func target(_ link: URL, base: URL = base) -> String? {
        CompanionDeepLink.target(from: link, configuredBase: base)?.absoluteString
    }

    func testAcceptsThePairingLinkWithItsCodeFragment() {
        let pair = "http://openflight.local:8080/pair#code=ABCD-EFGH"
        XCTAssertEqual(target(link(pair)), pair)
    }

    func testDecodesAnEncodeURIComponentLink() {
        // What the web page builds: encodeURIComponent(location.href).
        let link = URL(string: "openflight://open?url=http%3A%2F%2F192.168.4.1%3A8080%2Fpair%23code%3DABCD-EFGH")!
        XCTAssertEqual(target(link), "http://192.168.4.1:8080/pair#code=ABCD-EFGH")
    }

    func testAcceptsLocalHosts() {
        for host in [
            "openflight.local", "OpenFlight.LOCAL", "pi.lan.local",
            "10.0.0.1", "10.42.0.1", "172.16.0.1", "172.31.255.254",
            "192.168.4.1", "192.168.178.20", "169.254.10.20",
        ] {
            XCTAssertNotNil(target(link("http://\(host):8080/pair")), host)
            XCTAssertTrue(CompanionDeepLink.isLocalHost(host), host)
        }
        XCTAssertNotNil(target(link("https://192.168.4.1/")), "https")
    }

    func testAcceptsTheConfiguredHostEvenWhenNotLocal() {
        let base = URL(string: "http://mock.example:9000/")!
        XCTAssertEqual(target(link("http://mock.example:9000/pair"), base: base), "http://mock.example:9000/pair")
        XCTAssertNil(target(link("http://other.example:9000/pair"), base: base))
    }

    func testRejectsForeignHosts() {
        for host in [
            "example.com", "openflight.local.example.com", "local", ".local", "a..local",
            "8.8.8.8", "172.15.0.1", "172.32.0.1", "192.169.0.1", "169.253.0.1", "127.0.0.1",
            "11.0.0.1", "10.0.0", "10.0.0.1.2", "010.0.0.1", "10.0.0.256", "0x0a.0.0.1", "[fe80::1]",
        ] {
            XCTAssertNil(target(link("http://\(host):8080/pair")), host)
        }
    }

    func testRejectsOtherSchemesAndMalformedLinks() {
        XCTAssertNil(target(link("javascript:alert(1)")))
        XCTAssertNil(target(link("file:///etc/passwd")))
        XCTAssertNil(target(link("ftp://192.168.4.1/")))
        XCTAssertNil(target(link("//192.168.4.1/pair")))
        XCTAssertNil(target(link("http://user:pw@192.168.4.1/pair")))
        XCTAssertNil(target(link("")))
        XCTAssertNil(target(URL(string: "openflight://open")!), "no url parameter")
        XCTAssertNil(target(URL(string: "openflight://other?url=http%3A%2F%2F192.168.4.1%2F")!), "unknown action")
        XCTAssertNil(target(URL(string: "openflight://open/extra?url=http%3A%2F%2F192.168.4.1%2F")!), "unknown path")
        XCTAssertNil(target(URL(string: "https://open?url=http%3A%2F%2F192.168.4.1%2F")!), "not our scheme")
    }

    func testOpenSwitchesTheBaseURLLikeTheSettingsScreen() throws {
        let suite = "openflight.tests.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let settings = AppSettings(defaults: defaults)
        let shell = WebShell(services: FakeServices(), config: FakeServices.config, baseURL: settings.baseURL)

        XCTAssertTrue(CompanionDeepLink.open(link("http://192.168.4.1:8080/pair#code=ABCD-EFGH"), settings: settings, shell: shell))

        XCTAssertEqual(settings.baseURL.absoluteString, "http://192.168.4.1:8080/")
        XCTAssertEqual(AppSettings(defaults: defaults).baseURL.absoluteString, "http://192.168.4.1:8080/", "stored")
        XCTAssertEqual(shell.baseURL, settings.baseURL, "the shell's origin follows")
        XCTAssertEqual(shell.bridge.baseURL, settings.baseURL, "the bridge serves the new origin")
        XCTAssertTrue(shell.hasStartedLoading)
    }

    func testOpenKeepsTheBaseURLForTheConfiguredHost() throws {
        let suite = "openflight.tests.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let settings = AppSettings(defaults: defaults)
        let shell = WebShell(services: FakeServices(), config: FakeServices.config, baseURL: settings.baseURL)

        XCTAssertTrue(CompanionDeepLink.open(link("http://openflight.local:8080/pair#code=ABCD-EFGH"), settings: settings, shell: shell))

        XCTAssertEqual(settings.baseURL, AppSettings.defaultBaseURL)
        XCTAssertNil(defaults.string(forKey: AppSettings.baseURLKey), "no override stored")
        XCTAssertEqual(shell.baseURL, AppSettings.defaultBaseURL)
    }

    func testOpenIgnoresARejectedLink() throws {
        let suite = "openflight.tests.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let settings = AppSettings(defaults: defaults)
        let shell = WebShell(services: FakeServices(), config: FakeServices.config, baseURL: settings.baseURL)

        XCTAssertFalse(CompanionDeepLink.open(link("http://example.com/pair#code=ABCD-EFGH"), settings: settings, shell: shell))

        XCTAssertEqual(settings.baseURL, AppSettings.defaultBaseURL)
        XCTAssertFalse(shell.hasStartedLoading)
        XCTAssertFalse(shell.isWebViewCreated)
    }
}
