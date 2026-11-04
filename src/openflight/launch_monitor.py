"""
Golf Launch Monitor data types and carry estimation.

Provides Shot and carry distance estimation used by
RollingBufferMonitor and the Flask server.
"""

import copy
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from .clubs import ClubType
from .clubs.physics import get_club_physics
from .ops243 import SpeedReading

# Spin confidence threshold for "high" quality — used across modules.
# Measured spin is trusted for physics simulation only above this level.
SPIN_CONFIDENCE_HIGH = 0.7

# Floor below which a measured spin is diagnostic only: it is still reported,
# but the carry table substitutes the club-optimal spin for the ball speed.
SPIN_CONFIDENCE_RELIABLE = 0.6

# Physical ceiling on ball speed / club speed. The USGA COR limit (0.830)
# caps a conforming driver at ~1.50; every other club is lower. A larger
# ratio means club speed was under-read, so no smash is reported at all.
MAX_SMASH_FACTOR = 1.5

# ``Shot.ball_speed_status`` of a ball speed the OPS ball-track guard kept although
# it reads like the club head's trailing track (rolling_buffer.processor).
BALL_SPEED_UNCERTAIN = "uncertain"


def compute_smash_factor(ball_speed_mph: float, club_speed_mph: Optional[float]) -> Optional[float]:
    """Ball speed / club speed; None without a club speed or above MAX_SMASH_FACTOR."""
    if club_speed_mph is None or club_speed_mph <= 0:
        return None
    smash = ball_speed_mph / club_speed_mph
    if smash > MAX_SMASH_FACTOR:
        return None
    return smash


# Calculated club-data fields of ``Shot`` (``openflight.club_data``), in the
# order ``Shot.to_dict`` writes them.
CLUB_DATA_SHOT_FIELDS = (
    "club_data_status",
    "club_data_reason",
    "camera_club_path_deg",
    "camera_club_path_sigma_deg",
    "camera_attack_angle_deg",
    "camera_attack_angle_sigma_deg",
    "camera_low_point_cm",
    "camera_low_point_sigma_cm",
    "dynamic_loft_deg",
    "dynamic_loft_sigma_deg",
    "spin_loft_deg",
    "spin_loft_sigma_deg",
    "face_to_target_deg",
    "face_to_target_sigma_deg",
    "face_to_path_deg",
    "face_to_path_sigma_deg",
    "face_to_path_class",
    "calculated_spin_axis_deg",
    "calculated_spin_axis_sigma_deg",
    "spin_axis_class",
    "impact_toe_mm",
    "impact_sigma_mm",
    "impact_offset_mm",
    "impact_height_mm",
    "impact_height_sigma_mm",
    "impact_zone",
    "impact_method",
    "impact_scale_mm_per_px",
)


def trusted_spin_rpm(shot: "Shot") -> Optional[float]:
    """Return the measured spin only when its confidence reaches SPIN_CONFIDENCE_HIGH."""
    if (
        shot.spin_rpm is not None
        and shot.spin_confidence is not None
        and shot.spin_confidence >= SPIN_CONFIDENCE_HIGH
    ):
        return float(shot.spin_rpm)
    return None


