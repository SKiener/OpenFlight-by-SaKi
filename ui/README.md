# OpenFlight UI

The OpenFlight dashboard: a React + TypeScript + Vite app that connects to the
backend over `socket.io`. The kiosk is a tabbed instrument panel (Live, Stats,
Shots, Camera, Profiles, Debug) plus a screen-mounted display mode at `/display`.

This README covers frontend development. For the hardware, the radar pipeline,
and how the whole system fits together, see the [root README](../README.md).

## Quick start

You need Node 22.12 or newer (`electron@44` will not install cleanly on Node 20).

### Frontend-only (recommended for UI work)

Runs Vite plus a Node Socket.IO mock on port `8080` — no Python, hardware, or
`uv` required:

```bash
npm install
npm run dev:mock
```

Open `http://localhost:5173`. Use **Simulate** to generate shots. The mock
speaks the same Socket.IO events as the real backend (`shot`, `session_state`,
club/profile changes, clear/delete, stub cloud upload and shutdown).

### Against the real / Python mock backend

Start a backend without hardware from the repo root:

```bash
scripts/start-kiosk.sh --mock
```

Then in `ui/`:

```bash
npm install
npm run dev
```

See the [root README](../README.md#getting-started) for full-stack options.

The Vite server runs on port `5173`. When served there, the UI assumes the
backend is at `http://localhost:8080`. Point it elsewhere with
`VITE_SOCKET_URL`:

```bash
VITE_SOCKET_URL="http://localhost:8081" npm run dev
```

`/api/*` requests from the Vite origin are proxied to `http://localhost:8080`
(so shutdown works under `npm run dev` / `dev:mock`).

## Scripts

| Script                 | Description                                  |
| ---------------------- | -------------------------------------------- |
| `npm run dev`          | Dev server with hot reload (needs a backend) |
| `npm run dev:mock`     | Vite + Node mock backend on port 8080        |
| `npm run mock-server`  | Node mock Socket.IO server alone             |
| `npm run build`        | Type-check, build, and check the bundle      |
| `npm run preview`      | Serve the production build locally           |
| `npm run lint`         | ESLint                                       |
| `npm run test`         | Vitest unit tests                            |
| `npm run format`       | Format `src/` with Prettier                  |
| `npm run format:check` | Check formatting without writing             |

## How the UI connects

The app is entirely client-side. Everything flows through one socket connection.

- **`utils/serverOrigin.ts`** resolves the backend origin: `VITE_SOCKET_URL` if
  set, otherwise `http://localhost:8080` when running on the Vite dev port
  (`5173`), otherwise the page's own origin (the production case, where the
  backend serves the built UI).
- **`services/socketService.ts`** owns the connection. It receives events like
  `shot`, `session_state`, `camera_capture_settings`, and `trigger_status`, and
  sends commands like `set_club`, `clear_session`, `simulate_shot`, and
  `set_camera_capture_settings`. Read it before assuming what the
  backend emits. **`mock-server/`** implements that contract in Node for
  `npm run dev:mock`.
- **State** lives in `stores/` (Zustand: shots, system, camera, debug, profiles, …).
- **Shutdown** posts to `/api/shutdown` to stop the connected backend (stubbed
  as a no-op by the Node mock).

### Profiles socket contract

Shots are attributed to a server-owned **profile** — a person or a place — with
a stable id. There is no browser-local roster: the server is the only source of
truth, sent as one snapshot plus five mutations.

Server → client:

- `profiles` — `{ profiles: Profile[], active_profile_id: string }`. Sent on
  connect and after every mutation below. `Profile` is
  `{ id, name, created_at, settings }`.
- `session_cleared` — `{ profile_id: string, shots: Shot[] }`, sent after
  `clear_session`.

Client → server:

- `get_profiles` — request a fresh `profiles` snapshot.
- `set_active_profile` — `{ profile_id }`. Switches which profile new shots
  attribute to.
