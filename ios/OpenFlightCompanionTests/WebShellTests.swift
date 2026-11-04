import WebKit
import XCTest
@testable import OpenFlightCompanion

/// The app builds its `WebShell` in `App.init`, before `UIApplicationMain` has created the
/// application. The web view must not be built there: gesture recognizers added before the
/// application exists never get a delayed-event dispatcher, and on iOS 27 the first tap on the
/// page then aborts the app (see `WebShell.webView`).
@MainActor
final class WebShellTests: XCTestCase {
    private static let base = URL(string: "http://openflight.test:8080/")!

    private func makeShell() -> WebShell {
        WebShell(services: FakeServices(), config: FakeServices.config, baseURL: Self.base)
    }

    func testInitDoesNotBuildTheWebView() {
        let shell = makeShell()
        XCTAssertFalse(shell.isWebViewCreated)
    }

    func testFirstUseBuildsOneConfiguredWebView() {
        let shell = makeShell()
        let webView = shell.webView

        XCTAssertTrue(shell.isWebViewCreated)
        XCTAssertTrue(shell.webView === webView, "later uses return the same web view")
        XCTAssertTrue(shell.bridge.webView === webView)
        XCTAssertTrue(webView.navigationDelegate === shell)
        XCTAssertTrue(webView.uiDelegate === shell)
        XCTAssertTrue(webView.allowsBackForwardNavigationGestures)
        XCTAssertEqual(webView.scrollView.contentInsetAdjustmentBehavior, .never)
        XCTAssertFalse(webView.isOpaque)

        let configuration = webView.configuration
        XCTAssertEqual(configuration.applicationNameForUserAgent, CompanionServices.userAgentToken)
        XCTAssertTrue(configuration.allowsInlineMediaPlayback)
        XCTAssertEqual(configuration.mediaTypesRequiringUserActionForPlayback, [])
        XCTAssertTrue(configuration.websiteDataStore.isPersistent)
        XCTAssertFalse(configuration.userContentController.userScripts.isEmpty, "bridge shim installed")
    }

    func testLoadBuildsTheWebView() {
        let shell = makeShell()
        shell.load(baseURL: Self.base)
        XCTAssertTrue(shell.isWebViewCreated)
    }
}
