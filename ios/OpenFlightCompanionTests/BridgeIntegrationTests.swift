import WebKit
import XCTest
@testable import OpenFlightCompanion

/// A real `WKWebView` in the test host, configured exactly as the app does (design §12.1 #13).
@MainActor
final class BridgeIntegrationTests: XCTestCase {
    private static let base = URL(string: "http://openflight.test:8080/")!
    private var services: FakeServices!
    private var shell: WebShell!

    private static let page = """
    <!doctype html><html><head><script>
      window.__early = typeof window.openflightNative !== 'undefined' ? window.openflightNative.version : 'missing';
      window.__events = [];
      window.addEventListener('openflight-native', function (e) { window.__events.push(e.detail); });
    </script></head><body>fixture</body></html>
    """

    override func setUp() async throws {
        services = FakeServices()
        shell = WebShell(services: services, config: FakeServices.config, baseURL: Self.base)
        // Inside a window, so timers and JS run as on the device.
        let scene = UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first
        scene?.windows.first?.addSubview(shell.webView)
        shell.webView.frame = CGRect(x: 0, y: 0, width: 10, height: 10)
        shell.webView.loadHTMLString(Self.page, baseURL: Self.base)
        try await waitForLoad()
    }

    override func tearDown() async throws {
        shell.webView.removeFromSuperview()
        shell = nil
        services = nil
    }

    private func waitForLoad() async throws {
        let deadline = Date().addingTimeInterval(10)
        while Date() < deadline {
            if !shell.webView.isLoading, shell.webView.url != nil,
               (try? await js("return document.readyState")) as? String == "complete" {
                return
            }
            try await Task.sleep(nanoseconds: 50_000_000)
        }
        XCTFail("fixture page did not load")
    }

    private func js(_ body: String) async throws -> Any? {
        try await shell.webView.callAsyncJavaScript(body, arguments: [:], in: nil, contentWorld: .page)
    }

    func testShimExistsBeforePageScriptsRun() async throws {
        let early = try await js("return window.__early")
        XCTAssertEqual(early as? Int, 1)
        let platform = try await js("return window.openflightNative.platform")
        XCTAssertEqual(platform as? String, "ios")
        let version = try await js("return window.openflightNative.app_version")
        XCTAssertEqual(version as? String, "test")
        let weather = try await js("return window.openflightNative.capabilities.weather")
        XCTAssertEqual(weather as? Bool, true)
        let audio = try await js("return window.openflightNative.capabilities.audio")
        XCTAssertEqual(audio as? Bool, false)
        let frozen = try await js("return Object.isFrozen(window.openflightNative)")
        XCTAssertEqual(frozen as? Bool, true)
        // The page cannot replace it.
        let kind = try await js("try { window.openflightNative = 1 } catch (e) {} return typeof window.openflightNative")
        XCTAssertEqual(kind as? String, "object")
    }

    func testSpeechRoundTrip() async throws {
        let available = try await js("return window.openflightNative.capabilities.speech")
        XCTAssertEqual(available as? Bool, true)
        let speaking = try await js("""
            return (await window.openflightNative.speech.speak({ text: 'Carry 151 Meter', lang: 'de-DE', interrupt: true })).speaking
        """)
        XCTAssertEqual(speaking as? Bool, true)
        XCTAssertEqual(services.spoken, [SpeechSpeakArguments(text: "Carry 151 Meter", lang: "de-DE", interrupt: true)])

        let rejected = try await js("""
            try { await window.openflightNative.speech.speak({ text: 'Carry' }); return 'resolved' }
            catch (e) { return e.code }
        """)
        XCTAssertEqual(rejected as? String, "bad_request")

        let cancelled = try await js("return (await window.openflightNative.speech.cancel()).speaking")
        XCTAssertEqual(cancelled as? Bool, false)
        XCTAssertEqual(services.speechCancels, 1)
    }

