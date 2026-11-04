---
icon: lucide/smartphone
---

# Pairing Devices

The Pi's own screen always works. Every other device — a phone, a tablet, a TV
browser, a laptop on the LAN or over Tailscale — must be paired once before it
can use OpenFlight. This covers the normal UI, `/display`, `/companion`, the
analysis page, the HTTP API, and live updates. A paired device has the same
rights as the Pi's screen, including shutdown and deleting shots.

## Pair a device

1. On the Pi's screen, open the menu and tap **Pair device**.
2. On the phone, scan the QR code with the camera. The link opens
   `/pair`, pairs the phone, and continues to the app, which a phone in
   portrait shows in its own layout. The companion stays at `/companion`.
3. Without a camera, open the address shown under the QR code (for example
   `http://openflight.local:8080/pair`) and type the eight-character code.
   Opening any OpenFlight page on an unpaired device also shows the code entry.

A code works once and only for five minutes; **New code** replaces it. Five
wrong codes from one device block it for a minute, and ten wrong codes in total
retire the code.

The pairing belongs to the browser and the address it used. If the QR code
offers the hostname and the IP address, pair through the one you will use.
On iPhone and iPad, an app added to the home screen keeps its own cookies:
open it and type a fresh code once.

## Sign out all devices

Menu → **Sign out all devices** unpairs every phone, tablet, and computer at
once and closes their live connections. The Pi's screen stays signed in.

The pairing secret lives in `~/.config/openflight/pairing-secret` (mode 0600)
and survives restarts. Signing out all devices replaces it; deleting the file
has the same effect at the next start.

## Security notes

- OpenFlight serves plain HTTP. On a network you do not trust, anyone who can
  watch the traffic can copy a paired device's cookie. Use your own Wi-Fi or
  Tailscale, which encrypts the connection, and never expose port 8080 to the
  internet.
- The Pi trusts requests that come from itself (`localhost`) without proxy
  headers. A reverse proxy or port forwarder on the Pi, such as
  `tailscale serve`, must add `X-Forwarded-For`; otherwise every device it
  forwards counts as the Pi itself. The Vite dev server (`npm run dev`) proxies
  `/api` the same way, so keep it bound to `localhost`, its default.
- Scripts on the Pi should call `http://localhost:8080` or
  `http://127.0.0.1:8080`. From another computer, pair a browser or tunnel the
  port with `ssh -L 8080:localhost:8080 <pi>`.
