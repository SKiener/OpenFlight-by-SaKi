import Foundation

/// Wire protocol of `window.openflightNative` (design §7). Pure: no WebKit, no sensors.
enum BridgeProtocol {
    /// Bumped on any breaking change to the JS API.
    static let version = 1
    /// `window.webkit.messageHandlers.<name>`.
    static let handlerName = "openflight"
    /// `window.dispatchEvent(new CustomEvent(<name>, { detail }))` for native → web events.
    static let eventName = "openflight-native"
}

/// Error codes of the reply envelope (design §7.2).
enum BridgeErrorCode: String, CaseIterable {
    case badRequest = "bad_request"
    case forbidden
    case permissionDenied = "permission_denied"
    case unavailable
    case timeout
    case network
    case httpError = "http_error"
    case parseError = "parse_error"
    case busy
    case `internal`
}

struct BridgeError: Error, Equatable {
    let code: BridgeErrorCode
    let message: String

    init(_ code: BridgeErrorCode, _ message: String) {
        self.code = code
        self.message = message
    }
}

/// Every command the page may send.
enum BridgeCommand: String, CaseIterable {
    case getSensors
    case fetchWeather
    case setKeepAwake
    case audioStart = "audio.start"
    case audioStop = "audio.stop"
    case audioStatus = "audio.status"
    case audioOnsets = "audio.onsets"
    case audioExportCaptures = "audio.exportCaptures"
    case motionStart = "motion.start"
    case motionSamples = "motion.samples"
    case motionStop = "motion.stop"
    case speechSpeak = "speech.speak"
    case speechCancel = "speech.cancel"
}

/// A parsed `postMessage({ v, cmd, args })` body.
struct BridgeRequest {
    let command: BridgeCommand
    /// `args` as WebKit delivered it (Foundation objects); empty when the page sent none.
    let arguments: [String: Any]

    static func parse(_ body: Any?) -> Result<BridgeRequest, BridgeError> {
        guard let dict = body as? [String: Any] else {
            return .failure(BridgeError(.badRequest, "message body must be an object"))
        }
        guard let version = FoundationReader.number(dict["v"]), version == Double(BridgeProtocol.version) else {
            return .failure(BridgeError(.badRequest, "unsupported bridge version"))
        }
        guard let name = FoundationReader.string(dict["cmd"]) else {
            return .failure(BridgeError(.badRequest, "cmd must be a string"))
        }
        guard let command = BridgeCommand(rawValue: name) else {
            return .failure(BridgeError(.badRequest, "unknown command \(name)"))
        }
        let rawArgs = dict["args"]
        if rawArgs == nil || rawArgs is NSNull {
            return .success(BridgeRequest(command: command, arguments: [:]))
        }
        guard let args = rawArgs as? [String: Any] else {
            return .failure(BridgeError(.badRequest, "args must be an object"))
        }
        return .success(BridgeRequest(command: command, arguments: args))
    }
}

/// Reads optional, bounded numbers out of `args`.
enum BridgeArguments {
    /// nil when absent or null; `bad_request` when present but not a finite number in range.
    static func optionalNumber(
        _ args: [String: Any], _ key: String, in range: ClosedRange<Double>
    ) -> Result<Double?, BridgeError> {
        guard let raw = args[key], !(raw is NSNull) else { return .success(nil) }
        guard let value = FoundationReader.number(raw), range.contains(value) else {
            return .failure(BridgeError(.badRequest, "\(key) must be a number in \(range.lowerBound)…\(range.upperBound)"))
        }
        return .success(value)
    }

    static func requiredNumber(
        _ args: [String: Any], _ key: String, in range: ClosedRange<Double>
    ) -> Result<Double, BridgeError> {
        switch optionalNumber(args, key, in: range) {
        case .failure(let error): return .failure(error)
        case .success(nil): return .failure(BridgeError(.badRequest, "\(key) is required"))
        case .success(let value?): return .success(value)
        }
    }
}

struct GetSensorsArguments: Equatable {
    static let defaultTimeoutMs = 15_000.0
    static let timeoutRangeMs = 0.0...60_000.0

    /// Request a new fix when the cached one is older (or missing); nil = cached values only.
    let locationMaxAgeS: Double?
    let timeoutMs: Double

