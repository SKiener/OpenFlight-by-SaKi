import Foundation
import Network

/// Two path monitors (design §7.3 `network`, §8): the default route, and a cellular-only path.
final class NetworkPaths {
    struct State: Equatable {
        var wifi: Bool
        var cellularAvailable: Bool
        /// The user switched cellular data off for this app.
        var cellularDenied: Bool
    }

    private let defaultMonitor = NWPathMonitor()
    private let cellularMonitor = NWPathMonitor(requiredInterfaceType: .cellular)
    private let queue = DispatchQueue(label: "openflight.network-paths", qos: .utility)
    private let lock = NSLock()
    private var current = State(wifi: false, cellularAvailable: false, cellularDenied: false)

    init() {
        defaultMonitor.pathUpdateHandler = { [weak self] path in
            guard let self else { return }
            let wifi = path.status == .satisfied && path.usesInterfaceType(.wifi)
            self.lock.withLock { self.current.wifi = wifi }
        }
        cellularMonitor.pathUpdateHandler = { [weak self] path in
            guard let self else { return }
            let satisfied = path.status == .satisfied
            let denied = path.status != .satisfied && path.unsatisfiedReason == .cellularDenied
            self.lock.withLock {
                self.current.cellularAvailable = satisfied
                self.current.cellularDenied = denied
            }
        }
        defaultMonitor.start(queue: queue)
        cellularMonitor.start(queue: queue)
    }

    deinit {
        defaultMonitor.cancel()
        cellularMonitor.cancel()
    }

    var state: State { lock.withLock { current } }
}
