import Foundation

/// The `audio_onset` and `audio_status` event payloads (design §9.8). Pure.
enum AudioPayloads {
    static let version = 1

    /// One `audio_onset`. Host times come from `fit`; `onset_host_us` has the input latency
    /// subtracted, `onset_host_us_raw` does not.
    static func onset(
        _ onset: DetectedOnset, context: CaptureContext, fit: HostTimeFit, clock: ClockPair,
        snippetFile: String?, timeZone: TimeZone = .current
    ) -> JSONValue {
        let fs = context.sampleRate
        func hostUs(_ position: Double?) -> Int64? {
            guard let position, let host = fit.hostMicroseconds(at: position), host.isFinite else { return nil }
            return Int64(host.rounded())
        }
        let raw = hostUs(onset.refined.position)
        let corrected = raw.map { $0 - context.inputLatencyMicroseconds }
        let features = onset.features
        let fat = features.fatCue
        let second = features.secondTransient

        var fields = ClockFields.fields(clock, timeZone: timeZone)
        fields["type"] = .string("audio_onset")
        fields["v"] = .integer(version)
        fields["capture_id"] = .string(context.captureId)
        fields["seq"] = .integer(onset.seq)
        fields["onset_host_us"] = .integer(corrected)
        fields["onset_host_us_raw"] = .integer(raw)
        fields["input_latency_us"] = .integer(context.inputLatencyMicroseconds)
        fields["onset_epoch_ms"] = .number(corrected.map { clock.epochMilliseconds(forHostMicroseconds: $0) })
        fields["onset_sample"] = .number(onset.refined.position)
        fields["onset_uncertainty_us"] = .integer(Int64((onset.refined.uncertaintySamples / fs * 1e6).rounded()))
        fields["sample_rate_hz"] = .number(fs)
        fields["sample_rate_eff_hz"] = .number(fit.model?.sampleRateEffectiveHz)
        fields["host_fit_rms_us"] = .number(fit.model?.rmsMicroseconds)
        fields["detection"] = .object([
            "snr_db": .number(features.snrDb),
            "peak_dbfs": .number(features.peakDbfs),
            "clipped": .bool(features.clipped),
            "rise_time_ms": .number(features.riseTimeMs),
            "crest_db": .number(features.crestDb),
        ])
        fields["features"] = .object([
            "band_db": .object([
                "low": .number(features.bandLowDb),
                "mid": .number(features.bandMidDb),
                "high": .number(features.bandHighDb),
            ]),
            "centroid_hz_0_2ms": .number(features.centroidHz0to2ms),
            "centroid_hz_2_20ms": .number(features.centroidHz2to20ms),
            "decay_ms_20db": .number(features.decayMs20dB),
            "post_low_db": .number(features.postLowDb),
            "second_transient": .object([
                "detected": .bool(second.detected),
                "delay_ms": .number(second.delayMs),
                "rel_db": .number(second.relDb),
            ]),
        ])
        // The candidate's values are reported whether or not it passed: log-only, for tuning.
        fields["fat_cue"] = .object([
            "detected": .bool(fat.detected),
            "lead_ms": .number(fat.leadMs),
            "precursor_host_us": .integer(hostUs(fat.precursorPosition).map { $0 - context.inputLatencyMicroseconds }),
            "precursor_snr_db": .number(fat.precursorSnrDb),
            "precursor_rel_db": .number(fat.precursorRelDb),
            "precursor_centroid_hz": .number(fat.precursorCentroidHz),
            "pre_low_db_1_10ms": .number(features.preLowRelDb1to10ms),
        ])
        fields["snippet_file"] = .text(snippetFile)
        fields["detector_version"] = .string(OnsetConfig.detectorVersion)
        return .object(fields)
    }
}

/// Everything `audio_status` and the `audio.*` replies report (design §9.8). Assembled by the
/// capture controller, serialised here.
struct AudioStatus: Equatable, JSONRepresentable {
    enum State: String {
        case idle, starting, running, interrupted, error
    }

    enum Permission: String {
        case granted, denied, undetermined
    }

    struct Session: Equatable {
        var sampleRateHz: Double?
        var ioBufferMs: Double?
        var inputLatencyMs: Double?
        var inputPort: String?
        var inputDataSource: String?
        var category: String?
        var mode: String?
    }

    var state: State = .idle
    var permission: Permission = .undetermined
    var captureId: String?
    var restarted = false
    var startedEpochMs: Double?
    var session = Session()
    var debugCapture = false
    var onsets = 0
    var lastSeq = 0
    var droppedFrames: Int64 = 0
    var deviceModel = "unknown"
    var error: BridgeError?
    var clock = ClockPair(hostMicroseconds: 0, epochMilliseconds: 0, bracketMicroseconds: 0)
    var timeZone: TimeZone = .current

    /// `getSensors().audio` (design §7.3).
    var summary: SensorSnapshot.Audio {
        SensorSnapshot.Audio(state: state.rawValue, captureId: captureId, lastSeq: lastSeq)
    }

    var json: JSONValue {
        var fields = ClockFields.fields(clock, timeZone: timeZone)
        fields["type"] = .string("audio_status")
        fields["v"] = .integer(AudioPayloads.version)
        fields["state"] = .string(state.rawValue)
        fields["permission"] = .string(permission.rawValue)
        fields["capture_id"] = .text(captureId)
        fields["restarted"] = .bool(restarted)
        fields["started_epoch_ms"] = .number(startedEpochMs)
        fields["sample_rate_hz"] = .number(session.sampleRateHz)
        fields["io_buffer_ms"] = .number(session.ioBufferMs)
        fields["input_latency_ms"] = .number(session.inputLatencyMs)
        fields["input_port"] = .text(session.inputPort)
        fields["input_data_source"] = .text(session.inputDataSource)
        fields["session_category"] = .text(session.category)
        fields["session_mode"] = .text(session.mode)
        fields["debug_capture"] = .bool(debugCapture)
        fields["onsets"] = .integer(onsets)
        fields["last_seq"] = .integer(lastSeq)
        fields["dropped_frames"] = .integer(droppedFrames)
        fields["thresholds"] = OnsetConfig.v1.thresholds
        fields["detector_version"] = .string(OnsetConfig.detectorVersion)
        fields["device_model"] = .string(deviceModel)
        fields["error"] = error.map { .object(["code": .string($0.code.rawValue), "message": .string($0.message)]) } ?? .null
        return .object(fields)
    }
}
