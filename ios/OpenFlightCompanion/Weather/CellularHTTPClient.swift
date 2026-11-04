import Foundation
import Network

/// Builds the one request the cellular client sends (design §8 step 1). Pure.
enum HTTPRequestBuilder {
    enum BuildError: Error, Equatable {
        case notHTTPS
        case missingHost
        case invalidCharacters
    }

    struct Target: Equatable {
        let host: String
        let port: UInt16
    }

    static func target(for url: URL) -> Result<Target, BuildError> {
        guard url.scheme?.lowercased() == "https" else { return .failure(.notHTTPS) }
        guard let host = url.host, !host.isEmpty else { return .failure(.missingHost) }
        let port = url.port.flatMap { UInt16(exactly: $0) } ?? 443
        return .success(Target(host: host, port: port))
    }

    /// `GET <path>?<query> HTTP/1.1` with `Host`, `Accept`, `Accept-Encoding: identity`,
    /// `Connection: close` and `User-Agent`, terminated by an empty line.
    static func get(_ url: URL, userAgent: String) -> Result<Data, BuildError> {
        let target: Target
        switch self.target(for: url) {
        case .failure(let error): return .failure(error)
        case .success(let value): target = value
        }
        guard let components = URLComponents(url: url, resolvingAgainstBaseURL: false) else {
            return .failure(.invalidCharacters)
        }
        var requestTarget = components.percentEncodedPath.isEmpty ? "/" : components.percentEncodedPath
        if let query = components.percentEncodedQuery, !query.isEmpty {
            requestTarget += "?" + query
        }
        let hostHeader = target.port == 443 ? target.host : "\(target.host):\(target.port)"
        let fields = [requestTarget, hostHeader, userAgent]
        // CR, LF, NUL or spaces in the request target would let a value inject header lines.
        let forbidden = CharacterSet(charactersIn: "\r\n\0")
        guard fields.allSatisfy({ $0.rangeOfCharacter(from: forbidden) == nil }),
              !requestTarget.contains(" ")
        else { return .failure(.invalidCharacters) }

        let lines = [
            "GET \(requestTarget) HTTP/1.1",
            "Host: \(hostHeader)",
            "Accept: application/json",
            "Accept-Encoding: identity",
            "Connection: close",
            "User-Agent: \(userAgent)",
        ]
        return .success(Data((lines.joined(separator: "\r\n") + "\r\n\r\n").utf8))
    }

    /// TLS over TCP, forced onto the cellular interface (design F4). Default TLS trust
    /// evaluation; the SNI comes from the endpoint's host name.
    static func cellularParameters() -> NWParameters {
        let parameters = NWParameters(tls: NWProtocolTLS.Options(), tcp: NWProtocolTCP.Options())
        parameters.requiredInterfaceType = .cellular
        // Cellular is an expensive path; it must stay allowed for this one request.
        parameters.prohibitExpensivePaths = false
        parameters.prohibitConstrainedPaths = false
        return parameters
    }
}

/// Transport failure of one weather attempt.
struct WeatherTransportError: Error, Equatable {
    enum Kind: Equatable {
        /// No route on this transport (unsatisfied path, cellular off, connect failed).
        case unreachable
        case timeout
        /// The server answered, but not with a usable HTTP response.
        case protocolError
    }

    let kind: Kind
    let message: String
}

/// One hand-written HTTP/1.1 GET over `NWConnection` with `requiredInterfaceType = .cellular`.
/// Stateless apart from its serial queue, so it is safe to share.
final class CellularHTTPClient: @unchecked Sendable {
    private let queue = DispatchQueue(label: "openflight.cellular-http", qos: .userInitiated)

