import Foundation

/// The phone host clock: `CLOCK_UPTIME_RAW`, the same base as `mach_absolute_time()` and
/// `AVAudioTime` host time (design F14). It does not run during sleep and is never adjusted.
enum HostClock {
    static func nowNanoseconds() -> UInt64 {
        clock_gettime_nsec_np(CLOCK_UPTIME_RAW)
    }

    static func nowMicroseconds() -> Int64 {
        Int64(nowNanoseconds() / 1_000)
    }

    /// `CMLogItem.timestamp` (seconds since boot) as host µs. The design assumes this is the
    /// `CLOCK_UPTIME_RAW` base; device check §12.3 #7 verifies it.
    static func microseconds(fromUptimeSeconds seconds: TimeInterval) -> Int64? {
        guard seconds.isFinite, seconds >= 0 else { return nil }
        return Int64((seconds * 1_000_000).rounded())
    }
}

/// One host↔wall reading (design §7.1): host `t_a`, then wall time, then host `t_b`.
struct ClockPair: Equatable, JSONRepresentable {
    /// Midpoint of the two host reads, integer µs on `CLOCK_UPTIME_RAW`.
    let hostMicroseconds: Int64
    /// Unix wall time in ms with µs resolution (3 decimals).
    let epochMilliseconds: Double
    /// `t_b − t_a` in µs: how tightly the wall read is bracketed.
    let bracketMicroseconds: Int64

    /// Builds a pair from raw reads; pure, so tests can check the arithmetic.
    init(hostBeforeNs: UInt64, wallSeconds: Int, wallNanoseconds: Int, hostAfterNs: UInt64) {
        let before = Int64(hostBeforeNs)
        let after = Int64(max(hostAfterNs, hostBeforeNs))
        // (a + b) / 2 without overflowing: a + (b − a) / 2.
        hostMicroseconds = (before + (after - before) / 2) / 1_000
        bracketMicroseconds = (after - before) / 1_000
        let milliseconds = Double(wallSeconds) * 1_000 + Double(wallNanoseconds / 1_000) / 1_000
        epochMilliseconds = (milliseconds * 1_000).rounded() / 1_000
    }

    init(hostMicroseconds: Int64, epochMilliseconds: Double, bracketMicroseconds: Int64) {
        self.hostMicroseconds = hostMicroseconds
        self.epochMilliseconds = epochMilliseconds
        self.bracketMicroseconds = bracketMicroseconds
    }

    /// Reads the clocks now.
    static func capture() -> ClockPair {
        let before = HostClock.nowNanoseconds()
        var wall = timespec()
        clock_gettime(CLOCK_REALTIME, &wall)
        let after = HostClock.nowNanoseconds()
        return ClockPair(
            hostBeforeNs: before,
            wallSeconds: Int(wall.tv_sec),
            wallNanoseconds: Int(wall.tv_nsec),
            hostAfterNs: after
        )
    }

    /// Wall time in ms for a host time, through this pair.
    func epochMilliseconds(forHostMicroseconds host: Int64) -> Double {
        let value = epochMilliseconds + Double(host - hostMicroseconds) / 1_000
        return (value * 1_000).rounded() / 1_000
    }

    /// Integer Unix ms, the `epoch_ms` field `parse_client_clock()` reads.
    var epochMillisecondsInteger: Int64 {
        Int64(epochMilliseconds.rounded(.down))
    }

    var json: JSONValue {
        .object([
            "host_us": .integer(hostMicroseconds),
            "epoch_ms": .number(epochMilliseconds),
            "bracket_us": .integer(bracketMicroseconds),
        ])
    }
}

/// The clock fields every payload carries (design §7.1).
enum ClockFields {
    /// Local time minus UTC in minutes, e.g. +60 for CET, as `clientClockPayload()` sends it.
    static func tzOffsetMinutes(at epochMilliseconds: Double, timeZone: TimeZone = .current) -> Int {
        let date = Date(timeIntervalSince1970: epochMilliseconds / 1_000)
        return timeZone.secondsFromGMT(for: date) / 60
    }

    /// `epoch_ms`, `tz_offset_min` and `clock` for a payload built now.
    static func fields(_ pair: ClockPair, timeZone: TimeZone = .current) -> [String: JSONValue] {
        [
            "epoch_ms": .integer(pair.epochMillisecondsInteger),
            "tz_offset_min": .integer(tzOffsetMinutes(at: pair.epochMilliseconds, timeZone: timeZone)),
            "clock": pair.json,
        ]
    }
}