def estimate_carry_distance(ball_speed_mph: float, club: ClubType = ClubType.DRIVER) -> float:
    """
    Estimate carry distance from ball speed using TrackMan-derived data.

    This uses interpolation from real-world data assuming optimal launch
    conditions (10-14° launch angle, appropriate spin for ball speed).

    Data sources:
    - TrackMan PGA Tour averages
    - pitchmarks.com ball speed to distance tables

    Args:
        ball_speed_mph: Ball speed in mph
        club: Club type (affects the model used)

    Returns:
        Estimated carry distance in yards

    Note:
        Without launch angle and spin rate, this is an approximation.
        Actual distance can vary ±10-15% based on:
        - Launch angle (optimal: 10-14° for driver)
        - Spin rate (optimal: 2000-3000 rpm for driver)
        - Weather conditions
        - Altitude
    """
    # Driver ball speed to carry distance lookup table
    # Based on TrackMan data assuming optimal launch conditions
    # Format: (ball_speed_mph, carry_yards_low, carry_yards_high)
    DRIVER_TABLE = [
        (100, 130, 142),
        (110, 157, 170),
        (120, 183, 197),
        (130, 207, 223),
        (140, 231, 249),
        (150, 254, 275),
        (160, 276, 301),
        (167, 275, 285),  # PGA Tour average
        (170, 298, 325),
        (180, 320, 349),
        (190, 342, 372),
        (200, 360, 389),
        (210, 383, 408),
    ]
    # Some source rows are mixed (e.g. PGA average vs optimal carry) and can
    # create local dips in the midpoint curve. Clamp midpoints to a
    # non-decreasing sequence so carry does not decrease as ball speed rises.
    monotonic_driver_curve = []
    max_carry_so_far = 0.0
    for speed, carry_min, carry_max in DRIVER_TABLE:
        midpoint = (carry_min + carry_max) / 2
        max_carry_so_far = max(max_carry_so_far, midpoint)
        monotonic_driver_curve.append((speed, max_carry_so_far))

    # No club factor applied — ball speed already reflects the club's smash
    # factor. Club-specific carry differences come from launch angle and spin
    # adjustments applied downstream.

    # Interpolate from driver table
    if ball_speed_mph <= monotonic_driver_curve[0][0]:
        # Below minimum - extrapolate linearly
        ratio = ball_speed_mph / monotonic_driver_curve[0][0]
        base_carry = monotonic_driver_curve[0][1]
        carry = base_carry * ratio
    elif ball_speed_mph >= monotonic_driver_curve[-1][0]:
        # Above maximum - extrapolate conservatively
        # Use ~1.8 yards per mph above 210 mph
        base_carry = monotonic_driver_curve[-1][1]
        carry = base_carry + (ball_speed_mph - monotonic_driver_curve[-1][0]) * 1.8
    else:
        # Interpolate between table entries
        for i in range(len(monotonic_driver_curve) - 1):
            if monotonic_driver_curve[i][0] <= ball_speed_mph < monotonic_driver_curve[i + 1][0]:
                # Linear interpolation
                speed_low, carry_low = monotonic_driver_curve[i]
                speed_high, carry_high = monotonic_driver_curve[i + 1]

                # Interpolate
                t = (ball_speed_mph - speed_low) / (speed_high - speed_low)
                carry = carry_low + t * (carry_high - carry_low)
                break
        else:
            # Fallback (shouldn't reach here)
            carry = ball_speed_mph * 1.65

    return carry


def adjust_carry_for_launch_angle(
    base_carry: float,
    launch_angle: float,
    club: ClubType = ClubType.DRIVER,
    confidence: float = 1.0,
) -> float:
    """
    Adjust carry distance based on launch angle deviation from optimal.

    Deviation from optimal costs carry:
    - Too low: -2.0 yards per degree (ball doesn't get enough height)
    - Too high: -1.5 yards per degree (ball balloons, less severe)
    - Penalty is scaled by confidence and capped at 10% of base carry.

    Args:
        base_carry: Base carry distance in yards (from estimate_carry_distance)
        launch_angle: Measured vertical launch angle in degrees
        club: Club type (determines optimal launch angle)
        confidence: Confidence in the launch angle measurement (0-1)

    Returns:
        Adjusted carry distance in yards
    """
    optimal = get_club_physics(club).optimal_launch_deg
    angle_delta = launch_angle - optimal

    if angle_delta < 0:
        raw_penalty = abs(angle_delta) * 2.0
    else:
        raw_penalty = angle_delta * 1.5

    penalty = raw_penalty * confidence
    max_penalty = base_carry * 0.10
    penalty = min(penalty, max_penalty)

    return base_carry - penalty