- `add_profile` — `{ name }`. Creates a profile and makes it active.
- `rename_profile` — `{ profile_id, name }`. Renames in place; the id (and
  every shot already attributed to it) is unchanged.
- `remove_profile` — `{ profile_id }`. The server refuses to remove the active
  profile, the last remaining profile, or a profile that still has session
  shots. Clear that profile's session first.
- `clear_session` — `{ profile_id }` (defaults to the active profile).
  Deletes that profile's shots; other profiles are untouched.

Shots carry `profile_id` and `profile_name` (a snapshot at capture time) for
logging / external consumers. The kiosk UI renders the current profile name
from the `profiles` roster (joined by `profile_id`), so renaming updates the
on-screen name for past shots.

**Kiosk shell.** Footer tabs switch views. The footer logo opens a sheet for
units (MPH/YDS vs KMH/M), dark/light theme, language, and simulator status.
The footer power icon is always visible and opens a
shutdown confirmation. Change club (or training implement) lives on the Live
header. Tap a Live metric to pin it top-left while keeping all metrics visible.
Camera-backed shots add a **Replay** action in the Live header and a play button
in Shots. Selecting either action asks the backend to create and cache a 60 FPS
MP4 (or reuse the cached one). With camera capture enabled, the backend also
prepares each finalized shot's MP4 in a low-priority background worker once no
shot is in flight, then emits `camera_replay_ready`
(`{ shot_number, replay_id, video_url }`). The full-screen player includes touch
controls, a scrubber, an impact marker, and retryable preparation/playback
errors.

