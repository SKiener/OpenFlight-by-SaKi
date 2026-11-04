# Local analysis and downloads

Open `http://openflight.local:8080/?view=analysis` on a device on the same network while OpenFlight is running. The session-history dialog also has an **Analysis & export / Auswertung & Export** button. There is no cloud upload. A device other than the Pi's own screen must be [paired](using/pairing.md) once. Do not expose the existing OpenFlight server to the public internet.

The page reads the existing 30-day session-history API. It does not start a measurement session, connect a live sensor socket, change calibration, or rewrite saved measurements. Refresh explicitly to include new shots. A failed session is flagged; partial downloads contain only the sessions that loaded.

## Filters and statistics

Filter by session, club, profile and local calendar dates. Speed and distance units are independent. DE/EN and light/dark appearance follow the existing browser preferences.

Carry uses `carry_spin_adjusted` when present, otherwise `estimated_carry_yards`. Both are estimates, not independently tracked distances. Missing values do not count as zero. Counts are shown per metric. Carry spread is sample standard deviation; fewer than two valid values yields no spread. All shots are retained, including mishits and outliers. Spin statistics exclude calculated, experimental and low-confidence values.

Charts show chronological Carry and mean Carry by club, with sample counts. The shot table has 50 rows per page, sortable by time, Carry or ball speed. Shot details show provenance, launch angles, spin and video availability.

## Downloads and replay

- **Shots CSV:** filtered rows in the selected speed/distance units, with initial/final Carry, spin source, candidate quality and replay metadata.
- **Club statistics CSV:** per-club sample counts, mean, median and sample standard deviation.
- **JSON:** the same filtered shots and statistics, preserving canonical mph/yards/degrees/rpm, with display-unit and incomplete-session metadata. This is an analysis export, not a backup of raw radar or camera frames.
- **MP4:** download the recording associated with one shot, if its saved frames remain available. Missing clips never remove the shot metrics. The replay player supports 0.1×, 0.05× and 0.025× using capture/playback timing metadata. A downloaded MP4 retains its encoded timing; it is not a new retimed rendering of the selected playback rate.

Charts are bundled locally; no CDN is contacted. Export is generated in the browser. Opening a replay may create the normal on-device MP4 playback cache. Original session logs and raw captures are not changed.

## Validation

Unit tests cover Carry selection, missing and zero values, spin provenance, filtering, sample statistics, spreadsheet-formula escaping, session-loading failures and request concurrency. Browser tests cover DE/EN dark/light at 1024×600, touch dragging/tapping, downloads, pagination and replay error handling. Simulated browser video responses do not constitute physical camera validation.
