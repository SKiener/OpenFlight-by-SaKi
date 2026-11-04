# Horizontal ring-tail ordering — 2026-10-04

The horizontal proxy groups phase snapshots by frame and uses the final eight
frames. The producer previously supplied physical ring-slot numbers. Sorting
those numbers does not recover capture order when the ring wraps. It now supplies
the frame-start time from the header-derived geometry. Loop weighting, phase
conversion, confidence threshold, vertical processing and calibration are unchanged.

Verification: 10 of 12 synthetic ring-storage cases failed before the fix; all
12 pass after it, including variable frame cadence. All 45 Oct 3 source captures
use trigger slot zero, and their complete results are unchanged. In 123 controls
made by rotating the decoded storage slots of 41 accepted captures while retaining
their actual times, the old horizontal result changed in 106 cases. The fixed
result is invariant in all 123 cases. These are offline controls, not new shots.

This is not a fix for the remaining launch-direction discrepancy in that session:
the current horizontal proxy still measures a late-flight bearing, not a fitted
flight direction, and the electrical phase reference is uncalibrated. Experimental
trajectory/phase-zero fitting of the saved data was not sufficiently stable and
has not been enabled. No Toptracer-derived offset or gain has been installed.

Rollback: revert the dedicated local commit, then restart only OpenFlight.
The deployment backup contains the previous repository bundle and source.
