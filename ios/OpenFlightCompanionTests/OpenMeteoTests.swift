import XCTest
@testable import OpenFlightCompanion

final class OpenMeteoTests: XCTestCase {
    /// Shape of the live probe in design F18 (request 48.1374, 11.5755; grid-snapped answer).
    static let fixture = """
    {"latitude":48.12743,"longitude":11.576928,"generationtime_ms":0.05,"utc_offset_seconds":0,
     "timezone":"GMT","timezone_abbreviation":"GMT","elevation":524.0,
     "current_units":{"time":"unixtime","interval":"seconds","temperature_2m":"°C",
       "relative_humidity_2m":"%","surface_pressure":"hPa","pressure_msl":"hPa",
       "wind_speed_10m":"m/s","wind_direction_10m":"°"},
     "current":{"time":1793001600,"interval":900,"temperature_2m":9.4,"relative_humidity_2m":81,
       "surface_pressure":954.6,"pressure_msl":1016.2,"wind_speed_10m":2.3,"wind_direction_10m":245}}
    """

    private func context(_ transport: WeatherTransportKind = .cellular) -> OpenMeteo.Context {
        OpenMeteo.Context(
            latitude: 48.1374, longitude: 11.5755, phonePressureHpa: 953.9, transport: transport,
            fetchMs: 412.37,
            clock: ClockPair(hostMicroseconds: 5_000_000, epochMilliseconds: 1_793_001_700_000.5, bracketMicroseconds: 3),
            timeZone: TimeZone(identifier: "Europe/Berlin")!
        )
    }

    private func parse(_ text: String) -> Result<NativeWeatherReport, BridgeError> {
        OpenMeteo.parse(Data(text.utf8), context: context())
    }

    // MARK: URL

    func testForecastURLIsExact() {
        XCTAssertEqual(
            OpenMeteo.forecastURL(latitude: 48.1374, longitude: 11.5755).absoluteString,
            "https://api.open-meteo.com/v1/forecast?latitude=48.13740&longitude=11.57550"
                + "&current=temperature_2m,relative_humidity_2m,surface_pressure,pressure_msl,wind_speed_10m,wind_direction_10m"
                + "&wind_speed_unit=ms&timeformat=unixtime&timezone=GMT"
        )
    }

    func testCoordinatesAreFormattedWithFiveDecimalsAndNoLocale() {
        XCTAssertEqual(OpenMeteo.formatCoordinate(-33.868819), "-33.86882")
        XCTAssertEqual(OpenMeteo.formatCoordinate(0), "0.00000")
        XCTAssertEqual(OpenMeteo.formatCoordinate(151.2093), "151.20930")
    }

    func testSameDensityVariablesAsTheWebClient() {
        // wt-weather ui/src/services/openMeteo.ts CURRENT_FIELDS.
        let web = "temperature_2m,relative_humidity_2m,surface_pressure,wind_speed_10m,wind_direction_10m"
        XCTAssertTrue(Set(web.split(separator: ",").map(String.init)).isSubset(of: Set(OpenMeteo.currentVariables)))
        XCTAssertTrue(OpenMeteo.currentVariables.contains("surface_pressure"))
    }

    // MARK: Mapping

    func testFixtureMapsFieldByField() throws {
        let report = try parse(Self.fixture).get()
        let json = report.json
        XCTAssertEqual(json["source"], .string("open-meteo"))
        // Coordinates echo the request, not the grid point.
        XCTAssertEqual(json["latitude"], .double(48.1374))
        XCTAssertEqual(json["longitude"], .double(11.5755))
        XCTAssertEqual(json["grid_latitude"], .double(48.12743))
        XCTAssertEqual(json["grid_longitude"], .double(11.576928))
        XCTAssertEqual(json["temperature_c"], .double(9.4))
        XCTAssertEqual(json["pressure_hpa"], .double(954.6))          // surface_pressure, not MSL
        XCTAssertEqual(json["pressure_msl_hpa"], .double(1016.2))
        XCTAssertEqual(json["humidity_pct"], .double(81))
        XCTAssertEqual(json["wind_speed_ms"], .double(2.3))
        XCTAssertEqual(json["wind_direction_deg"], .double(245))
        XCTAssertEqual(json["observed_epoch_s"], .double(1_793_001_600))
        XCTAssertEqual(json["model_elevation_m"], .double(524))
        XCTAssertEqual(json["phone_pressure_hpa"], .double(953.9))
        XCTAssertEqual(json["transport"], .string("cellular"))
        XCTAssertEqual(json["fetch_ms"], .double(412.4))
        XCTAssertEqual(json["epoch_ms"], .int(1_793_001_700_000))
        XCTAssertEqual(json["tz_offset_min"], .int(60))
        XCTAssertEqual(json["clock"]?["host_us"], .int(5_000_000))
    }

    /// The keys `weather.py` parse_weather_report reads, with numeric values.
    func testReportCarriesEveryServerField() throws {
        let json = try parse(Self.fixture).get().json
        for key in ["temperature_c", "pressure_hpa", "humidity_pct", "wind_speed_ms", "wind_direction_deg",
                    "observed_epoch_s", "latitude", "longitude", "epoch_ms", "tz_offset_min"] {
            switch json[key] {
            case .double?, .int?: break
            default: XCTFail("\(key) missing or not a number")
            }
        }
    }

