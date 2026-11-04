# Spin/fade ambiguity safeguard — 2026-10-04

The fade fit shares its frequency band with possible ball modulation. It can
erase a pure 4500 rpm amplitude tone and report a different, lower-frequency
residual as a spin candidate. This is not evidence of low physical ball spin.

The estimator now records residual energy relative to the polynomial-detrended
input. The processor withholds a candidate if less than 0.01% remains, or if
less than 50% remains (raised from 2% in b5ab0e7) and residual peak-to-floor
evidence is below 5. Diagnostic
candidate and fitted fade are retained; no substitute spin is inserted.

These conservative numerical safeguards are not a validated spin classifier.
They do not determine whether a removed component was real spin or multipath.
The short-record counterexample is rejected; the distinct weak 9000 rpm tone
with a stronger 48 Hz fade remains an experimental candidate. Confidence is
not promoted and no club prior is added.

On 45 saved October 3 captures, none triggers this new safeguard and all
experimental candidates remain unchanged. Therefore this patch does **not**
solve their unreliable spin measurements or the associated carry discrepancy.
No hardware, raw evidence, or calibration is modified.