@dataclass
class Shot:
    """
    Represents a detected golf shot with ball and club data.

    Attributes:
        ball_speed_mph: Peak ball speed detected (mph)
        ball_speed_status: "uncertain" when the OPS ball-track guard kept a ball
            speed that reads like the club head's trailing track (no replacement
            found); None otherwise. Such shots stay out of the session averages
        ball_speed_status_reason: Guard rule that fired ("club_like_track" or
            "smash_implausible"), also when it replaced the ball speed (status None)
        club_speed_mph: Peak club head speed detected (mph), if available
        smash_factor: Ratio of ball speed to club speed (typically 1.4-1.5 for driver)
        timestamp: When the shot was detected
        shot_number: Stable session sequence assigned when the server receives the shot
        impact_timestamp: Epoch timestamp aligned to impact/OPS trigger time
        impact_timestamp_kld7: Ball-contact instant used by the K-LD7
            geometry launch-angle estimator. This currently mirrors the
            trusted hardware sound-trigger timestamp; OPS ball_timestamp_ms is
            a radar-return position inside the capture and should not move
            geometry t=0.
        peak_magnitude: Signal strength of strongest reading
        readings: All raw speed readings for this shot
        club: Club type for distance estimation
        launch_angle_vertical: Vertical launch angle in degrees (from camera)
        launch_angle_horizontal: Horizontal launch angle in degrees (from camera)
        launch_angle_confidence: Backward-compatible primary launch angle confidence (0-1)
        launch_angle_vertical_confidence: Confidence in vertical launch angle measurement
        launch_angle_horizontal_confidence: Confidence in horizontal launch angle measurement
        launch_angle_vertical_source: Source for vertical launch angle: "radar",
            "camera" (an accepted camera ball flight), "radar_calibrated" (the TI
            corrected by the session's camera-referenced offset) or "estimated"
        launch_angle_horizontal_source: Source for horizontal launch angle (the
            TI keeps "radar": its session offset is logged, never applied)
        launch_angle_vertical_rejection_reason: Why a measured vertical radar angle
            was discarded (e.g. "ti_launch_implausible"), if it was
        iwr6843_vertical_deg: The plausible TI vertical launch angle as measured,
            uncorrected, whichever value the shot shows
        experimental_camera_vertical_deg: Vertical launch of an accepted camera
            ball-flight estimate, None otherwise
        experimental_camera_vertical_confidence: Its confidence by estimator tier
        angle_reference: Per-shot state of the camera angle reference
            (``openflight.angle_reference``): what each axis selected, the
            radar and camera values, the session offsets and the target line
        spin_rpm: Spin rate in RPM (from rolling buffer mode)
        spin_confidence: Confidence in spin measurement (0-1)
        spin_method: Estimator that produced the spin candidate
        spin_result_quality: Processor quality label for the spin detection
        spin_multipath_fade_hz: Fitted two-ray fade frequency, when available
        spin_snr: Signal-to-noise ratio of the spin envelope peak
        spin_modulation_depth: Envelope std/mean inside the spin window
        spin_peak_freq_hz: Frequency of the detected spin candidate
        spin_seam_cycles: Number of spin cycles in the analysis window
        spin_at_lower_rail: Whether the spin candidate hit the low search boundary
        spin_at_upper_rail: Whether the spin candidate hit the high search boundary
        spin_candidates: Ranked spin candidates for offline analysis
        spin_phase_method: Phase confirmation method, if attempted
        spin_phase_rpm: Phase-derived spin candidate, if available
        spin_phase_snr: Phase-derived candidate SNR
        spin_phase_agreement_pct: Envelope/phase agreement percentage
        spin_phase_confirmed: Whether phase recovered a low-SNR spin
        spin_rejection_reason: Why spin was withheld, if it was rejected
        carry_spin_adjusted: Carry distance adjusted for spin (yards)
        carry_lateral_yards: Simulated landing offset from the target line (yards, +right)
        apex_yards: Highest point of the simulated flight behind a ballistic carry
            (yards above the tee); None for a table carry
        flight_path: The simulated flight behind a ballistic carry, as
            {"unit": "yd", "points": [[t, x, y, z], ...]} with at least two points,
            strictly increasing t and a last point on the ground; None for table
            carries, for a flight that ends at launch and for one that never
            touched down
        roll_yards: total_yards - carry (yards), negative when the ball spins back
        total_yards: Distance from the tee to where the ball rests, measured like carry (yards)
        roll_path: The bounces and roll from the touchdown to the rest point, in
            flight_path's format with t continuing from the flight, at most 25 points.
            The three roll fields are None when the shot has no flight_path, when the
            ball lands faster than the roll model's 40 m/s and when the model fails
        mode: Shot source — "streaming", "rolling-buffer", "mock" or "demo"
        readings_data: Serialized readings for session logging
        camera_replay: Public replay metadata for a matched high-speed capture
        demo: A demo shot lives in memory only: it is never logged, forwarded to a
            simulator or shown in the saved history
    """

    ball_speed_mph: float
    timestamp: datetime
    shot_number: Optional[int] = None
    impact_timestamp: Optional[float] = None
    impact_timestamp_kld7: Optional[float] = None
    club_speed_mph: Optional[float] = None
    # Raw OPS radial ball speed, kept when the cosine correction rewrites
    # ball_speed_mph (radar bin anchoring must keep using the radial value)
    ball_speed_raw_mph: Optional[float] = None
    # Ball-track guard of the OPS processor (rolling_buffer.processor.BALL_GUARD_DROP_MPH).
    ball_speed_status: Optional[str] = None
    ball_speed_status_reason: Optional[str] = None
    peak_magnitude: Optional[float] = None
    readings: List[SpeedReading] = field(default_factory=list)
    club: ClubType = ClubType.DRIVER
    launch_angle_vertical: Optional[float] = None
    launch_angle_horizontal: Optional[float] = None
    launch_angle_confidence: Optional[float] = None
    launch_angle_vertical_confidence: Optional[float] = None
    launch_angle_horizontal_confidence: Optional[float] = None
    launch_angle_vertical_source: Optional[str] = None
    launch_angle_horizontal_source: Optional[str] = None
    launch_angle_vertical_rejection_reason: Optional[str] = None
    spin_rpm: Optional[float] = None
    spin_confidence: Optional[float] = None
    # Raw radar-measured spin, kept when --calculated-spin rewrites
    # spin_rpm with the kinematic estimate (for offline scoring)
    spin_rpm_measured: Optional[float] = None
    spin_source: Optional[str] = None  # "measured", "calculated", or None
    spin_method: Optional[str] = None
    spin_result_quality: Optional[str] = None
    spin_multipath_fade_hz: Optional[float] = None
    spin_snr: Optional[float] = None
    spin_modulation_depth: Optional[float] = None
    spin_peak_freq_hz: Optional[float] = None
    spin_seam_cycles: Optional[float] = None
    spin_at_lower_rail: Optional[bool] = None
    spin_at_upper_rail: Optional[bool] = None
    spin_candidates: Optional[list] = None
    spin_phase_method: Optional[str] = None
    spin_phase_rpm: Optional[float] = None
    spin_phase_snr: Optional[float] = None
    spin_phase_agreement_pct: Optional[float] = None
    spin_phase_confirmed: bool = False
    spin_rejection_reason: Optional[str] = None
    carry_spin_adjusted: Optional[float] = None
    carry_spin_rpm: Optional[float] = None
    carry_spin_source: Optional[str] = None
    # Club-typical spin table of the ballistic carry (ballistics.TYPICAL_SPIN_TABLES): the
    # shot profile's ball type, else --typical-spin-table. Set only when the ballistic
    # carry flew a club-typical spin from it (carry_spin_source "club_typical"); None when
    # a trusted measured spin was flown (carry_spin_source "measured"/"calculated") and
    # for a table carry.
    typical_spin_table: Optional[str] = None
    carry_model: Optional[str] = None
    # Air density the ballistic carry flew with and its source ("open-meteo", "manual"
    # or "standard"); the weather's age in s at the shot (None for "standard").
    carry_air_density_kg_m3: Optional[float] = None
    carry_air_density_source: Optional[str] = None
    carry_weather_age_s: Optional[float] = None
    # Ballistic carry at the fixed --air-density, whatever the weather: the carry the
    # simulator export sends, so GSPro & co. are not changed by the weather (None for a
    # table carry, which has no density in it).
    carry_standard_air_yards: Optional[float] = None
    carry_lateral_yards: Optional[float] = None
    # Peak height of the flight behind a ballistic carry (yards); None for a table carry.
    apex_yards: Optional[float] = None
    # Carry over 50-100 % of the club-typical spin (ballistics.carry_span_yards); only
    # for a club_typical carry. Set after the shot event, so not in the session log.
    carry_span_min_yards: Optional[float] = None
    carry_span_max_yards: Optional[float] = None
    flight_path: Optional[dict] = None
    roll_yards: Optional[float] = None
    total_yards: Optional[float] = None
    roll_path: Optional[dict] = None
    mode: str = "rolling-buffer"
    profile_id: str = ""
    profile_name: str = ""
    readings_data: Optional[list] = None
    camera_replay: Optional[dict] = None
    angle_source: Optional[str] = None  # "radar", "camera", "estimated", or None
    club_angle_deg: Optional[float] = None  # Club angle of attack from K-LD7 (vertical)
    club_path_deg: Optional[float] = None
    experimental_attack_angle_deg: Optional[float] = None
    experimental_attack_angle_status: Optional[str] = None
    experimental_club_path_deg: Optional[float] = None
    experimental_club_path_status: Optional[str] = None
    # Camera-fused club delivery: radar AoA candidate + per-club calibration
    # offset, club path derived from the camera delivery-plane trace. Same
    # experimental estimator level as the fields above.
    experimental_fused_attack_angle_deg: Optional[float] = None
    experimental_fused_club_path_deg: Optional[float] = None
    experimental_fused_status: Optional[str] = None
    experimental_fused_attack_angle_confidence: Optional[str] = None
    experimental_fused_club_path_confidence: Optional[str] = None
    experimental_camera_trace_deg: Optional[float] = None
    experimental_aoa_offset_source: Optional[str] = None
    # Independent horizontal ball-flight evidence. The camera-assisted value
    # may become the displayed experimental result while the original IWR
    # value remains available for replay and comparison.
    iwr6843_horizontal_deg: Optional[float] = None
    iwr6843_horizontal_confidence: Optional[float] = None
    # The camera is the angle reference (openflight.angle_reference): the TI
    # vertical as measured, the accepted camera vertical and the per-shot state.
    iwr6843_vertical_deg: Optional[float] = None
    experimental_camera_vertical_deg: Optional[float] = None
    experimental_camera_vertical_confidence: Optional[float] = None
    angle_reference: Optional[dict] = None
    experimental_camera_horizontal_deg: Optional[float] = None
    experimental_camera_horizontal_confidence: Optional[float] = None
    experimental_camera_horizontal_status: Optional[str] = None
    experimental_camera_iwr_delta_deg: Optional[float] = None
    # Camera time-zero source and candidate impact instants (ms from trigger).
    experimental_camera_time_zero: Optional[dict] = None
    # Which ball locator the camera start direction used and what it measured
    # (``ball_flight.BallLocatorDiagnostics.to_dict``).
    experimental_camera_ball_locator: Optional[dict] = None
    # Transient fitted IWR range trajectory consumed by live camera fusion.
    # Deliberately excluded from websocket/session serializers.
    iwr6843_club_range_evidence: object | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    iwr6843_ball_range_evidence: object | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    spin_axis_deg: Optional[float] = None  # Spin axis tilt: 0=backspin, +right(fade), -left(draw)
    spin_axis_source: Optional[str] = None  # "calculated" (D-plane), or None
    # Curve shape of the drawn flight, one of dplane.CURVE_CLASSES: set from the same
    # axis as spin_axis_deg, and only for a "calculated" one; None otherwise (woods).
    curve_class: Optional[str] = None
    # Calculated club data ("berechnet", ``openflight.club_data``): set by the
    # camera club-data job after the shot is published, each value only where its
    # gate passed, so None means "not available". Nothing reads them for the
    # carry, the D-plane spin axis above or the simulator export.
    club_data_status: Optional[str] = None  # "pending", "done" or "skipped"
    club_data_reason: Optional[str] = None
    camera_club_path_deg: Optional[float] = None  # + in-to-out
    camera_club_path_sigma_deg: Optional[float] = None
    camera_attack_angle_deg: Optional[float] = None  # + up
    camera_attack_angle_sigma_deg: Optional[float] = None
    camera_low_point_cm: Optional[float] = None  # + ahead of the ball (toward the target)
    camera_low_point_sigma_cm: Optional[float] = None
    dynamic_loft_deg: Optional[float] = None
    dynamic_loft_sigma_deg: Optional[float] = None
    spin_loft_deg: Optional[float] = None
    spin_loft_sigma_deg: Optional[float] = None
    face_to_target_deg: Optional[float] = None  # + open (right of the target)
    face_to_target_sigma_deg: Optional[float] = None
    # The face-to-path and spin-axis numbers feed session means only; per shot the
    # UI shows their class words.
    face_to_path_deg: Optional[float] = None
    face_to_path_sigma_deg: Optional[float] = None
    face_to_path_class: Optional[str] = None  # "closed", "neutral" or "open"
    calculated_spin_axis_deg: Optional[float] = None
    calculated_spin_axis_sigma_deg: Optional[float] = None
    spin_axis_class: Optional[str] = None  # "draw", "straight" or "fade"
    # Impact point (``club_data._impact_fields``): heel/toe offset (mm, + toward the
    # toe, right-handed) and its sigma; ``impact_toe_mm`` is its older name, the same
    # value. The face height (ball centre above the head's lower edge) only for a
    # teed wood or driver; the zone None where ambiguous; the method "D" (measured
    # across contact) or "E" (extrapolated fallback); the mm scale at the ball.
    impact_toe_mm: Optional[float] = None
    impact_sigma_mm: Optional[float] = None
    impact_offset_mm: Optional[float] = None
    impact_height_mm: Optional[float] = None
    impact_height_sigma_mm: Optional[float] = None
    impact_zone: Optional[str] = None  # "heel", "center" or "toe"
    impact_method: Optional[str] = None  # "D" or "E"
    impact_scale_mm_per_px: Optional[float] = None
    inclinometer: Optional[dict] = None  # Stable enclosure orientation used for this shot
    club_selection: Optional[str] = None
    demo: bool = False

    @property
    def ball_speed_ms(self) -> float:
        """Ball speed in meters per second."""
        return self.ball_speed_mph * 0.44704

    @property
    def club_speed_ms(self) -> Optional[float]:
        """Club speed in meters per second."""
        if self.club_speed_mph is None:
            return None
        return self.club_speed_mph * 0.44704

    @property
    def smash_factor(self) -> Optional[float]:
        """
        Smash factor: ratio of ball speed to club speed.

        Indicates quality of contact:
        - Driver: 1.44-1.50 (optimal)
        - Irons: 1.30-1.38
        - Wedges: 1.20-1.25

        Returns:
            Smash factor, or None if club speed is unavailable or the ratio
            exceeds MAX_SMASH_FACTOR (physically impossible, so club speed
            was mis-measured)
        """
        return compute_smash_factor(self.ball_speed_mph, self.club_speed_mph)

    @property
    def estimated_carry_yards(self) -> float:
        """Estimated carry distance based on ball speed, club type, and launch angle."""
        base = estimate_carry_distance(self.ball_speed_mph, self.club)
        if self.launch_angle_vertical is not None:
            return adjust_carry_for_launch_angle(
                base,
                self.launch_angle_vertical,
                self.club,
                self.launch_angle_confidence or 0.2,
            )
        return base

    @property
    def estimated_carry_range(self) -> tuple:
        """
        Return (min, max) carry distance estimate to show uncertainty.

        Returns:
            Tuple of (low_estimate, high_estimate) in yards
        """
        base = self.estimated_carry_yards
        # ±10% uncertainty without launch angle/spin data
        # Reduce to ±5% if we have launch angle data
        if self.has_launch_angle:
            return (base * 0.95, base * 1.05)
        return (base * 0.90, base * 1.10)

    @property
    def has_launch_angle(self) -> bool:
        """Check if launch angle data is available for this shot."""
        return self.launch_angle_vertical is not None

    @property
    def has_spin(self) -> bool:
        """Check if spin data is available for this shot."""
        return self.spin_rpm is not None

    @property
    def spin_quality(self) -> Optional[str]:
        """
        Get spin measurement quality as a string.

        Returns:
            "high", "medium", "low", or None if no spin data
        """
        if self.spin_rpm is not None and self.spin_result_quality:
            return self.spin_result_quality
        if self.spin_confidence is None:
            return None
        if self.spin_confidence >= SPIN_CONFIDENCE_HIGH:
            return "high"
        if self.spin_confidence >= 0.4:
            return "medium"
        return "low"

    def to_dict(self) -> dict:
        """Return the canonical, unrounded representation of this shot."""
        return {
            "shot_number": self.shot_number,
            "ball_speed_mph": self.ball_speed_mph,
            "ball_speed_raw_mph": self.ball_speed_raw_mph,
            "ball_speed_status": self.ball_speed_status,
            "ball_speed_status_reason": self.ball_speed_status_reason,
            "club_speed_mph": self.club_speed_mph,
            "smash_factor": self.smash_factor,
            "estimated_carry_yards": self.estimated_carry_yards,
            "carry_range": list(self.estimated_carry_range),
            "club": self.club_selection or self.club.value,
            "profile_id": self.profile_id,
            "profile_name": self.profile_name,
            "timestamp": self.timestamp.isoformat(),
            "impact_timestamp": self.impact_timestamp,
            "peak_magnitude": self.peak_magnitude,
            "readings_count": len(self.readings),
            "readings": self.readings_data,
            "mode": self.mode,
            "demo": self.demo,
            "launch_angle_vertical": self.launch_angle_vertical,
            "launch_angle_horizontal": self.launch_angle_horizontal,
            "launch_angle_confidence": self.launch_angle_confidence,
            "launch_angle_vertical_confidence": self.launch_angle_vertical_confidence,
            "launch_angle_horizontal_confidence": self.launch_angle_horizontal_confidence,
            "launch_angle_vertical_source": self.launch_angle_vertical_source,
            "launch_angle_horizontal_source": self.launch_angle_horizontal_source,
            "launch_angle_vertical_rejection_reason": self.launch_angle_vertical_rejection_reason,
            "angle_source": self.angle_source,
            "club_angle_deg": self.club_angle_deg,
            "club_path_deg": self.club_path_deg,
            "experimental_attack_angle_deg": self.experimental_attack_angle_deg,
            "experimental_attack_angle_status": self.experimental_attack_angle_status,
            "experimental_club_path_deg": self.experimental_club_path_deg,
            "experimental_club_path_status": self.experimental_club_path_status,
            "experimental_fused_attack_angle_deg": self.experimental_fused_attack_angle_deg,
            "experimental_fused_club_path_deg": self.experimental_fused_club_path_deg,
            "experimental_fused_status": self.experimental_fused_status,
            "experimental_fused_attack_angle_confidence": (
                self.experimental_fused_attack_angle_confidence
            ),
            "experimental_fused_club_path_confidence": (
                self.experimental_fused_club_path_confidence
            ),
            "experimental_camera_trace_deg": self.experimental_camera_trace_deg,
            "experimental_aoa_offset_source": self.experimental_aoa_offset_source,
            "iwr6843_horizontal_deg": self.iwr6843_horizontal_deg,
            "iwr6843_horizontal_confidence": self.iwr6843_horizontal_confidence,
            "iwr6843_vertical_deg": self.iwr6843_vertical_deg,
            "experimental_camera_vertical_deg": self.experimental_camera_vertical_deg,
            "experimental_camera_vertical_confidence": (
                self.experimental_camera_vertical_confidence
            ),
            "angle_reference": (
                copy.deepcopy(self.angle_reference) if self.angle_reference else None
            ),
            "experimental_camera_horizontal_deg": self.experimental_camera_horizontal_deg,
            "experimental_camera_horizontal_confidence": (
                self.experimental_camera_horizontal_confidence
            ),
            "experimental_camera_horizontal_status": self.experimental_camera_horizontal_status,
            "experimental_camera_iwr_delta_deg": self.experimental_camera_iwr_delta_deg,
            "experimental_camera_time_zero": (
                dict(self.experimental_camera_time_zero)
                if self.experimental_camera_time_zero
                else None
            ),
            "experimental_camera_ball_locator": (
                dict(self.experimental_camera_ball_locator)
                if self.experimental_camera_ball_locator
                else None
            ),
            "camera_replay": dict(self.camera_replay) if self.camera_replay else None,
            "spin_axis_deg": self.spin_axis_deg,
            "spin_axis_source": self.spin_axis_source,
            "curve_class": self.curve_class,
            **{name: getattr(self, name) for name in CLUB_DATA_SHOT_FIELDS},
            "inclinometer": self.inclinometer,
            "spin_rpm": self.spin_rpm,
            "spin_rpm_measured": self.spin_rpm_measured,
            "spin_source": self.spin_source,
            "spin_method": self.spin_method,
            "spin_confidence": self.spin_confidence,
            "spin_quality": self.spin_quality,
            "spin_multipath_fade_hz": self.spin_multipath_fade_hz,
            "spin_snr": self.spin_snr,
            "spin_modulation_depth": self.spin_modulation_depth,
            "spin_peak_freq_hz": self.spin_peak_freq_hz,
            "spin_candidate_rpm": (
                self.spin_peak_freq_hz * 60 if self.spin_peak_freq_hz is not None else None
            ),
            "spin_seam_cycles": self.spin_seam_cycles,
            "spin_at_lower_rail": self.spin_at_lower_rail,
            "spin_at_upper_rail": self.spin_at_upper_rail,
            "spin_candidates": self.spin_candidates,
            "spin_phase_method": self.spin_phase_method,
            "spin_phase_rpm": self.spin_phase_rpm,
            "spin_phase_snr": self.spin_phase_snr,
            "spin_phase_agreement_pct": self.spin_phase_agreement_pct,
            "spin_phase_confirmed": self.spin_phase_confirmed,
            "spin_rejection_reason": self.spin_rejection_reason,
            "carry_spin_adjusted": self.carry_spin_adjusted,
            "carry_spin_rpm": self.carry_spin_rpm,
            "carry_spin_source": self.carry_spin_source,
            "typical_spin_table": self.typical_spin_table,
            "carry_model": self.carry_model,
            "carry_air_density_kg_m3": self.carry_air_density_kg_m3,
            "carry_air_density_source": self.carry_air_density_source,
            "carry_weather_age_s": self.carry_weather_age_s,
            "carry_standard_air_yards": self.carry_standard_air_yards,
            "carry_lateral_yards": self.carry_lateral_yards,
            "apex_yards": self.apex_yards,
            "flight_path": self.flight_path,
            "roll_yards": self.roll_yards,
            "total_yards": self.total_yards,
            "roll_path": self.roll_path,
        }


