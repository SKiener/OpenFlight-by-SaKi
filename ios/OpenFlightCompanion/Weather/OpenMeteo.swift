import Foundation

/// Which path answered the weather request.
enum WeatherTransportKind: String {
    case cellular
    case `default`
}

/// The `fetchWeather()` result: a ready-to-emit `weather_report` for `weather.py`
/// `parse_weather_report` plus extras it ignores today (design §7.4).
struct NativeWeatherReport: JSONRepresentable {
    /// `SOURCE_OPEN_METEO` in `weather.py`; the server only accepts its known sources, so the
    /// transport goes in `transport`, never here.
    static let source = "open-meteo"

    /// Echo of the request, not Open-Meteo's grid point (design F18).
    var latitude: Double
    var longitude: Double
    var temperatureC: Double
    /// `current.surface_pressure` (station pressure, what air density needs).
    var pressureHpa: Double
    var humidityPct: Double
    var windSpeedMs: Double
    var windDirectionDeg: Double
    var observedEpochS: Double
    var pressureMslHpa: Double?
    var modelElevationM: Double?
    var gridLatitude: Double?
    var gridLongitude: Double?
    var phonePressureHpa: Double?
    var transport: WeatherTransportKind
    var fetchMs: Double
    /// Phone clock when the response was parsed.
    var clock: ClockPair
    var timeZone: TimeZone = .current

    var json: JSONValue {
        var fields = ClockFields.fields(clock, timeZone: timeZone)
        fields.merge([
            "source": .string(Self.source),
            "latitude": .number(latitude),
            "longitude": .number(longitude),
            "temperature_c": .number(temperatureC),
            "pressure_hpa": .number(pressureHpa),
            "humidity_pct": .number(humidityPct),
            "wind_speed_ms": .number(windSpeedMs),
            "wind_direction_deg": .number(windDirectionDeg),
            "observed_epoch_s": .number(observedEpochS),
            "pressure_msl_hpa": .number(pressureMslHpa),
            "model_elevation_m": .number(modelElevationM),
            "grid_latitude": .number(gridLatitude),
            "grid_longitude": .number(gridLongitude),
            "phone_pressure_hpa": .number(phonePressureHpa),
            "transport": .string(transport.rawValue),
            "fetch_ms": .number((fetchMs * 10).rounded() / 10),
        ]) { _, new in new }
        return .object(fields)
    }
}

/// Pure Open-Meteo request building and response mapping (design §7.4, F18).
enum OpenMeteo {
    static let host = "api.open-meteo.com"
    static let path = "/v1/forecast"
    /// The web client's `CURRENT_FIELDS` (wt-weather `ui/src/services/openMeteo.ts`) plus the
    /// `pressure_msl` extra. The density inputs are the same variables in both clients.
    static let currentVariables = [
        "temperature_2m", "relative_humidity_2m", "surface_pressure", "pressure_msl",
        "wind_speed_10m", "wind_direction_10m",
    ]

    /// 5 decimals, the precision `RangeLocation` stores; POSIX formatting, never localised.
    static func formatCoordinate(_ value: Double) -> String {
        String(format: "%.5f", locale: Locale(identifier: "en_US_POSIX"), value)
    }

    static func forecastURL(latitude: Double, longitude: Double) -> URL {
        var components = URLComponents()
        components.scheme = "https"
        components.host = host
        components.path = path
        components.queryItems = [
            URLQueryItem(name: "latitude", value: formatCoordinate(latitude)),
            URLQueryItem(name: "longitude", value: formatCoordinate(longitude)),
            URLQueryItem(name: "current", value: currentVariables.joined(separator: ",")),
            URLQueryItem(name: "wind_speed_unit", value: "ms"),
            URLQueryItem(name: "timeformat", value: "unixtime"),
            URLQueryItem(name: "timezone", value: "GMT"),
        ]
        // URLComponents leaves ',' unescaped in queries; Open-Meteo accepts it as-is.
        guard let url = components.url else { preconditionFailure("static Open-Meteo URL") }
        return url
    }

    /// Unit conversions to the payload units, by the label in `current_units`. A missing label
    /// means the unit we asked for. An unknown label is a `parse_error`: a silent wrong unit
    /// would corrupt air density.
    enum Unit {
        static func pressureHpa(_ value: Double, unit: String?) -> Double? {
            switch unit?.trimmingCharacters(in: .whitespaces) {
            case nil, "hPa", "hpa", "mbar", "mb": return value
            case "kPa", "kpa": return Pressure.hectopascals(fromKilopascals: value)
            case "Pa", "pa": return Pressure.hectopascals(fromPascals: value)
            default: return nil
            }
        }