    func testDefaultTransportIsReported() throws {
        let report = try OpenMeteo.parse(Data(Self.fixture.utf8), context: context(.default)).get()
        XCTAssertEqual(report.json["transport"], .string("default"))
        XCTAssertEqual(report.json["source"], .string("open-meteo"), "source never carries the transport")
    }

    func testMissingOrNullVariableIsParseError() {
        for key in ["time", "temperature_2m", "relative_humidity_2m", "surface_pressure", "wind_speed_10m", "wind_direction_10m"] {
            for replacement in ["null", "\"9.4\"", "true"] {
                let broken = Self.fixture.replacingOccurrences(
                    of: #""\#(key)":"#, with: #""\#(key)":\#(replacement),"x_\#(key)":"#
                )
                // Only the `current` occurrence matters; `current_units` has string values anyway.
                guard case .failure(let error) = parse(broken) else {
                    return XCTFail("\(key) = \(replacement) accepted")
                }
                XCTAssertEqual(error.code, .parseError, key)
            }
            let removed = Self.fixture.replacingOccurrences(of: #""\#(key)":"#, with: #""gone_\#(key)":"#)
            guard case .failure(let error) = parse(removed) else { return XCTFail("missing \(key) accepted") }
            XCTAssertEqual(error.code, .parseError)
        }
    }

    func testMissingExtrasAreNull() throws {
        let minimal = """
        {"current":{"time":1793001600,"temperature_2m":9.4,"relative_humidity_2m":81,
          "surface_pressure":954.6,"wind_speed_10m":2.3,"wind_direction_10m":245}}
        """
        let json = try parse(minimal).get().json
        XCTAssertEqual(json["pressure_msl_hpa"], .null)
        XCTAssertEqual(json["model_elevation_m"], .null)
        XCTAssertEqual(json["grid_latitude"], .null)
        XCTAssertEqual(json["pressure_hpa"], .double(954.6))
    }

    func testNotJSONOrNoCurrentIsParseError() {
        for text in ["<html>", "[]", "{}", #"{"current":[]}"#] {
            guard case .failure(let error) = parse(text) else { return XCTFail("accepted \(text)") }
            XCTAssertEqual(error.code, .parseError)
        }
    }

    func testOpenMeteoErrorBodyIsHTTPError() {
        guard case .failure(let error) = parse(#"{"error":true,"reason":"Latitude must be in range"}"#) else {
            return XCTFail("error body accepted")
        }
        XCTAssertEqual(error.code, .httpError)
        XCTAssertTrue(error.message.contains("Latitude"))
    }

    // MARK: Units

    func testPressureUnitsAreConvertedToHectopascals() throws {
        let kPa = Self.fixture
            .replacingOccurrences(of: #""surface_pressure":"hPa""#, with: #""surface_pressure":"kPa""#)
            .replacingOccurrences(of: #""surface_pressure":954.6"#, with: #""surface_pressure":95.46"#)
        XCTAssertEqual(try parse(kPa).get().pressureHpa, 954.6, accuracy: 1e-9)

        let pa = Self.fixture
            .replacingOccurrences(of: #""surface_pressure":"hPa""#, with: #""surface_pressure":"Pa""#)
            .replacingOccurrences(of: #""surface_pressure":954.6"#, with: #""surface_pressure":95460"#)
        XCTAssertEqual(try parse(pa).get().pressureHpa, 954.6, accuracy: 1e-9)
    }

    func testWindAndTemperatureUnitsAreConverted() throws {
        let kmh = Self.fixture
            .replacingOccurrences(of: #""wind_speed_10m":"m/s""#, with: #""wind_speed_10m":"km/h""#)
            .replacingOccurrences(of: #""wind_speed_10m":2.3"#, with: #""wind_speed_10m":36"#)
        XCTAssertEqual(try parse(kmh).get().windSpeedMs, 10, accuracy: 1e-9)

        let fahrenheit = Self.fixture
            .replacingOccurrences(of: #""temperature_2m":"°C""#, with: #""temperature_2m":"°F""#)
            .replacingOccurrences(of: #""temperature_2m":9.4"#, with: #""temperature_2m":50"#)
        XCTAssertEqual(try parse(fahrenheit).get().temperatureC, 10, accuracy: 1e-9)
    }

    func testUnknownUnitIsParseError() {
        let inHg = Self.fixture.replacingOccurrences(of: #""surface_pressure":"hPa""#, with: #""surface_pressure":"inHg""#)
        guard case .failure(let error) = parse(inHg) else { return XCTFail("inHg accepted") }
        XCTAssertEqual(error.code, .parseError)
        XCTAssertTrue(error.message.contains("inHg"))
    }

    func testUnitHelpers() {
        XCTAssertEqual(OpenMeteo.Unit.pressureHpa(1013.25, unit: nil), 1013.25)
        XCTAssertEqual(OpenMeteo.Unit.pressureHpa(101.325, unit: "kPa"), 1013.25)
        XCTAssertNil(OpenMeteo.Unit.pressureHpa(1, unit: "psi"))
        XCTAssertEqual(OpenMeteo.Unit.windMs(3.6, unit: "km/h")!, 1, accuracy: 1e-12)
        XCTAssertNil(OpenMeteo.Unit.percent(50, unit: "ratio"))
    }
}
