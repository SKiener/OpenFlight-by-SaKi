# Rejected horizontal fallback must not remain selected

When camera reconstruction is unavailable, `select_camera_assisted_horizontal`
can return `camera_withheld_iwr_implausible` with no selected angle. The server
previously only copied non-null decisions, leaving the earlier IWR selection
and its confidence intact. The UI and subsequent calculations could therefore
still receive a direction that the selector had explicitly rejected.

The fix clears the selected horizontal angle, confidence, and source for that
specific rejection. Raw IWR evidence, vertical launch, ball speed, valid radar
fallbacks, and accepted camera results are retained. It does not add or alter
the selector's existing 20-degree fallback policy.

Regression controls at -25 and +25 degrees failed before the four-line fix
and pass after it. This is an independently reproduced server bug, **not** an
explanation or correction of the recorded approximately 10-degree TI bias.
No phase zero, spin default, launch calibration, or historical shot is changed.