    func testPromiseRoundTripResolves() async throws {
        let result = try await js("""
            const s = await window.openflightNative.getSensors({ location_max_age_s: 30 });
            return s.type + ':' + (s.barometer === null);
        """)
        XCTAssertEqual(result as? String, "sensors:true")
        XCTAssertEqual(services.lastSensorArgs?.locationMaxAgeS, 30)

        let awake = try await js("return (await window.openflightNative.setKeepAwake(false)).keep_awake")
        XCTAssertEqual(awake as? Bool, false)
        XCTAssertEqual(services.keepAwake, false)
    }

    func testRejectionsCarryTheCode() async throws {
        let badRequest = try await js("""
            try { await window.openflightNative.fetchWeather({ latitude: 999, longitude: 0 }); return 'resolved'; }
            catch (e) { return e.code + '|' + (e instanceof Error); }
        """)
        XCTAssertEqual(badRequest as? String, "bad_request|true")

        let network = try await js("""
            try { await window.openflightNative.fetchWeather({ latitude: 48.1, longitude: 11.5 }); return 'resolved'; }
            catch (e) { return e.code + '|' + e.message; }
        """)
        XCTAssertEqual(network as? String, "network|offline in tests")

        let audio = try await js("""
            try { await window.openflightNative.audio.start(); return 'resolved'; } catch (e) { return e.code; }
        """)
        XCTAssertEqual(audio as? String, "unavailable")
    }

    func testAudioCommandsRoundTrip() async throws {
        services.audioStartResult = .success(.object(["type": .string("audio_status"), "state": .string("running")]))
        let started = try await js("return (await window.openflightNative.audio.start({ debug_capture: true })).state")
        XCTAssertEqual(started as? String, "running")
        XCTAssertEqual(services.lastAudioStart, AudioStartArguments(debugCapture: true))
        let onsets = try await js("""
            const list = await window.openflightNative.audio.onsets({ capture_id: 'abc', after_seq: 2 });
            return list.length + ':' + list[0].seq;
        """)
        XCTAssertEqual(onsets as? String, "1:3")
        let bad = try await js("""
            try { await window.openflightNative.audio.onsets({ capture_id: 'abc' }); return 'resolved'; } catch (e) { return e.code; }
        """)
        XCTAssertEqual(bad as? String, "bad_request")
        let stopped = try await js("return (await window.openflightNative.audio.stop()).state")
        XCTAssertEqual(stopped as? String, "idle")
    }

    func testWeatherResultReachesThePage() async throws {
        services.weatherResult = .success(.object(["source": .string("open-meteo"), "pressure_hpa": .double(954.6)]))
        let result = try await js("""
            const r = await window.openflightNative.fetchWeather({ latitude: 48.1, longitude: 11.5 });
            return r.source + ':' + r.pressure_hpa;
        """)
        XCTAssertEqual(result as? String, "open-meteo:954.6")
    }

    func testCustomEventDelivery() async throws {
        shell.bridge.dispatch(.object(["type": .string("audio_status"), "v": .int(1), "state": .string("idle")]))
        let deadline = Date().addingTimeInterval(5)
        var delivered: String?
        while Date() < deadline {
            delivered = try await js("return window.__events.length ? JSON.stringify(window.__events[0]) : null") as? String
            if delivered != nil { break }
            try await Task.sleep(nanoseconds: 50_000_000)
        }
        XCTAssertEqual(delivered, #"{"state":"idle","type":"audio_status","v":1}"#)
    }

    func testForeignOriginIsForbidden() async throws {
        // The page is now on an origin the bridge no longer trusts.
        shell.bridge.baseURL = URL(string: "http://openflight.local:8080/")!
        let result = try await js("""
            try { await window.openflightNative.getSensors(); return 'resolved'; } catch (e) { return e.code; }
        """)
        XCTAssertEqual(result as? String, "forbidden")
        XCTAssertEqual(services.sensorCalls, 0)
    }

    func testMalformedDirectPostIsBadRequest() async throws {
        let result = try await js("""
            const env = await window.webkit.messageHandlers.openflight.postMessage({ v: 1, cmd: 'nope' });
            return env.ok + ':' + env.error.code;
        """)
        XCTAssertEqual(result as? String, "false:bad_request")
    }
}