    static func parse(_ args: [String: Any]) -> Result<GetSensorsArguments, BridgeError> {
        Result {
            let maxAge = try BridgeArguments.optionalNumber(args, "location_max_age_s", in: 0...86_400).get()
            let timeout = try BridgeArguments.optionalNumber(args, "timeout_ms", in: timeoutRangeMs).get()
            return GetSensorsArguments(locationMaxAgeS: maxAge, timeoutMs: timeout ?? defaultTimeoutMs)
        }.mapError { $0 as? BridgeError ?? BridgeError(.badRequest, "\($0)") }
    }
}

struct FetchWeatherArguments: Equatable {
    /// Per transport attempt (cellular, then default route), as `OPEN_METEO_TIMEOUT_MS` in the web.
    static let defaultTimeoutMs = 10_000.0
    static let timeoutRangeMs = 1_000.0...30_000.0

    let latitude: Double
    let longitude: Double
    let timeoutMs: Double

    static func parse(_ args: [String: Any]) -> Result<FetchWeatherArguments, BridgeError> {
        Result {
            let latitude = try BridgeArguments.requiredNumber(args, "latitude", in: -90...90).get()
            let longitude = try BridgeArguments.requiredNumber(args, "longitude", in: -180...180).get()
            let timeout = try BridgeArguments.optionalNumber(args, "timeout_ms", in: timeoutRangeMs).get()
            return FetchWeatherArguments(
                latitude: latitude, longitude: longitude, timeoutMs: timeout ?? defaultTimeoutMs
            )
        }.mapError { $0 as? BridgeError ?? BridgeError(.badRequest, "\($0)") }
    }
}

struct SetKeepAwakeArguments: Equatable {
    let on: Bool

    static func parse(_ args: [String: Any]) -> Result<SetKeepAwakeArguments, BridgeError> {
        guard let on = FoundationReader.bool(args["on"]) else {
            return .failure(BridgeError(.badRequest, "on must be a boolean"))
        }
        return .success(SetKeepAwakeArguments(on: on))
    }
}

struct AudioStartArguments: Equatable {
    /// Debug WAV capture for this page load; nil keeps the native setting.
    let debugCapture: Bool?

    static func parse(_ args: [String: Any]) -> Result<AudioStartArguments, BridgeError> {
        guard let raw = args["debug_capture"], !(raw is NSNull) else { return .success(AudioStartArguments(debugCapture: nil)) }
        guard let value = FoundationReader.bool(raw) else {
            return .failure(BridgeError(.badRequest, "debug_capture must be a boolean"))
        }
        return .success(AudioStartArguments(debugCapture: value))
    }
}

struct AudioOnsetsArguments: Equatable {
    let captureId: String
    let afterSeq: Int

    static func parse(_ args: [String: Any]) -> Result<AudioOnsetsArguments, BridgeError> {
        guard let captureId = FoundationReader.string(args["capture_id"]), !captureId.isEmpty, captureId.count <= 64 else {
            return .failure(BridgeError(.badRequest, "capture_id must be a non-empty string"))
        }
        switch BridgeArguments.requiredNumber(args, "after_seq", in: 0...Double(JSONValue.maxSafeInteger)) {
        case .failure(let error):
            return .failure(error)
        case .success(let value):
            guard value == value.rounded() else {
                return .failure(BridgeError(.badRequest, "after_seq must be an integer"))
            }
            return .success(AudioOnsetsArguments(captureId: captureId, afterSeq: Int(value)))
        }
    }
}

struct MotionSamplesArguments: Equatable {
    let afterSeq: Int

    static func parse(_ args: [String: Any]) -> Result<MotionSamplesArguments, BridgeError> {
        switch BridgeArguments.requiredNumber(args, "after_seq", in: 0...Double(JSONValue.maxSafeInteger)) {
        case .failure(let error):
            return .failure(error)
        case .success(let value):
            guard value == value.rounded() else {
                return .failure(BridgeError(.badRequest, "after_seq must be an integer"))
            }
            return .success(MotionSamplesArguments(afterSeq: Int(value)))
        }
    }
}

struct SpeechSpeakArguments: Equatable {
    /// A callout is a few values ("Carry 151 Meter, Ballgeschwindigkeit 193"); this only bounds a bad call.
    static let maxTextLength = 500
    /// A BCP 47 tag as the page sends it (`de-DE`, `en-US`, `es-ES`): language, then up to three subtags.
    static let languagePattern = "^[A-Za-z]{2,3}(-[A-Za-z0-9]{1,8}){0,3}$"

