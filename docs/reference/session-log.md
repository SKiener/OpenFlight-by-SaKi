---
icon: lucide/file-json
---

# Session Log Schema

Every session writes a JSON Lines file to
`~/openflight_sessions/session_<timestamp>.jsonl` (override with `--log-dir`,
disable with `--no-logging`).

One JSON object per line. Every entry carries at least:

```json
{"ts": "2026-08-23T14:31:07.882431", "type": "shot_detected", "...": "..."}
```

| Field | Meaning |
| --- | --- |
| `ts` | ISO 8601 local timestamp |
| `type` | Entry type, from the table below |

Everything else is type-specific and merged into the same object — entries are
flat, not nested under a payload key.

## Entry types

Written by `src/openflight/session_logger.py`.

### Session lifecycle

| Type | Written when |
| --- | --- |
| `session_start` | Session opens. Carries the session metadata block. |
| `session_end` | Session closes. Carries the summary. |
| `connection` | A radar connects — port, firmware, negotiated baud. |
| `ops_clock_sync` | OPS243 clock synchronisation summary. |
| `client_clock` | A kiosk or phone browser connected over Socket.IO and reported its own time once. Fields: `client` (`kiosk` = the Pi's own browser, `phone` = a paired device), `client_epoch_ms` (the browser's `Date.now()`), `client_tz_offset_min` (browser local time minus UTC, e.g. `120` for CEST; null if missing), `server_epoch` (Pi time in s when the report arrived), `server_tz_offset_min` (the Pi's local time minus UTC at that moment, e.g. `120` for CEST) and `offset_s` = client − Pi in seconds. `offset_s` is a UTC epoch difference (positive: the Pi clock is behind), while `ts` is the Pi's naive local time, so correct timestamps via UTC: `real_utc = (ts − server_tz_offset_min) + offset_s`, phone local time = `real_utc + client_tz_offset_min`. Do not add `offset_s` straight to `ts`: that is an hour off when a DST change lies between the Pi's date and the real date (on 2026-10-25 the Pi was on CEST, the phone on CET, so `offset_s` read 5 d 18 h 29 min for a 5 d 17 h 29 min wall-clock gap). For older logs without `server_tz_offset_min`, take it as this entry's `ts` minus `server_epoch` (as UTC), rounded to 15 min. The Pi has no NTP at the range, so this is how wrong session timestamps are corrected afterwards; above 120 s the kiosk log gets one `[CLOCK] Pi clock (UTC) differs from <client> by … (more than 120 s); correct session timestamps as described in docs/reference/session-log.md (client_clock)` warning per session (it points here instead of naming a correction: adding the offset to `ts` is wrong across DST) and Diagnose → Status shows the offset. The system time is never changed. |
| `config_change` | Runtime configuration changed, with its source. |
| `angle_reference` | The camera angle reference changed (`src/openflight/angle_reference.py`). `event`: `offset_validity_changed` (a session offset became valid or invalid with this shot's pair), `offset_reset` (`reason` `session_cleared`, `profile_switch` or `camera_geometry_changed`), `target_set`, `target_cleared`, `camera_geometry_changed` (the capture mode or the sensor window changed: between 640x400 and the 640x200 strip the target keeps its direction and the ball mark its spot, both re-projected into the new mode; otherwise both were dropped) or `ball_mark_outside_mode` (the ball mark fell outside the new mode's frame and was dropped alone). Carries the full state: `vertical` and `horizontal` (each `valid`, `offset_deg` = median of TI minus camera over the last `window` (20) pairs, `spread_deg` = 1.4826 x their median absolute deviation, `pairs`, `pairs_total`, `min_pairs` (5), `max_spread_deg` (1.0), `reset_reason`, `applied`: true for the vertical, false for the horizontal, which is logged only) and `target_line` (null, or `azimuth_deg` / `elevation_deg` in the camera frame, + is target-right / up, `frame_x_px` / `frame_y_px` in saved-frame pixels, `preview_x_pct` / `preview_y_pct` where it was tapped, `set_at` epoch s). `session_start` `config.angle_reference` holds the same state at the start, with a target restored from the last run. |
| `weather_location` | The range location was stored (Settings → Weather; persisted in `~/.config/openflight/weather.json`, or `--weather-path` / `OPENFLIGHT_WEATHER_PATH`). Fields: `name`, `latitude`, `longitude`, `country`, `admin1`. |
| `weather` | A weather update was accepted (`src/openflight/weather.py`). Fields: `source` (`open-meteo` = fetched by the paired phone from Open-Meteo for the stored location, `manual` = typed in Settings), `temperature_c`, `pressure_hpa` (surface pressure, not sea-level), `humidity_pct`, `humidity_assumed` (true when a manual entry left humidity out and 50 % was used), `wind_speed_ms` and `wind_direction_deg` (Open-Meteo only; logged, not used by the flight model), `air_density_kg_m3` (CIPM-2007 moist-air density), `observed_epoch_s` (observation time, real UTC: Open-Meteo's interval start, or the client's clock for a manual entry), `observed_pi_epoch_s` (the same moment on the Pi's clock, `observed_epoch_s − clock_offset_s`, the time base of `ts`), `age_at_receipt_s`, `client` (`phone`/`kiosk`), `client_epoch_ms`, `client_tz_offset_min`, `server_epoch`, `clock_offset_s` (client − Pi, as in `client_clock`) and `location` (the stored location for `open-meteo`, else null). Open-Meteo readings also carry `pressure_source` (`open-meteo` = Open-Meteo's surface pressure, `phone-barometer` = the station pressure the iOS companion's barometer measured at the phone, used only while its sample is at most 10 min old), `transport` (`cellular` = the companion's cellular fetch, `default` = its fallback over the default route, `browser` = the companion page's own fetch after the native fetch failed or was unavailable; null = Safari's page fetch, which sends its report unchanged, or an older page), and for a barometer pressure `open_meteo_pressure_hpa` (Open-Meteo's surface pressure of the same fetch) and `phone_pressure_age_s`; all null for `manual`. A reading flies the ballistics while its age (age on arrival plus Pi-monotonic time since) is at most 3 h; it is not kept across a server restart. |
| `weather_fetch_failed` | The paired phone's weather fetch or GPS range location failed (log only, never broadcast; `src/openflight/weather.py` `parse_weather_fetch_failure`). One entry per failed try, at most one per 5 s; a malformed report is dropped. Fields: `stage` (`location` = storing the phone's GPS fix as the range location, `fetch` = the Open-Meteo fetch, `send` = the Pi refused the `weather_report` or did not answer), `reason` (classified on the phone, `ui/src/services/weatherFailure.ts`: `offline` = no route to Open-Meteo, `timeout`, `http_<status>` = Open-Meteo's HTTP error, `invalid_response` = an answer without current weather, `rejected` = the Pi refused the report (its code is in the server log's `[WEATHER] Report rejected` line), `no_permission` = location services off or not allowed for the app, `no_fix` = no usable GPS fix in time, `save_failed` = the Pi did not store the GPS location, `unknown`; inside the iOS companion a failed native fetch gives the reason even when the page's own fallback fetch failed too), `consecutive_failures` (failures in a row, counted apart for `location` and for `fetch`/`send`; reset by a success), `next_retry_s` (the pause before the next try: 15 s after the first failure, doubling up to 300 s), and `client` (`phone`/`kiosk`). Both counts are integers clamped to 0–100000. No error text, URLs or coordinates. |
| `phone_sensors` | The iOS companion's web page forwarded a sensor snapshot (on connect and every 60 s; `src/openflight/phone_events.py`, design in `docs/ios-companion/design.md` §7.3). Log only. Fields: the snapshot as the app sends it (`clock` `{host_us, epoch_ms, bracket_us}`, `barometer` `{authorization, pressure_hpa, relative_altitude_m, sample_host_us, age_s}`, `absolute_altitude`, `location` `{authorization, accuracy_authorization, latitude, longitude, horizontal_accuracy_m, altitude_m, vertical_accuracy_m, fix_epoch_ms, age_s}`, `network` `{wifi, cellular_available}`, `audio` `{state, capture_id, last_seq}`, `battery_pct`, `battery_state`, `keep_awake`; a missing sensor is null; only these keys are kept, any other top-level key is dropped), plus `client_epoch_ms` and `client_tz_offset_min` (the page's clock when it sent the snapshot), `server_epoch`, `client_clock_offset_s` (phone − Pi from this message alone, one-way delay included) and the clock-estimate fields `clock_offset_s`, `clock_source`, `clock_uncertainty_ms`, `clock_probe_n` (as in `phone_audio_onset`). Accepted only from a paired phone socket. |
| `phone_audio_onset` | The iOS companion detected an impact onset on the phone's microphone and its page forwarded it (design §9.8). Log only: nothing feeds the shot, the carry, the club data or the trigger. Fields: the event's `capture_id`, `seq`, `onset_host_us` (phone `CLOCK_UPTIME_RAW` µs, input latency subtracted), `onset_host_us_raw`, `input_latency_us`, `onset_epoch_ms` (phone wall time), `onset_sample`, `onset_uncertainty_us`, `sample_rate_hz`, `sample_rate_eff_hz`, `host_fit_rms_us`, `clock`, `detection` `{snr_db, peak_dbfs, clipped, rise_time_ms, crest_db}`, `features` `{band_db, centroid_hz_0_2ms, centroid_hz_2_20ms, decay_ms_20db, post_low_db, second_transient}`, `fat_cue` (the fat-strike cue, design §9.6), `snippet_file`, `detector_version`, `client_epoch_ms`, `client_tz_offset_min`; then `server_epoch` and `server_monotonic_ns` (Pi clocks on receipt). Clock mapping: `clock_offset_s` (phone − Pi wall), `clock_source` (`probe` = the two-way `phone_clock_probe` with the smallest round trip in the last 10 min, `client_clock` = the phone's connect-time offset, null = none yet), `clock_uncertainty_ms` (half that round trip; null for `client_clock`), `clock_probe_n` (probes in the window) and `onset_pi_epoch_s` = `onset_epoch_ms / 1000 − clock_offset_s`. Edge match against the last 64 sound-trigger edges (every fanned-out edge, not only those that started a dump): `edge_match` (`matched` = exactly one edge within ±100 ms, `ambiguous` = several, the nearest is reported, `none`, `no_clock`), `edge_candidates`, `edge_sequence` (the edge's number since the server started), `edge_timestamp` and `edge_time_source` (`kernel` line-event time when known, else the `callback` stamp; Pi wall clock), `edge_kernel_timestamp`, `edge_callback_timestamp`, `edge_monotonic_ns` (kernel edge on the Pi's monotonic clock: callback stamp minus callback latency), `edge_residual_ms` (onset − edge; it carries the constant acoustic path and latency terms, so it is not a TDOA yet) and `edge_next_residual_ms` (the second-nearest candidate, when ambiguous). A `trigger_event` gets no new fields. The two-way `phone_clock_probe` itself is not logged: the server echoes its wall and monotonic time in the ack, the page reports its measured round trip, and the offset is computed from the Pi's own stamp of that probe. |
| `error` | An error, with optional context. |

### Shots and readings

| Type | Written when |
| --- | --- |
| `shot_detected` | A shot is measured. Ball speed, club speed, spin, angles, carry. `carry_air_density_kg_m3` is the air density the ballistic carry flew with, `carry_air_density_source` where it came from (`open-meteo` or `manual` while a `weather` entry is at most 3 h old, else `standard` = the fixed `--air-density`, default 1.184) and `carry_weather_age_s` that weather's age at the shot (null for `standard`). `carry_standard_air_yards` is the same ballistic carry at the fixed `--air-density`, whatever the weather; the simulator export sends it as `CarryDistance` (equal to `carry_spin_adjusted` for `standard`; null for `open-meteo`/`manual` when no simulator was connected at the shot, since it is only flown for the export). All four are null for a table carry (`carry_model` `table`). `typical_spin_table` (`tour` or `range-ball`) is the club-typical spin table of the shot's ballistic carry: its profile's ball type (`standard` = `tour`, `range` = `range-ball`), else the launcher's `--typical-spin-table`; it is set only when the ballistic carry flew a club-typical spin from that table (`carry_spin_source` `club_typical`), and null when a trusted measured spin was flown (`carry_spin_source` `measured`/`calculated`) and for a table carry. Absent in logs before the ball type setting. The launcher default table and the range-ball drag slope every ballistic carry of the session flew with are in `session_start` `config.typical_spin_table` (`tour` or `range-ball`, a profile's ball type overrides it per shot) and `config.range_ball_drag_slope` (0.0 = off), both null with `--no-ballistics`. |
| `carry_variants` | Shadow carries of one ballistic carry for the next range validation against Toptracer (`ballistics.carry_variants`), written after the shot is published, so it follows the shot's `shot_detected` entry; a shot flown again after its publication (`shot_updated`) gets a second entry after that one, and the last entry of a shot counts. Join to `shot_detected` by `shot_number` and `shot_timestamp` (= its `impact_timestamp`). `carry_variants` is `{tour, range_ball_spin, range_ball_spin_drag, range_ball_spin_drag_half}`, each `{spin_rpm, carry_m, apex_m}` (carry and apex in m, 0.1 m): the shot's launch (ball speed, both launch angles, spin axis) at the carry's air density (`air_density_kg_m3`), flown with the tour table, the range-ball table, and the range-ball table with the unvalidated range-ball drag at slope 0.3546 and 0.225. The tables are used unscaled (no `--typical-spin-scale`) and independently of the live `--typical-spin-table` / `--range-ball-drag-slope`; a trusted measured spin stays measured in all four. Integrated at 0.01 s (≤ 0.011 m from the live 0.002 s). `carry_variants` is null and `reason` says why (`failed: <error>`, `not_started: <error>`) when the flights failed; `reason` is null otherwise. `elapsed_ms` is the time the four flights took (null when not started). Every ballistic carry of a live shot gets one while a session log is written; table carries and demo shots get none. Never reaches the shot, the screens or the simulators; `scripts/analysis/range_session_report.py` shows them per club against TT. See [ballistics](../how-it-works/ballistics.md#carry-variants-session-log-only). |

#### Camera start direction fields of `shot_detected`

`experimental_camera_horizontal_deg` is the camera's start direction (null when
withheld); `experimental_camera_horizontal_status` names the decision, and for a
withheld camera the estimator's reason after a colon, e.g.
`camera_withheld_fallback_iwr:rejected_line_fit_residual`.
`experimental_camera_ball_locator` (null when the ball flight did not run, e.g.
for lighting) tells how the resting ball was found. `session_start`
`config.camera_capture.ball_locator` records `--camera-ball-locator`.

| Field | Meaning |
| --- | --- |
| `locator` | `disappeared` (the spot that vanished at the strike), `legacy` (`--camera-ball-locator legacy`) or `legacy_fallback` (the disappeared locator found no ball; the legacy locator ran). With reason `invalid_camera_frames` no locator ran and this only echoes the requested mode |
| `reason` | Outcome of the disappeared locator: `located`, `insufficient_pre_frames` (fewer than 30 frames before the trigger), `insufficient_post_frames` (the clip ends before trigger + 23 frames; the trigger frame lands up to 6 frames after the pre-trigger count, so `--camera-capture-post-ms` below 101 at 288 fps can end too early; 178 or more gives the full 30-frame window; the server warns at startup and after a camera-mode change when clips can be this short), `empty_search_box`, `no_search_box` (no hitting zone and no tee estimate), `no_ball_edge`, `implausible_ball_size`, `low_contrast` (contrast below 8 grey levels), `low_snr` (contrast below 8 x pixel noise), `no_ball_departure` (a ball was found but did not leave between trigger - 8 and trigger + 10 frames, e.g. a late trigger or a slow scene change: the shot is `rejected_no_ball_departure`, no legacy fallback, and the ball teaches neither the pose nor the session tee anchor), `invalid_camera_frames` (the clip is unreadable: fewer than 4 frames or timestamps that do not match; nothing ran); `legacy` in legacy mode |
| `ball_x`, `ball_y`, `ball_diameter_px` | Departed ball centre and diameter, full-frame pixels |
| `contrast`, `noise`, `snr` | Change inside the ball core between the pre-swing and late frames (grey levels): the larger mean of the core's upper and lower half, so a ball lit from above (bright on top, darker than the mat in its own shadow below) keeps its contrast; the core's temporal pixel noise before the swing (after the flicker step); and their ratio |
| `polarity` | `-1` the ball was darker than what it hid, `1` brighter |
| `teed`, `tee_px` | Whether a tee continues below the ball, and for how many rows |
| `ground_anchor_sample` | Whether this ball fed the ground anchor (the camera pose's median of untee'd balls): false for a ball with tee rows and for every `driver` ball on its profile tee, whatever its tee rows; null without a departed ball |
| `search_box`, `search_box_source` | `[x0, y0, x1, y1]` searched, from the `tee_zone` or, without a zone, around the `last_ball`, the `session_anchor` or a `detected` blob |
| `pose_source`, `pose_x`, `pose_y` | Camera pose source: `ground_anchor` (median of the session's untee'd balls), `this_ball` or `teed_ball` (fewer than 3 untee'd balls so far) |
| `flicker` | The mains-flicker step before the locator (`camera.flicker.compensate_ball_flicker`): `compensated`, `reason`, `rel_std` (mat waveform below the hitting zone's top), `ball_rel_std`, `ball_step_ratio`, `ball_pixels` (the ball window's most varying pixels), `window`, `detected_by` (`mat` or `ball`), `roi_top`, `lit_pixels`, `reference_frames`. The saved clip is unchanged; the step's wall time is part of the ball flight's in the kiosk log |
| `motion_gate` | `applied` (flight candidates must be reachable from the tee at the OPS ball speed), `no_ops_speed`, or null (legacy track search) |
| `gate_candidates`, `gate_kept` | Flight candidates of the threshold sweep before and after the motion gate |
| `line_fit_status` | Straight line at the OPS speed through the representative track (no TI data): `ok`, `over_residual` (rms above 3 mrad: direction withheld as `rejected_line_fit_residual`), `no_ops_speed`, `no_track`, `failed`; null when the legacy locator ran (legacy mode or fallback) |
| `line_fit` | `horizontal_deg`, `vertical_deg`, `time_zero_ms` (departure from the acoustic impact estimate), `rms_mrad`, `max_mrad`, `n_points` of that line |

The kiosk log's `[SERVER] Camera-assisted horizontal` line names the locator and
the ball flight's wall time.

With a target tapped in the Align view, `experimental_camera_horizontal_deg` (and
`line_fit.horizontal_deg`) is measured from the line to that target, not from the
camera axis plus `--camera-capture-horizontal-offset-deg`; see the angle
reference fields below.

#### Angle reference fields of `shot_detected`

The camera is the launch-angle reference (`src/openflight/angle_reference.py`).
Toptracer never enters it.

| Field | Meaning |
| --- | --- |
| `launch_angle_vertical_source` | `camera` (an accepted camera ball flight with a vertical: it wins over the TI), `radar` (the TI as measured; also when the corrected value failed the plausibility guard), `radar_calibrated` (the TI minus the session's valid vertical offset, passing the same club/speed guard as the raw TI), `estimated` (club table) |
| `launch_angle_horizontal_source` | As before: the TI azimuth keeps the fixed `--iwr6843-azimuth-offset-deg` (`radar`). The session's horizontal offset is logged, never applied: the TI horizontal is compressed, so TI minus camera follows the start directions of the pairs |
| `iwr6843_vertical_deg` | The plausible TI vertical as measured, never corrected; null when the TI had none or the plausibility guard rejected it |
| `experimental_camera_vertical_deg`, `experimental_camera_vertical_confidence` | The vertical of an accepted camera estimate (`status` `accepted` / `accepted_camera_only`) and its confidence: 0.75 high tier, 0.45 experimental, 0.30 camera-size depth; null otherwise (also for a direction the line fit withheld) |
| `angle_reference.vertical`, `angle_reference.horizontal` | `selected` (the source the shot shows), `radar_deg`, `camera_deg` (the pair this shot added, if both exist), `corrected_deg` (vertical: the TI minus the valid offset, shown as `radar_calibrated` unless rejected; horizontal: always null), `offset` (the session offset state after this shot, as in the `angle_reference` entry). Vertical only: `calibration_rejected` (null, or `reason` `calibrated_implausible` and the guard's `rule`, e.g. `bounds_0_45`, when `corrected_deg` failed the club/speed plausibility guard and the TI value was kept). Horizontal only: `target_changed` (true when the target was set or cleared while this shot's ball flight ran; the shot then adds no pair) |
| `angle_reference.target_line` | Null when the shot's ball flight was measured without a tapped target (or, for a shot without a camera ball flight, when no target is set); else the target this shot was measured against, as in the `angle_reference` entry, plus `shot_azimuth_deg`, its azimuth under this shot's camera pose (the start direction is reported minus this value) |

A vertical pair is a shot with both `iwr6843_vertical_deg` and
`experimental_camera_vertical_deg`; a horizontal pair a shot with
`iwr6843_horizontal_deg` whose shown direction is the camera's. An offset is valid
from 5 pairs on while its robust spread is at most 1.0 deg. It resets on session
clear, profile switch and camera geometry change; the horizontal one also when the
target is set or cleared.

#### Ball-track guard fields

The OPS ball-track guard checks the post-impact ball vote of every capture the sound
trigger or the OPS speed trigger (`--shot-trigger ops-speed*`) started
(`RollingBufferProcessor._guard_ball_speed`, constants `BALL_GUARD_*`; see
[rolling buffer](../how-it-works/rolling-buffer.md#ball-track-guard)). It is always
on. A rule fires when the voted ball's forward track reads like the club head's
follow-through (`club_like_track`: at least 3 links, at least 8 mph lost within 40 ms,
least-squares slope at or below −0.30 mph/ms) or, with a club speed and not a wedge,
when its smash is below 0.90 (`smash_implausible`). The guard then reads the
post-impact windows again with up to 12 peaks each: their fastest track, at least
10 % above the voted ball, not club-like itself and with a smash of 1.00–1.50
(PW/GW/SW/LW: 0.75–1.45), replaces the ball. Without one the number is kept and
flagged `uncertain`; the shot is never rejected.

`shot_detected` (and `shot_updated`):

| Field | Meaning |
| --- | --- |
| `ball_speed_status` | `uncertain` when a rule fired and no replacement was taken, else null. Such a shot stays out of the session averages; the screens mark its ball speed, smash and carry estimated and show a note. Simulators still get it |
| `ball_speed_status_reason` | The rule that fired, `club_like_track` or `smash_implausible`: for a flagged shot and for a replaced ball speed (status null, no hint on the screens); null when none fired |

`rolling_buffer_capture` of a shot carries the same two fields, plus:

| Field | Meaning |
| --- | --- |
| `ball_speed_replaced_from_mph` | The voted ball speed the guard replaced (`ball_speed_mph` is then the replacement); null otherwise |
| `ball_guard` | The whole decision, also when no rule fired (null without the anchored club of the sound or OPS speed trigger): `fired`, `rule`, `status`, `voted_ball_speed_mph`, `ball_speed_mph` (after the guard), `track_links`, `track_drop_mph` (null without a later link), `track_slope_mph_per_ms` (null under two links), `smash` (voted ball / club speed, uncapped; null without a club speed), `replaced_from_mph`, `replacement_mph` and `replacement_smash` (the fastest uncapped track tried, null when none was found or searched), `replacement_rejection` (why no replacement was taken after a rule fired: `no_club_speed`, `no_track`, `club_like_track` or `smash_out_of_range`) |

The accepted `trigger_event` the screens' debug view gets carries `ball_speed_status`,
`ball_speed_status_reason` and `ball_speed_replaced_from_mph` too.

```bash
# Every guard decision that fired, with its track
jq -c 'select(.type=="rolling_buffer_capture" and .ball_guard.fired) | {shot_number, ball_guard}' session_*.jsonl
```

#### OPS spin session median fields of `shot_detected`

Log only: neither field is in the shot the screens get, and neither changes the
carry or the simulator export (`src/openflight/ops_spin_median.py`).

| Field | Meaning |
| --- | --- |
| `ops_spin_session_median_rpm` | Median OPS spin (rpm) of the earlier shots of the same club in this session, the current shot excluded; null while `ops_spin_session_median_n` is below 3. The OPS spin is `spin_rpm` at any confidence (`spin_rpm_measured` when `--calculated-spin` replaced it); shots without a positive spin do not count |
| `ops_spin_session_median_n` | Number of earlier shots of that club with an OPS spin behind the median |

The earlier shots start over when the session is cleared, the active profile
changes (or a shot of another profile arrives), the monitor restarts; a deleted
shot stops counting. Demo shots never count. The carry span the server sends for a
carry with typical spin (`carry_span_min_yards`, `carry_span_max_yards`; no screen
shows it) is flown after the shot was logged and is not in the log.

#### Curve class field of `shot_detected`

`curve_class` is the curve shape of the drawn flight: `hook`, `draw`, `straight`,
`fade` or `slice` (right-handed: draw and hook curve left), from the same calculated
D-plane axis as `spin_axis_deg`, so it is set exactly when `spin_axis_source` is
`calculated` and null otherwise (driver and fairway woods, a missing club path, a
start direction below the confidence gate). `|spin_axis_deg|` below 2.5 deg is
`straight`, from 2.5 deg `draw` / `fade`, from 8.0 deg `hook` / `slice`
(`openflight.dplane.curve_class`; provisional limits). The screens show the class
word, never the axis. Every calculated axis (`spin_axis_deg`,
`calculated_spin_axis_deg`, hence `spin_axis_class` and `curve_class`) includes the
provisional +3.5 deg calibration offset `dplane.SPIN_AXIS_OFFSET_DEG` (against
Toptracer curves of 2026-10-26); its sigma does not.

#### Calculated club data fields of `shot_detected`

Every `shot_detected` entry carries the 28 calculated club-data keys of
`openflight.launch_monitor.CLUB_DATA_SHOT_FIELDS`: `club_data_status`,
`club_data_reason`, `camera_club_path_deg`, `camera_attack_angle_deg`,
`camera_low_point_cm`, `dynamic_loft_deg`, `spin_loft_deg`, `face_to_target_deg`,
`face_to_path_deg` (each with its `_sigma_deg` / `_sigma_cm`), `face_to_path_class`,
`calculated_spin_axis_deg` with its sigma, `spin_axis_class`, and the impact point:
`impact_offset_mm` (+ toe) with `impact_sigma_mm`, `impact_toe_mm` (its older name,
the same value), `impact_height_mm` with `impact_height_sigma_mm` (teed woods and
driver only), `impact_zone` (`heel` / `center` / `toe`, null when ambiguous),
`impact_method` (`D` measured across contact, `E` the extrapolated fallback) and
`impact_scale_mm_per_px` (see [camera](../camera/README.md), "Impact point"). Of
these, the screens show only the path and the attack angle; the rest is logged and
sent, not displayed (the impact point waits for its own tile).

They are a snapshot taken when the shot was logged, before the camera club-data job
finished, so do not read them as the shot's club data:

- `--club-data show`: `club_data_status` is `pending` and the values null for a
  clip the job runs on; null everywhere for a shot without one. Now and then the
  values are already there, when the job finished before the shot was logged, so
  the rows are not consistent either.
- `--club-data log`: always null.

The gated values are in the `club_data` entry below (and in the saved-session
history, which merges the `show` entries into its shots). Join it to
`shot_detected` by `shot_number` and `shot_timestamp`.

### Capture data

| Type | Written when |
| --- | --- |
| `trigger_event` | Trigger accepted or rejected, with latency. With the Pi trigger gate it carries the `gate_*` fields of the pulse that started the dump; `gate_first_byte_match` says how the dump found it (`window`: first byte in the usual window after the pulse; `late`: dump queued while the host was busy and read up to 1 s late; `none`: no pulse, `gate_decision` `unmatched`, plus `gate_nearest_edge_sequence` / `_edge_timestamp` / `_first_byte_lag_ms` of the last pulse before the first byte; `self_trip`, only with `--shot-trigger ops-speed-shadow`: also `unmatched`, the dump matched a fallback pulse with a first-byte lag below 1 ms, so it began before that pulse (an OPS speed trip pin 1 did not show), and the `gate_nearest_*` fields name that pulse). In `passthrough` mode with a camera, `gate_shadow_decision` / `_reason` / `_first_departure_ms` / `_frame_states` hold what `--ops-trigger-mode camera` would have decided for that edge (`still_there` = it would have blocked the pulse); the pulse itself never waits for it. Reason `ops_busy` is not a trigger: a sound edge whose pulse fell in OPS blind time and started no dump (`accepted` false, counted in `session_end` `stats.triggers_ops_busy`, not in the trigger totals). It carries `ops_busy_phase` (`dump` transfer, host `processing` after the dump, `rearm_drain`, `rearm_commands`), `ops_busy_started_by` (`dump` or `rearm`), `ops_busy_dump_end_timestamp` (host epoch in s when the dump was fully read; null for a bare re-arm or a dump not read to the end), `ops_busy_start_edge_sequence` (null for a bare re-arm or an unmatched dump), `ops_busy_elapsed_ms`, `ops_busy_ms_left` and `ops_busy_ended_by`. |
| `rolling_buffer_capture` | Raw OPS243 I/Q samples — 4,096 each. Also written for a capture the processor rejects (e.g. `no_ball_track`): then `shot_number` is null, `rejection_reason` names the reason, and no shot speeds are set, so the rejection can be replayed offline. A shot's capture carries the ball-track guard's decision (`ball_guard`, `ball_speed_status`, `ball_speed_status_reason`, `ball_speed_replaced_from_mph`; see [ball-track guard fields](#ball-track-guard-fields)). |
| `camera_capture` | High-speed camera clip metadata and its correlated shot. `metadata` is the clip's `metadata.json`; among its fields: `settings.vertical_offset_px` (the sensor window the driver applied; with `width` and `height` it places the clip's pixels in the canonical 640x400 frame, `capture_runtime.CAPTURE_MODE_FRAME_OFFSETS`), `auto_exposure` (the saved `capture_exposure_status`: `ball_analysis_eligible`; `ball_analysis_relaxed`, true when only the ball gate's dark rule admits the clip, which then runs its ball flight without the legacy fallback and no club data; `frame_flat`, true when a sampled frame was flat, one grey level and no picture: `status` `no_image`, eligible for neither path nor the low-light shadow) and `trigger_check` (the trigger gate's verdict on the clip: `ball_source` `hint`/`ball_mark`/`zone`/`full_frame`, `ball_mark` the window's mark in frame pixels or null, `flicker` its mains-flicker step). |
| `camera_club_head_path` | Camera club-data job of a ball-analysis-eligible clip, written after the shot is published (or with a `skipped_*` `reason`): the path (`path_deg`, `sigma_deg`, gate status, `segmentation`, `segmentation_sigma_deg`, `segmentation_paths_deg`), `frames` (per tracked head frame: `frame_index`, `t_rel_ms`, `quality` status, the rated head `contrast` in grey levels and `edge_width_px`, both 1 decimal and null where no head was rated or the value is not finite, `width_m`, `residual_mm`; a woods-segmented head (woods and hybrids) is rated on its background difference, every other head on the raw frame; a wood's head (`WOOD_CATEGORIES`) is `low_contrast` below 12, every other head below 20; the frames rated `ok` feed the path, `attack` and `impact` alike, so the wood limit moves all three and, through the path, `dplane`), the contact time (`t0_source` `departure_fit`/`camera_departure`/`acoustic_calibrated`, `t0_ms`, `t0_sigma_ms`, `t0_reason`, `camera_departure_ms` with its `_points`/`_rms_px`/`_span_frames`, `departure_agreement_ms`, `acoustic_ms`, `acoustic_reference`, `acoustic_delay_ms`/`_source`/`_n`, `acoustic_kernel_ms`), the shadow contact time (logged only, it never decides: `t0_shadow_reason` `accepted`, `teed`, `not_acoustic`, `no_candidate`, `before_acoustic`, `not_before_stamp` or `non_finite`; `teed` here is the server's driver shot (the job's `teed`: the club is the driver), not `ball_teed`; a shot that is not a driver shot whose `t0_source` is `acoustic_calibrated` because its departure lay after the sound bound has a candidate, for `t0_reason` `departure_fit_after_sound_bound` the live fit, for `camera_departure_after_sound_bound` the camera departure, accepted when `acoustic_ms` <= candidate < 0; `t0_shadow_source` `live_departure_fit`/`camera_departure` names the candidate judged, also a refused one, null without one; `t0_shadow_ms` (ms from the trigger stamp) and `t0_shadow_minus_t0_ms` (it minus `t0_ms`) are null unless accepted), the session's tee control for it as the job found it (`tee_control_n_teed` driver (`teed`) shots with a live departure fit, else a camera departure; `tee_control_n_late` how many of them lay more than 0.3 ms after their sound bound; `tee_control_passes` at least 5 teed and at most 10 % late; reset with the acoustic-delay calibration; all three are logged for skipped clips too), `path_last_frame_gap_ms` (ms from the last `ok` path frame to contact, null without one or when the job did not run), the arc path, logged only and read by nothing calculated (`path_arc_status`, `not_run` when the job did not run; `path_arc_deg`, `path_arc_sigma_deg`, null without a path; `path_arc_radii_deg` per closing radius for woods; `path_arc_window_deg`, one plain fit each without the last and the first usable frame, `drop_last`, `drop_first`, from 5 frames; its sigma terms are `attack.arc_path_*`), `path_noise_sigma` (the background's grey-level noise in the path's region before the swing, logged only, null when the job did not reach segmentation), the resolved ball (`ball_source`, `ball_x_px`, `ball_y_px`, `ball_d_px`; `ball_anchor` where its anchor came from: `departed` the ball flight's departed ball, `tracker` detection plus the session tracker (without a departed ball), `mark` the tapped ball mark (without a departed ball, when the tracker had no plausible ball; never for a club that may be teed: woods and hybrids, the driver included), null without one; without a ball the last anchor tried; `ball_refined` whether the teed-ball refine found a ball around that anchor, also when that ball was then dropped (false when the refine found nothing and the anchor stands as it is, when the anchor was dropped before any refine ran, and when the refine around the mark found nothing, so no anchor stands); `ball_rejected` the list, in order, of why tracker or mark anchors were dropped, null when none was: `tracker_`/`mark_`, then `anchor_`/`refined_`, then `outside_hitting_zone` or `far_from_mark` (more than two refine search radii, 50 px at a 16.4 px ball, from the mark), or `mark_refined_not_found` (the refine found no ball around the mark, whose tap is not this clip's ball), or `mark_anchor_club_may_be_teed` (a club that may be teed, every wood and hybrid and the driver, never anchors on the mark, whether or not the server calls the shot `teed`: around a tap on the ground the refine can latch onto the tee stem; the mark still bounds its tracker ball); a ball on the mark never feeds the session's head-length medians (its ball is the refine's find around the tap); a dropped tracker ball can leave the mark's ball standing, without any ball the path is `rejected_reference_ball_not_found`; all three null when the job did not run), `attack` (arc attack angle, low point, lofts), `impact` (the extrapolated heel/toe impact E or its rejection), `impact_point` (the impact point measured across contact, D: offset, sigma and its terms, height, method, scale and its source, the ball diameter and distance, frames L/P and why a frame after contact was not used, or its rejection; `scale_source` is always `geometry` since 2026-11-03: the regulation ball at the known range through the lens model, `ball_diameter_px` that expected diameter; logs before then also hold `ball` or `session` with the image-measured `ball_diameter_measured_px`, no longer written), `ball_teed`, `dplane` (the D-plane face) with `dplane_reason`, `elapsed_s`, `stage_elapsed_s`, `budget_s`, `club_data_mode` and `worker_restart_started`. Join to `shot_detected` by `shot_number`/`shot_timestamp`. Never feeds `club_path_deg`, the measured spin axis, the carry or the simulators. See [camera](../camera/README.md). |
| `camera_lowlight_ball_flight` | Low-light camera start direction in shadow (`camera.lowlight`; off with `--no-camera-lowlight-shadow`), one per shot clip, written after the shot is published and after the club-data job: `status` `done` or `skipped` with `reason` (`ran`, a `skipped_*` reason as for the club data, `skipped_gate_nearly_black`/`_flat_frame` (a flat clip: no picture, `frame_flat`)/`_unknown_controls`/`_no_exposure_metadata`, `skipped_missing_frames`, `timeout`, `job_error`, `worker_error`), `lowlight_gate` (`live`: the live ball gate admits the clip; `relaxed`: only the relaxed gate does, i.e. motion-blur risk ignored and only a nearly black hitting area blocks, or the live gate admits it only by its own relaxed dark rule), the shadow direction `lowlight_camera_horizontal_deg` with `_confidence` and `_status` (as `experimental_camera_horizontal_*`), `lowlight_camera_iwr_delta_deg`, `lowlight_camera_vertical_deg`, `lowlight_estimate_status`, `lowlight_ball_locator`, `lowlight_legacy_fallback` (false for a relaxed-only clip: no legacy-locator directions), `lowlight_locator`, the floodlight flicker (`flicker_std`, `flicker_compensated`, `flicker`), the live result for comparison (`live_ball_analysis_eligible`, `live_camera_horizontal_deg`/`_confidence`/`_status`), `lowlight_new_direction` (shadow has a direction, live none), `lowlight_minus_live_deg`, `lowlight_job_ms` and `elapsed_s`. Join to `shot_detected` by `shot_number`/`shot_timestamp`. Never reaches the shot (`launch_angle_horizontal`, curve, spin axis), the screens or the simulators. |
| `club_data` | The gated calculated club data of one eligible clip: `{shot_number, shot_timestamp, status, reason, mode, fields}`. `status` is `done` or `skipped` (with `reason`), `mode` the server's `--club-data` mode, `fields` the calculated shot fields (`openflight.club_data.CLUB_DATA_FIELDS`; None where a gate failed). The saved-session history merges only `show` entries into its shots. |
| `iwr6843_capture` | IWR6843 L3 dump for a shot. |
| `ti_self_trigger` | IWR6843 firmware self-trigger (`--shot-trigger ti` or `ti-shadow`, `src/openflight/iwr6843/self_trigger.py`). Timestamps: `*_timestamp` Pi wall clock, `*_monotonic_ns` Pi monotonic clock (the notice when the host read the `Triggered` line, the edge from the GPIO callback). `ti`: one `kind` `fire` per accepted fire, with `mode`, `sequence` (TI capture number), `edge_timestamp` (= the notice; the gate, the camera and the TI capture use it as the shot edge), `notice_timestamp`, `notice_monotonic_ns`, `notice_in_reply` (true when the line arrived inside another CLI reply, so its stamp is late by that exchange), `cause` (`crossing`, `end`, `leave` from `triggerLog track`, or null), `trigger_cfg` and `anchor_lead_ms` (notice minus the club-speed anchor, from `--ti-fire-latency-ms`). `ti-shadow`: one entry per outcome of pairing notices with microphone edges (window `window_ms`, notice minus edge, default −150…+100 ms): `matched` (`delta_ms` = notice − edge, `cause`, `ring` = what the host did with the latched ring: `dumped` = the edge's l3dump streamed it, `released`, `consumed` = it fired as a command reached it; `ti_state` at the edge), `notice_without_edge` (a would-be false fire; `nearest_edge_delta_ms`, `ring`), `edge_without_notice` (a would-be miss; `ti_state` `listening` = the board could fire, else `dumping`/`latched`/`recovering`/`failed`; `ti_listen_ms` = how long after the edge the board could still fire before the sound-path l3dump froze its ring, ≈ `--ti-shadow-holdoff-ms`; `nearest_notice_delta_ms`). The microphone drives OPS, camera and dump as in `sound` mode. `trigger_event` / `rolling_buffer_capture` gate fields gain `gate_edge_source` (`sound`/`ti`), `gate_pulse_delay_ms` (edge to HOST_INT), `ti_fire_cause` and `ti_anchor_lead_ms`; `sound_edge_delay_ms` is pulse delay plus that lead. `session_start` `config.iwr6843` carries `shot_trigger` and `self_trigger` (`command`, `tee_bin`, `snr`, `offset_m`, `explicit_bin`, `ball_bin` (tee + 30 mm), `shadow_holdoff_ms`, `shadow_window_ms`), `config.trigger_gate.ti_anchor` the anchor model. |
| `ops_speed_trigger` | OPS243-A firmware 1.3.3 speed trigger (`--shot-trigger ops-speed` or `ops-speed-shadow`, `src/openflight/ops_speed.py`), with `mode`. Witness outcomes of pairing J3 pin 1 edges with microphone edges on the monotonic clock (window `window_ms`, microphone minus pin, default 0…60 ms): `matched` (`pin_timestamp`, `mic_timestamp`, their `*_monotonic_ns`, `delta_ms` = microphone − pin, the delay M3 recalibrates `--ops-speed-ti-delay-ms` and `--ops-speed-anchor-ms` from; `pin_high_ms` = pin 1 HIGH width, `pin_released`; `ops_state`, always `busy` here: the OPS is counted blind from its own trip on, so only `mic_without_pin` entries carry information in it), `pin_without_mic` (a neighbour, a practice swing or a shot the microphone missed; `nearest_mic_delta_ms`), `mic_without_pin` (`ops_state` `armed` = a would-be B2 miss, `busy` = the OPS was blind: from a trip or HOST_INT pulse before the edge until its dump can arrive (post-trigger span + 60 ms), then during the dump and its re-arm; the shadow's own fallback pulse for that edge does not count; `fallback_pulse` = the shadow pulsed HOST_INT for it; `nearest_pin_delta_ms`) and `glitch` (pin 1 released before `--ops-speed-debounce-ms` confirmed it). Further kinds: `stall_rearm` (`ops-speed`: `mic_edges` armed `mic_without_pin` in a row, `rearm_requested`; the re-arm itself is a `trigger_event` with reason `ops_speed_stall_rearm`), `pin_silent` (`dumps_without_pin_edge` dumps in a row without a pin edge, i.e. `ops_speed_dump` `no_record` or `self_trip`; `pin_bcm`: check wiring and polarity), `refused` (the preflight fell back to `--shot-trigger sound`: `reason`, `firmware`) and, at session end, `summary` (counts per kind, `mic_without_pin_armed` / `_busy`, `stall`, `pending_pins` / `pending_mics`, `delta_ms` and `pin_high_ms` as `n`/`median`/`p10`/`p90`/`min`/`max`, the gate's `trips` / `claimed` / `fallback` / `host_int_echoes`, `stall_rearms_requested`). `trigger_event` and `rolling_buffer_capture` gain `gate_ops_speed_mode`, `gate_ops_speed_edge_timestamp` (the pin edge), `ops_speed_fallback`, `ops_speed_host_int_echo_ms` (shadow: pin 1 rose this long after a fallback pulse, with that pulse's own dump; such a pin edge is no trip and no witness record), `ops_speed_anchor_ms` (for a trip: anchor after it; `sound_edge_delay_ms` = −anchor, or −(microphone − pin) for a trip a microphone edge claimed), `gate_decision` `ops_speed` (`gate_reason` `speed_trip` or `sound_claimed`, `gate_edge_source` `ops-speed` or `sound`), `ops_speed_dump` (`pin`, `matched`, `unmatched`, `fallback`, `self_trip` (shadow: the dump began before the fallback pulse it matched, see `gate_first_byte_match`; rejected as `ops_speed_unmatched`, anchored −`--ops-speed-anchor-ms`), `no_record`), `ops_speed_preamble` (the bytes before the dump marker, up to 512, i.e. the 1.3.3 trip message) with `ops_speed_preamble_bytes` / `_timestamp`, `ops_speed_witness`, `ops_speed_mic_delta_ms`, `ops_speed_pin_high_ms` and `ops_speed_practice_swing` (`verdict` `practice_swing` / `shot` / `unknown`, `impact_source`, `impact_speed_delta_mph`, `smash_factor`, `ball_speed_mph`, `club_speed_mph`, `post_anchor_repeat_count`, `rejection_reason`, `max_smash`). Rejection reasons `ops_speed_unmatched` (shadow: a trip no microphone edge claimed or a `self_trip`, full I/Q kept) and `ops_speed_practice_swing` (only with `--ops-speed-reject-practice-swings`) only replace a missing processor reason. `iwr6843_capture` adds `l3dump_target_ms` / `l3dump_actual_ms` (edge to `l3dump`) in `ops-speed`. `session_start` `config.ops_speed`: `enabled`, `mode`, `firmware`, `pin_idle`, `flags` (every `--ops-speed-*` value), `speed_trigger_setup` (the commands and the raw `??` reply) or `refused`; `config.trigger_gate.ops_speed` (`mode`, `anchor_ms`, `window_ms`, `post_trigger_ms` = the dump's post-trigger span the witness's `busy` uses); `config.iwr6843.freeze_delay_ms` is the TI delay. |
| `kld7_buffer` | K-LD7 frame timing and selected angle metadata (deprecated hardware). |

### Outbound and system

| Type | Written when |
| --- | --- |
| `sim_send` | A shot was forwarded to a simulator. |
| `sim_status` | Simulator connection status changed. |
| `sim_player` | Player state — target, handedness, club. |
| `power_status` | Battery and external-power status. Each entry also carries the Pi's health (`src/openflight/pi_health.py`; log only, the UI's `power_status` event is unchanged): `cpu_temp_c` (CPU temperature from `thermal_zone0`, °C) and `throttle` (`vcgencmd get_throttled`: `raw` hex value plus `under_voltage_now`, `freq_capped_now`, `throttled_now` and the since-boot `under_voltage_ever`, `freq_capped_ever`, `throttled_ever`); either is null when it cannot be read. |

## Size

Raw capture entries dominate. A session with a few hundred shots produces a
file in the tens of megabytes because `rolling_buffer_capture` stores 8,192
samples per shot.

This is deliberate — those captures are what make offline estimator work
possible without re-hitting balls. See
[spin replay](../development/spin-replay.md).

[Cloud sync](../using/cloud-sync.md) strips the raw ADC entries before upload;
that filtering is **required** by the wire contract, not optional — see the
[cloud uploader spec](cloud-uploader-spec.md).

## Querying

Locally, `jq` is usually enough:

```bash
# Every shot's ball speed
jq -r 'select(.type=="shot_detected") | .ball_speed' session_*.jsonl

# Trigger rejects and why (edges lost to OPS blind time are not triggers)
jq -r 'select(.type=="trigger_event" and .accepted==false and .reason!="ops_busy")' session_*.jsonl

# Sound edges lost to OPS blind time, by phase
jq -r 'select(.type=="trigger_event" and .reason=="ops_busy") | .ops_busy_phase' session_*.jsonl
```

For LogQL queries against shipped logs, see
[observability](../using/observability.md#querying-session-data).

## Related

- [Observability & log shipping](../using/observability.md)
- [Cloud uploader contract](cloud-uploader-spec.md)