    func get(_ url: URL, userAgent: String, timeout: TimeInterval) async throws -> HTTPResponse {
        let request: Data
        let target: HTTPRequestBuilder.Target
        switch (HTTPRequestBuilder.get(url, userAgent: userAgent), HTTPRequestBuilder.target(for: url)) {
        case (.success(let bytes), .success(let value)):
            request = bytes
            target = value
        default:
            throw WeatherTransportError(kind: .protocolError, message: "cannot build request for \(url)")
        }
        guard let port = NWEndpoint.Port(rawValue: target.port) else {
            throw WeatherTransportError(kind: .protocolError, message: "bad port")
        }
        let connection = NWConnection(
            host: NWEndpoint.Host(target.host), port: port, using: HTTPRequestBuilder.cellularParameters()
        )
        let exchange = Exchange(connection: connection, request: request, queue: queue)
        return try await withTaskCancellationHandler {
            try await exchange.run(timeout: timeout)
        } onCancel: {
            exchange.cancel()
        }
    }

    /// One connection's lifecycle; resumes its continuation exactly once.
    /// All mutable state is touched only on `queue`.
    private final class Exchange: @unchecked Sendable {
        private let connection: NWConnection
        private let request: Data
        private let queue: DispatchQueue
        private var buffer = Data()
        private var continuation: CheckedContinuation<HTTPResponse, Error>?
        private var finished = false

        init(connection: NWConnection, request: Data, queue: DispatchQueue) {
            self.connection = connection
            self.request = request
            self.queue = queue
        }

        func run(timeout: TimeInterval) async throws -> HTTPResponse {
            try await withCheckedThrowingContinuation { continuation in
                queue.async {
                    // A cancel that ran first already finished this exchange.
                    guard !self.finished else {
                        continuation.resume(throwing: CancellationError())
                        return
                    }
                    self.continuation = continuation
                    self.start(timeout: timeout)
                }
            }
        }

        func cancel() {
            queue.async {
                self.finish(.failure(WeatherTransportError(kind: .unreachable, message: "cancelled")))
            }
        }

        private func start(timeout: TimeInterval) {
            guard !finished else { return }
            queue.asyncAfter(deadline: .now() + timeout) { [weak self] in
                self?.finish(.failure(WeatherTransportError(
                    kind: .timeout, message: "cellular request timed out after \(Int(timeout)) s"
                )))
            }
            connection.stateUpdateHandler = { [weak self] state in
                guard let self else { return }
                switch state {
                case .ready:
                    self.send()
                case .waiting(let error):
                    // With a required interface, "waiting" means that interface has no route now;
                    // the default-route fallback is the better bet than waiting for it.
                    let reason = self.connection.currentPath?.unsatisfiedReason == .cellularDenied
                        ? "cellular data is switched off for this app"
                        : "no cellular route (\(error.localizedDescription))"
                    self.finish(.failure(WeatherTransportError(kind: .unreachable, message: reason)))
                case .failed(let error):
                    self.finish(.failure(WeatherTransportError(
                        kind: .unreachable, message: "cellular connection failed: \(error.localizedDescription)"
                    )))
                default:
                    break
                }
            }
            connection.start(queue: queue)
        }

        private func send() {
            connection.send(content: request, completion: .contentProcessed { [weak self] error in
                guard let self else { return }
                if let error {
                    self.finish(.failure(WeatherTransportError(
                        kind: .unreachable, message: "cellular send failed: \(error.localizedDescription)"
                    )))
                    return
                }
                self.receive()
            })
        }

        private func receive() {
            connection.receive(minimumIncompleteLength: 1, maximumLength: 64 * 1024) { [weak self] data, _, isComplete, error in
                guard let self, !self.finished else { return }
                if let data { self.buffer.append(data) }
                let atEOF = isComplete || error != nil
                switch HTTPResponseParser.parse(self.buffer, atEOF: atEOF) {
                case .complete(let response):
                    self.finish(.success(response))
                case .failure(let parseError):
                    let message = error.map { "cellular receive failed: \($0.localizedDescription)" }
                        ?? "bad HTTP response: \(parseError)"
                    self.finish(.failure(WeatherTransportError(kind: .protocolError, message: message)))
                case .incomplete:
                    self.receive()
                }
            }
        }

        /// Runs on `queue`.
        private func finish(_ result: Result<HTTPResponse, Error>) {
            guard !finished else { return }
            finished = true
            connection.stateUpdateHandler = nil
            connection.cancel()
            let continuation = self.continuation
            self.continuation = nil
            continuation?.resume(with: result)
        }
    }
}