def summarize_shots(shots: List[Shot], mode: str) -> dict:
    """Calculate the shared session summary for a list of shots.

    ``shot_count`` counts every shot. The averages, extremes and the spin
    detection rate leave out shots whose ball speed is "uncertain" (the OPS
    ball-track guard kept a ball speed that reads like the club head's trailing
    track); with only such shots they read as for an empty session.
    """
    counted = len(shots)
    shots = [shot for shot in shots if shot.ball_speed_status != BALL_SPEED_UNCERTAIN]
    summary = {
        "shot_count": counted,
        "avg_ball_speed": 0,
        "max_ball_speed": 0,
        "min_ball_speed": 0,
        "avg_club_speed": None,
        "avg_smash_factor": None,
        "avg_carry_est": 0,
        "avg_spin_rpm": None,
        "spin_detection_rate": 0,
        "mode": mode,
    }
    if not shots:
        return summary

    ball_speeds = [shot.ball_speed_mph for shot in shots]
    club_speeds = [shot.club_speed_mph for shot in shots if shot.club_speed_mph]
    smash_factors = [shot.smash_factor for shot in shots if shot.smash_factor]
    spin_rpms = [shot.spin_rpm for shot in shots if shot.spin_rpm is not None]
    summary.update(
        {
            "avg_ball_speed": statistics.mean(ball_speeds),
            "max_ball_speed": max(ball_speeds),
            "min_ball_speed": min(ball_speeds),
            "std_dev": statistics.stdev(ball_speeds) if len(ball_speeds) > 1 else 0,
            "avg_club_speed": statistics.mean(club_speeds) if club_speeds else None,
            "avg_smash_factor": statistics.mean(smash_factors) if smash_factors else None,
            "avg_carry_est": statistics.mean([shot.estimated_carry_yards for shot in shots]),
            "avg_spin_rpm": statistics.mean(spin_rpms) if spin_rpms else None,
            "spin_detection_rate": len(spin_rpms) / len(shots),
        }
    )
    return summary
