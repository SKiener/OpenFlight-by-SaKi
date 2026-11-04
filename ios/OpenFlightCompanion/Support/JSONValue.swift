import Foundation

/// A JSON value as the bridge sends it to the page.
///
/// Payloads are built from this type instead of `Codable` so that an unknown value is an
/// explicit `null` (the design's §7.1 rule) and a non-finite number can never reach
/// JavaScript: `number(_:)` turns NaN and ±Inf into `null`.
enum JSONValue: Equatable {
    case null
    case bool(Bool)
    case int(Int64)
    case double(Double)
    case string(String)
    case array([JSONValue])
    case object([String: JSONValue])

    /// Largest integer a JS number holds exactly (2^53 − 1).
    static let maxSafeInteger: Int64 = 9_007_199_254_740_991

    /// A float, or `null` when it is missing or not finite.
    static func number(_ value: Double?) -> JSONValue {
        guard let value, value.isFinite else { return .null }
        return .double(value)
    }

    /// An integer, or `null` when it is missing or outside the exact JS integer range.
    static func integer(_ value: Int64?) -> JSONValue {
        guard let value, abs(value) <= maxSafeInteger else { return .null }
        return .int(value)
    }

    static func integer(_ value: Int?) -> JSONValue {
        integer(value.map(Int64.init))
    }

    static func text(_ value: String?) -> JSONValue {
        value.map(JSONValue.string) ?? .null
    }

    static func flag(_ value: Bool?) -> JSONValue {
        value.map(JSONValue.bool) ?? .null
    }

    /// Foundation objects for `WKScriptMessageHandlerWithReply` replies and `JSONSerialization`.
    var foundationObject: Any {
        switch self {
        case .null: return NSNull()
        case .bool(let value): return NSNumber(value: value)
        case .int(let value): return NSNumber(value: value)
        case .double(let value): return value.isFinite ? NSNumber(value: value) : NSNull()
        case .string(let value): return value
        case .array(let values): return values.map(\.foundationObject)
        case .object(let fields): return fields.mapValues(\.foundationObject)
        }
    }

    /// Compact JSON text with sorted keys (deterministic, also a valid JS expression).
    func jsonString() -> String {
        // Top-level scalars are allowed with .fragmentsAllowed.
        let data = (try? JSONSerialization.data(
            withJSONObject: foundationObject,
            options: [.sortedKeys, .fragmentsAllowed, .withoutEscapingSlashes]
        )) ?? Data("null".utf8)
        return String(decoding: data, as: UTF8.self)
    }

    subscript(key: String) -> JSONValue? {
        if case .object(let fields) = self { return fields[key] }
        return nil
    }
}

/// Types that the bridge sends to the page.
protocol JSONRepresentable {
    var json: JSONValue { get }
}

/// Reads values out of the Foundation objects WebKit hands over for a `postMessage` body.
///
/// JavaScript booleans arrive as `NSNumber` too, so every number read rejects them.
enum FoundationReader {
    static func isBoolean(_ value: Any) -> Bool {
        guard let number = value as? NSNumber else { return value is Bool }
        return CFGetTypeID(number) == CFBooleanGetTypeID()
    }

    /// A finite number that is not a boolean, else nil.
    static func number(_ value: Any?) -> Double? {
        guard let value, !(value is NSNull), !isBoolean(value) else { return nil }
        guard let number = value as? NSNumber else { return nil }
        let double = number.doubleValue
        return double.isFinite ? double : nil
    }

    static func bool(_ value: Any?) -> Bool? {
        guard let value, isBoolean(value) else { return nil }
        return (value as? NSNumber)?.boolValue ?? (value as? Bool)
    }

    static func string(_ value: Any?) -> String? {
        value as? String
    }
}
