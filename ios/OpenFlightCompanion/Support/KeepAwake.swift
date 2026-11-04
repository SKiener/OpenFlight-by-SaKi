import SwiftUI
import UIKit

/// Keeps the screen on while the app is in the foreground (design F15).
///
/// On by default because the phone hangs opposite the golfer and must keep showing shots. Two
/// switches can turn it off: the native setting "Display anlassen" (`setUserEnabled`) and the
/// page through `setKeepAwake(false)` (`setRequested`). The idle timer is always re-enabled
/// when the scene leaves the foreground (inactive or background), as Apple asks.
@MainActor
final class KeepAwake: ObservableObject {
    @Published private(set) var requested = true
    @Published private(set) var userEnabled = true
    @Published private(set) var sceneActive = false

    /// Writes `UIApplication.isIdleTimerDisabled`. Injected so tests need no application; the
    /// default touches `UIApplication.shared` only when called, never at construction (the
    /// services are built in `App.init`, before `UIApplicationMain`).
    private let setIdleTimerDisabled: @MainActor (Bool) -> Void

    init(userEnabled: Bool = true,
         setIdleTimerDisabled: @escaping @MainActor (Bool) -> Void = { UIApplication.shared.isIdleTimerDisabled = $0 }) {
        self.userEnabled = userEnabled
        self.setIdleTimerDisabled = setIdleTimerDisabled
    }

    /// Whether the idle timer is disabled right now.
    var isEngaged: Bool { userEnabled && requested && sceneActive }

    func setRequested(_ on: Bool) {
        requested = on
        apply()
    }

    func setUserEnabled(_ on: Bool) {
        userEnabled = on
        apply()
    }

    /// Only `.active` keeps the screen on; inactive (app switcher, Control Center) and
    /// background give the idle timer back.
    func setScenePhase(_ phase: ScenePhase) {
        setSceneActive(phase == .active)
    }

    func setSceneActive(_ active: Bool) {
        sceneActive = active
        apply()
    }

    private func apply() {
        setIdleTimerDisabled(isEngaged)
    }
}