The pin is stored in
`localStorage` under `openflight.hero-metric`. Theme is stored under
`openflight.theme` (default dark). The area that was open last is stored under
`openflight.last-area:v1`, so a reload reopens the Range only if it was the
last area. Those keys live in the **current browser
profile**. Electron does not share Chromium's profile; see
[Electron Kiosk Shell](../docs/electron-kiosk-shell.md#browser-local-state-breaking-on-first-electron-launch).

**Range** (kiosk only) flies every shot over a 3D driving range that fills the
screen, with the header and footer lying over it; three.js loads in its own
chunk when the Range opens. Only a page whose host is `localhost`,
`127.0.0.1` or `[::1]` (the Pi's own screen) gets it, including **Show on
range** in the history. On slow frames it drops anti-aliasing, then half the
vegetation, and keeps that step in `sessionStorage` under
`openflight.virtual-range.quality:v1`: it holds through reloads, and a kiosk
restart measures at full quality again. After the flight the ball rolls out to
its rest point, where the sign shows the Total. Tap a green to aim at it: the
range turns it straight ahead, and a second tap releases it. At rest a dashed
line runs to the target (the selected green, else the nearest flag) with its
distance, and the bar shows Carry · Total · to target. **Training** sets a
target distance with its buttons and scores each shot at its rest point in
zones of ±3, 6 and 10 %. `?rangeDemo=1` opens it with three demo flights,
each rolling out, for render measurements and logs one `OF_RESULT` line once
the third flight and its pause are over, about 51 s in (90 s at most). The
menu's **Demo mode** switch is unrelated: every screen shows the server's demo
session (six shots, then one every 20 s; real shots are ignored).

**Display mode** lives at `/display`: a compact, fullscreen-friendly dashboard
for mounted screens and TVs. The [root README](../README.md#tv-display-mode)
covers casting it.

**Companion mode** lives at `/companion`: a read-only phone/tablet view of the
live shot (carry, ball speed, launch, spin, club speed, smash), the session's
shot list, and an inline slow-motion replay (default 0.25×, also 1×/0.5×/0.1×).
It plays a replay as soon as the backend emits `camera_replay_ready`; if that
event was missed (late connect), it posts `prepare` itself and retries 409/503
with backoff. It sends no mutating socket commands. A web app manifest scoped to
`/companion` lets iOS/Android add it to the home screen; there is no service
worker. E2E: `npx playwright test -c playwright.companion.config.ts` (Node mock
backend, system Chrome, iPhone 390×844 and iPad 1024×768).

**Pairing** gates every page except `/pair` for clients other than the Pi
itself: `PairingGate` asks `GET /api/pairing/status` and shows the code entry
when the server answers `unpaired` or refuses the socket with
`pairing_required`. `/pair#code=…` (the kiosk's QR link) exchanges the code and
opens `/companion`; a typed code reloads the current page. The kiosk menu shows
**Pair device** and **Sign out all devices** only when the status is `local`.
Pairing requests use same-origin paths so the Vite dev proxy keeps the cookie.
E2E: `companion-pairing.spec.ts` (Node mock; `POST /mock/pairing` turns pairing
on) and `pairing-dialog.spec.ts` (kiosk dialog at 800×400, 800×480, 1024×600).

Its **Streuung** (dispersion) tab plots carry over lateral offset (left −,
right + seen from behind the ball) on equal axes, one colour per club in bag
order, with each club's mean and 2σ ellipse (≈86 % of a 2-D normal, from five
shots) and a per-club mean ± SD table. Ranges: current session, or 7/30 days
from `/api/session-history` (fetched once, merged with live shots). Lateral is
the server's `carry_lateral_yards`; older shots fall back to carry × sin(HLA)
and, like estimated or low-confidence directions, are drawn hollow. Uncalibrated
aim (`session_state.calibration`), substitute-spin carry and shots without a
direction are called out. Tapping a shot opens its values and replay.

**Launch Daddy** is a hidden overlay. Five taps on the header connection LED
and title toggle it. When on, new shots can fire an animation; tap the brand
mark to turn it off.

Touch and type conventions for the Pi kiosk are in [`AGENTS.md`](./AGENTS.md).

## Languages

UI copy lives in `src/i18n/`. English, German, and Spanish ship today. Pick a
language from the footer menu (**Language** buttons). The choice is stored in
`localStorage` under `openflight.locale:v1`.

### Add a language

1. Copy `src/i18n/en.ts` to `src/i18n/<code>.ts` (use a short BCP 47 language
   code such as `de` or `ja`).
2. Translate every value. Keep the same keys; TypeScript will fail the build if
   a key is missing.
3. Register it in `src/i18n/index.ts`:
   - Add the id to `LocaleId`.
   - Add `{ id, nativeName, htmlLang }` to `LOCALES` (`nativeName` is the
     language’s own name, shown in the dropdown).
   - Import the catalog and add it to `catalogs`.
4. Run `npm test` — a catalog that drifts from English keys fails.

Do not translate profile names, club tile codes (`7i`, `DR`), or unit
abbreviations (`MPH` / `YDS`).

## Project layout

A few files do most of the work. This is illustrative, not exhaustive —
components carry co-located `.css` and `.test.tsx` files.

```text
src/
  App.tsx                    # tabs, display routing, session wiring
  main.tsx                   # entry point (applies stored theme)
  i18n/                      # EN/DE/ES catalogs
  theme/                     # dark/light tokens
  services/socketService.ts  # socket connection, events, backend commands
  hooks/useSocket.ts         # connects on mount
  utils/serverOrigin.ts      # backend origin resolution
  stores/                    # Zustand (shots, system, camera, profiles, …)
  components/
    panel/                   # Live, Stats, Shots, Camera, Profiles, chrome
    ui/                      # MetricCard, TabBar, Button, SegmentedControl
    DisplayMode.tsx          # /display
    DebugPanel.tsx
    LaunchDaddy/             # hidden overlay
mock-server/                 # Node Socket.IO mock for frontend-only development
```

## Troubleshooting

**Socket won't connect.** Confirm a backend is running on port `8080` (or set
`VITE_SOCKET_URL`). For UI-only work, use `npm run dev:mock`. Connection logs
come from `services/socketService.ts`.

**Build fails.** `npm run build` surfaces TypeScript and bundling errors; `npm
run lint` catches the rest.

---

Contributing guidelines (setup, code quality, PRs) live in
[CONTRIBUTING.md](../CONTRIBUTING.md).
