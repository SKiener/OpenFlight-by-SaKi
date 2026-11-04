# OpenFlight iPhone companion

A thin native shell around the launch monitor's own web UI, plus the sensors a page served
over plain http cannot use. Design: [`docs/ios-companion/design.md`](../docs/ios-companion/design.md).

Stage 1: the `WKWebView` shell and `window.openflightNative` with `getSensors()`
(barometer, GPS, network, battery), `fetchWeather()` (Open-Meteo over cellular, falling back to
the default route) and `setKeepAwake()`.

Stage 2: the impact sound. While the app is not in the background and the launch monitor's
page is loaded, the microphone runs at 48 kHz in measurement mode (`Audio/`): a realtime C ring,
a streaming onset detector, sub-ms AIC refinement, features and a fat-strike cue, the host-time
fit, and one `audio_onset` event per impact to the page (`CustomEvent('openflight-native')`),
plus `audio_status`. Everything is log-only. The settings sheet (gear) switches capture and the
debug WAVs (±200 ms per onset, at most 500 files, `Documents/AudioCaptures/`) and exports them
as a zip through the share sheet. The page can use `audio.start/stop/status/onsets/
exportCaptures`. Details and measured accuracy: design §9 and "Stage 2 decisions" at its top.

## Build and test (Simulator)

```bash
cd ios
xcodegen generate
xcodebuild -scheme OpenFlightCompanion -destination 'platform=iOS Simulator,name=iPhone 17' \
  -derivedDataPath build/DerivedData build test
```

The generated `OpenFlightCompanion.xcodeproj` and `build/` are not committed. Both targets
compile Swift with `-O` in Debug as well: the per-sample audio code would otherwise cost about
half a core. The synthetic-signal tests (`OnsetDetectionTests`, `CapturePipelineTests`) take a
few seconds.

## Run against the mock backend

```bash
# repo root: build the UI the server serves, then start the mock server
(cd ui && npm run build)
uv run openflight-server --mock --host 127.0.0.1 --web-port 8361 --no-logging \
  --hotspot-ssid TestNet --hotspot-password e2e-dummy-pass-1234

# ios/: install and launch, pointed at it
APP=build/DerivedData/Build/Products/Debug-iphonesimulator/OpenFlightCompanion.app
xcrun simctl install booted "$APP"
xcrun simctl privacy booted grant location com.example.openflight
xcrun simctl location booted set 48.1374,11.5755
xcrun simctl launch --console-pty booted com.example.openflight \
  -baseURL http://127.0.0.1:8361 -bridgeSelfTest YES
```

- `-baseURL` overrides the stored server address for that launch (UserDefaults argument domain);
  `-audioCaptureEnabled NO` and `-debugCaptureEnabled YES` work the same way.
- Audio in the Simulator uses the Mac's microphone. The first start shows the microphone prompt
  (`xcrun simctl privacy booted grant microphone com.example.openflight` did not suppress it
  on iOS 27.1). Debug WAVs land in
  `$(xcrun simctl get_app_container booted com.example.openflight data)/Documents/AudioCaptures`.
- `-bridgeSelfTest YES` (Debug builds only) calls `getSensors()` and `fetchWeather()` from the
  loaded page once and prints the JSON as `[bridgeSelfTest] …`.
- Loopback makes the server treat the Simulator as the kiosk (no pairing). The Simulator has no
  barometer (`barometer: null`) and no cellular (`transport: "default"`).
- Debug builds set `isInspectable`, so Safari's Web Inspector can attach.

## On the iPhone

Signing: your team, bundle id `com.example.openflight`, automatic, no
entitlements. The default server is `http://openflight.local:8080`; the gear button opens the
settings (address override, reload, back). The app has its own cookie jar, so the phone pairs
once more inside the app. Device checks to do: design §12.3.