    let text: String
    let lang: String
    /// Cut off what is still speaking first (the menu's test button); else queue behind it.
    let interrupt: Bool

    static func parse(_ args: [String: Any]) -> Result<SpeechSpeakArguments, BridgeError> {
        guard let raw = FoundationReader.string(args["text"]) else {
            return .failure(BridgeError(.badRequest, "text must be a string"))
        }
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, text.count <= maxTextLength else {
            return .failure(BridgeError(.badRequest, "text must have 1…\(maxTextLength) characters"))
        }
        guard let lang = FoundationReader.string(args["lang"]),
              lang.range(of: languagePattern, options: .regularExpression) != nil
        else {
            return .failure(BridgeError(.badRequest, "lang must be a BCP 47 language tag"))
        }
        var interrupt = false
        if let rawInterrupt = args["interrupt"], !(rawInterrupt is NSNull) {
            guard let value = FoundationReader.bool(rawInterrupt) else {
                return .failure(BridgeError(.badRequest, "interrupt must be a boolean"))
            }
            interrupt = value
        }
        return .success(SpeechSpeakArguments(text: text, lang: lang, interrupt: interrupt))
    }
}

/// The reply every command gets: `{ ok: true, result }` or `{ ok: false, error: { code, message } }`.
enum BridgeEnvelope {
    static func success(_ result: JSONValue) -> JSONValue {
        .object(["ok": .bool(true), "result": result])
    }

    static func failure(_ error: BridgeError) -> JSONValue {
        .object([
            "ok": .bool(false),
            "error": .object(["code": .string(error.code.rawValue), "message": .string(error.message)]),
        ])
    }

    static func from(_ result: Result<JSONValue, BridgeError>) -> JSONValue {
        switch result {
        case .success(let value): return success(value)
        case .failure(let error): return failure(error)
        }
    }
}

/// Fixed hardware/feature presence for one app run (design §7.2 `capabilities`).
struct BridgeCapabilities: Equatable, JSONRepresentable {
    var barometer: Bool
    var absoluteAltitude: Bool
    var location: Bool
    var weather: Bool
    /// `AVAudioSession.isInputAvailable` at launch.
    var audio: Bool
    var keepAwake: Bool
    var debugCapture: Bool
    /// Accelerometer for the TI tilt calibration (`motion.*`).
    var motion: Bool
    /// Spoken shot callouts (`speech.*`).
    var speech: Bool

    var json: JSONValue {
        .object([
            "barometer": .bool(barometer),
            "absolute_altitude": .bool(absoluteAltitude),
            "location": .bool(location),
            "weather": .bool(weather),
            "audio": .bool(audio),
            "keep_awake": .bool(keepAwake),
            "debug_capture": .bool(debugCapture),
            "motion": .bool(motion),
            "speech": .bool(speech),
        ])
    }
}

/// The values the injected shim is templated with.
struct BridgeConfig: Equatable, JSONRepresentable {
    let appVersion: String
    let capabilities: BridgeCapabilities

    var json: JSONValue {
        .object([
            "version": .int(Int64(BridgeProtocol.version)),
            "app_version": .string(appVersion),
            "platform": .string("ios"),
            "handler": .string(BridgeProtocol.handlerName),
            "event": .string(BridgeProtocol.eventName),
            "capabilities": capabilities.json,
        ])
    }
}

/// Which page may use the bridge: the configured base origin, main frame only (design §6).
enum OriginPolicy {
    static func defaultPort(forScheme scheme: String) -> Int? {
        switch scheme.lowercased() {
        case "http": return 80
        case "https": return 443
        default: return nil
        }
    }

    /// `port` 0 or nil means the scheme's default, as `WKSecurityOrigin.port` reports it.
    static func matches(scheme: String, host: String, port: Int?, base: URL) -> Bool {
        guard let baseScheme = base.scheme?.lowercased(), let baseHost = base.host?.lowercased(),
              let basePort = base.port ?? defaultPort(forScheme: baseScheme)
        else { return false }
        let scheme = scheme.lowercased()
        guard scheme == baseScheme, host.lowercased() == baseHost else { return false }
        let effective = (port == nil || port == 0) ? defaultPort(forScheme: scheme) : port
        return effective == basePort
    }

    static func matches(url: URL, base: URL) -> Bool {
        guard let scheme = url.scheme, let host = url.host else { return false }
        return matches(scheme: scheme, host: host, port: url.port, base: base)
    }
}
