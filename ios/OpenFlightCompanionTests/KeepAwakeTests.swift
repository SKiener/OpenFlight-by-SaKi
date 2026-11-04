import SwiftUI
import XCTest
@testable import OpenFlightCompanion

/// The screen stays on while the app is in the foreground (design F15), with an injected
/// idle-timer setter instead of `UIApplication.shared`.
@MainActor
final class KeepAwakeTests: XCTestCase {
    private var writes: [Bool] = []

    private func makeKeepAwake(userEnabled: Bool = true) -> KeepAwake {
        KeepAwake(userEnabled: userEnabled) { [unowned self] in self.writes.append($0) }
    }

    func testConstructionDoesNotTouchTheIdleTimer() {
        _ = makeKeepAwake()
        XCTAssertEqual(writes, [])
    }

    func testActiveDisablesTheIdleTimerAndLeavingTheForegroundRestoresIt() {
        let keepAwake = makeKeepAwake()
        keepAwake.setScenePhase(.active)
        XCTAssertTrue(keepAwake.isEngaged)
        keepAwake.setScenePhase(.inactive)
        XCTAssertFalse(keepAwake.isEngaged)
        keepAwake.setScenePhase(.active)
        keepAwake.setScenePhase(.background)
        XCTAssertFalse(keepAwake.isEngaged)
        XCTAssertEqual(writes, [true, false, true, false])
    }

    func testTheSettingSwitchesItOffAndOnWhileActive() {
        let keepAwake = makeKeepAwake()
        keepAwake.setScenePhase(.active)
        keepAwake.setUserEnabled(false)
        XCTAssertFalse(keepAwake.isEngaged)
        keepAwake.setUserEnabled(true)
        XCTAssertTrue(keepAwake.isEngaged)
        XCTAssertEqual(writes, [true, false, true])
    }

    func testDisabledSettingNeverEngages() {
        let keepAwake = makeKeepAwake(userEnabled: false)
        keepAwake.setScenePhase(.active)
        XCTAssertFalse(keepAwake.isEngaged)
        XCTAssertEqual(writes, [false])
    }

    func testThePageRequestIsAlsoRequired() {
        let keepAwake = makeKeepAwake()
        keepAwake.setScenePhase(.active)
        keepAwake.setRequested(false)
        XCTAssertFalse(keepAwake.isEngaged)
        keepAwake.setRequested(true)
        XCTAssertEqual(writes, [true, false, true])
    }

    func testKeepScreenOnDefaultsToOnAndPersists() throws {
        let suite = "KeepAwakeTests.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        XCTAssertTrue(AppSettings(defaults: defaults).keepScreenOn)
        AppSettings(defaults: defaults).keepScreenOn = false
        XCTAssertFalse(AppSettings(defaults: defaults).keepScreenOn)
    }
}