        static func windMs(_ value: Double, unit: String?) -> Double? {
            switch unit?.trimmingCharacters(in: .whitespaces) {
            case nil, "m/s", "ms": return value
            case "km/h", "kmh": return value / 3.6
            case "mph": return value * 0.44704
            case "kn", "kt", "knots": return value * 0.514444
            default: return nil
            }
        }

        static func temperatureC(_ value: Double, unit: String?) -> Double? {
            switch unit?.trimmingCharacters(in: .whitespaces) {
            case nil, "°C", "C", "celsius": return value
            case "°F", "F", "fahrenheit": return (value - 32) * 5 / 9
            default: return nil
            }
        }

        static func percent(_ value: Double, unit: String?) -> Double? {
            switch unit?.trimmingCharacters(in: .whitespaces) {
            case nil, "%": return value
            default: return nil
            }
        }

        static func degrees(_ value: Double, unit: String?) -> Double? {
            switch unit?.trimmingCharacters(in: .whitespaces) {
            case nil, "°", "deg": return value
            default: return nil
            }
        }

        static func unixSeconds(_ value: Double, unit: String?) -> Double? {
            switch unit?.trimmingCharacters(in: .whitespaces) {
            case nil, "unixtime": return value
            default: return nil
            }
        }
    }

    struct Context {
        var latitude: Double
        var longitude: Double
        var phonePressureHpa: Double?
        var transport: WeatherTransportKind
        var fetchMs: Double
        var clock: ClockPair
        var timeZone: TimeZone = .current
    }

    static func parse(_ data: Data, context: Context) -> Result<NativeWeatherReport, BridgeError> {
        guard let root = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
            return .failure(BridgeError(.parseError, "Open-Meteo answered with no JSON object"))
        }
        if let reason = root["reason"] as? String, root["error"] as? Bool == true {
            return .failure(BridgeError(.httpError, "Open-Meteo error: \(reason)"))
        }
        guard let current = root["current"] as? [String: Any] else {
            return .failure(BridgeError(.parseError, "Open-Meteo answered without current weather"))
        }
        let units = root["current_units"] as? [String: Any] ?? [:]

        func field(_ key: String, _ convert: (Double, String?) -> Double?) -> Result<Double, BridgeError> {
            guard let raw = FoundationReader.number(current[key]) else {
                return .failure(BridgeError(.parseError, "current.\(key) missing or not a number"))
            }
            let unit = units[key] as? String
            guard let value = convert(raw, unit), value.isFinite else {
                return .failure(BridgeError(.parseError, "current.\(key) has unknown unit \(unit ?? "?")"))
            }
            return .success(value)
        }

        return Result {
            let observed = try field("time", Unit.unixSeconds).get()
            let temperature = try field("temperature_2m", Unit.temperatureC).get()
            let humidity = try field("relative_humidity_2m", Unit.percent).get()
            let pressure = try field("surface_pressure", Unit.pressureHpa).get()
            let windSpeed = try field("wind_speed_10m", Unit.windMs).get()
            let windDirection = try field("wind_direction_10m", Unit.degrees).get()
            // Extras: logged when present, never a reason to fail.
            let msl = try? field("pressure_msl", Unit.pressureHpa).get()
            return NativeWeatherReport(
                latitude: context.latitude,
                longitude: context.longitude,
                temperatureC: temperature,
                pressureHpa: pressure,
                humidityPct: humidity,
                windSpeedMs: windSpeed,
                windDirectionDeg: windDirection,
                observedEpochS: observed,
                pressureMslHpa: msl,
                modelElevationM: FoundationReader.number(root["elevation"]),
                gridLatitude: FoundationReader.number(root["latitude"]),
                gridLongitude: FoundationReader.number(root["longitude"]),
                phonePressureHpa: context.phonePressureHpa,
                transport: context.transport,
                fetchMs: context.fetchMs,
                clock: context.clock,
                timeZone: context.timeZone
            )
        }.mapError { $0 as? BridgeError ?? BridgeError(.parseError, "\($0)") }
    }
}
