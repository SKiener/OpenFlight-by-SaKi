# Signal Capture Analysis

Capture OPS243-A rolling-buffer I/Q data, then render time-domain and spectral
plots:

```bash
uv run python scripts/analysis/capture_iq.py
uv run python src/analysis/analyze_capture.py \
  ~/openflight_sessions/capture_<timestamp>.pkl
```

The radar must first be configured for persistent rolling-buffer mode; see
[`docs/rolling_buffer_spin_detection.md`](../../docs/rolling_buffer_spin_detection.md).
More focused replay and comparison tools live in `scripts/analysis/`.
