# Local camera contrast correction, 2026-10-04

This device change recognizes both dark silhouettes and bright flying balls.
Candidate components must change over time and retain local spatial contrast.
Global brightness shifts, vacated-background ghosts, missing ball departure,
weak reference contrast, unsuitable shutter times and missing exposure metadata
must not silently produce a camera measurement. Existing shape, size, speed,
depth and trajectory-consensus checks remain in place.

Capture metadata now separates `ball_analysis_eligible` from the existing
whole-scene/club `analysis_eligible`. The former permits an attempt at local
ball analysis in backlight; it does not certify a trajectory or unlock club
delivery. Old recordings without the new key retain their original gating.

Offline evidence: 45 linked Oct 3 recordings, 41 normal TI replays, four
explicit recovery/rejection exclusions. The updated estimator accepts 40/41;
the archived detector accepted 0/41. Repeated-static-frame controls produce no
accepted trajectories. On 36 robust Toptracer pairings the median absolute
vertical difference is 0.925 degrees, but horizontal has a +3.563 degree median
offset (maximum absolute difference 7.196 degrees). Target alignment is not
independently established. No fitted offset or geometry correction is applied.
Detector consensus is not independent measurement validation.

Local validation: 433 tests pass, one pre-existing test skips because its April
session fixture is absent. Ruff checks pass. Pylint on the three changed source
modules meets the required 9.0 threshold (9.56/10), with existing OpenCV dynamic
binding and large-module warnings. Unrelated server formatting is unchanged.

No changes to OPS speed/spin extraction, TI angle processing, calibration,
replay, UI, camera controls, dependencies or startup arguments are included.
Real post-install shot validation remains necessary. Camera values stay
experimental; the camera vertical estimate is not substituted for TI launch.

Rollback: restore only the files from the deployment's `files-before.tar.gz`,
remove/revert the newly added camera-specific tests/documentation if needed,
and restart OpenFlight normally. Do not reset the entire repository or alter
calibration. Roll back if camera/radar startup fails, saved replay retrieval
breaks, or inspection reveals false ball associations.

AI assistance: Codex drafted and reviewed the correction and tests. Tests and
offline replays were executed, but are not claimed as human hardware testing.
