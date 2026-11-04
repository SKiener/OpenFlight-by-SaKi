# OPS speed trigger (mic-free shot trigger, B2)

OPS243-A firmware 1.3.3 can trip its own rolling buffer: an outbound reading
faster than `ST` (mph) with at least `SM` magnitude freezes the buffer around the
trip, and J3 pin 1 goes HIGH "until the Rolling Buffer sampling has completed"
(vendor README). Wired to a Pi input, pin 1 can replace the microphone edge.

The code ships **off**. `--shot-trigger sound` stays the default, and the two
new modes refuse to start (back to the microphone, one clear log line) until
the board runs 1.3.3, pin 1 reads inactive at idle and the off command is known.
Do the steps below in order; each one needs the one before it.

| Step | Where | What |
| --- | --- | --- |
| M1 | home, app stopped | Flash 1.3.3, measure pin 1, run the bench script |
| M2 | code | Done: this guide, the flags and the tests |
| M3 | range | `--shot-trigger ops-speed-shadow`: the microphone still decides, pin 1 is logged |
| M4 | range | `--shot-trigger ops-speed`: pin 1 drives everything, the microphone is a witness |
| M5 | range | Without the microphone |

## Wiring

| OPS243-A | Pi |
| --- | --- |
| J3 pin 1 (speed-trigger output) | GPIO22, header pin 15 |
| GND | GND, header pin 14 |

GPIO22 is free in this build (no reference in `src/`, `scripts/`, `docs/` or
`config/`). The microphone stays on GPIO17 and HOST_INT on GPIO27
(`--ops-trigger-pin 27`, held LOW by the Pi).

!!! warning "Measure pin 1 before you connect it"

    The level, the driver type and the polarity of pin 1 are vendor statements
    only. Check with a meter: idle level, level while the trigger holds it
    (it must be 3.3 V, not 5 V), push-pull or open drain. A wrong level can
    damage the Pi input.

## M1: bench test at home

1. Flash firmware 1.3.3 (see the flash guide for the Debug Probe). Do not save
   anything with `A!`.
2. Stop the app: it owns the radar UART and the GPIOs.
3. Measure pin 1 (see above), then wire it.
4. Run the bench script with a temporary low threshold and wave a hand in
   front of the radar ten times:

    ```bash
    uv run python scripts/hardware-test/test_ops_speed_pin.py --magnitude 20
    uv run python scripts/hardware-test/test_ops_speed_pin.py --magnitude 20 --upstream-settings
    uv run python scripts/hardware-test/test_ops_speed_pin.py --magnitude 20 --host-int-test
    ```

    It never sends `GC` or `GS`, and `A!` only with `--save-to-flash` and a typed
    confirmation. It writes `?V`, `??` and every try to a results file.
5. Power-cycle the OPS afterwards. Without `A!` that clears `ST`/`SM`.

M1 must answer, and the code needs these answers before it can be switched on:

| Question | Where it goes |
| --- | --- |
| The command that switches the speed trigger off | `ops243.SPEED_TRIGGER_OFF_COMMANDS` (empty until then: the modes refuse) |
| Pin 1 level, polarity and driver | `--ops-speed-pin-active`, `--ops-speed-pin-pull` |
| Do `ST`/`SM` work on the persisted `GC` mode without `GC`, at 20 ksps? | Otherwise decide on 30 ksps (S#2 then keeps only 128 ms after the trip) |
| `??` after a dump: are `ST`/`SM` still set? | `OPS243Radar.speed_trigger_resend_on_rearm` |
| The bytes the board sends when it trips (the trip message) | dump-marker handling (`SPEED_TRIGGER_MARKERS`) |
| Pin 1 HIGH width (expected 192 ms at S#2 / 20 ksps) and rise to first byte | sanity of the witness and the gate match |
| Does HOST_INT still work on 1.3.3, and does pin 1 toggle with it? | the shadow fallback; the witness |
| Do buffer size 128, `X=2`, `R-`, `R>` (upstream restores them after `GC`) shift the trip? | `--upstream-settings` |
| A starting `SM` value | `--ops-speed-magnitude` |

## M3: shadow at the range

Add to the launcher (the other flags of today's setup stay):

```bash
--shot-trigger ops-speed-shadow --ops-speed-pin 22 --ops-speed-magnitude <SM from M1>
```

The microphone still decides. A dump counts only when a microphone edge comes
0–60 ms after its pin edge; a dump no microphone edge claimed becomes
`ops_speed_unmatched` (full I/Q logged, no shot). A microphone edge without a
pin edge before it pulses HOST_INT as today. That recovers the shot only when
the OPS did not trip by itself (club below `ST`, `SM` too high) and HOST_INT
works on 1.3.3. The OPS split is `--ops-speed-pre-trigger` (S#2), so a fallback
dump keeps only 12.8 ms before HOST_INT; its club speed may be missing.

When the OPS trips but the Pi does not see pin 1 (a loose or unwired pin 1
reads idle, so the start check passes), the board is already dumping when the
microphone edge pulses HOST_INT, and ignores the pulse. That dump began before
the pulse: its first byte comes less than 1 ms after the pulse plus the
post-trigger span, earlier than any HOST_INT dump (shortest 1.15 ms in 197
shots). It is logged as `ops_speed_dump` `self_trip`, rejected as
`ops_speed_unmatched` (no shot) and anchored 28 ms after its trip, and two such
dumps in a row log `pin_silent`. Shots are lost until pin 1 is fixed. A trip
whose first byte comes late (a first-byte lag more than 1 ms above the
microphone delay; the reader's 20 ms poll is part of that lag, which reached
27 ms in the logged shots) cannot be told from a HOST_INT dump and still counts
as a fallback shot anchored at the pulse (about 26 ms too early), so act on the
first `pin_silent`.

What to read from the session log (`ops_speed_trigger` entries, see the
[session log](../reference/session-log.md)):

- `matched` `delta_ms` (microphone minus pin): its median sets
  `--ops-speed-ti-delay-ms`, and the anchor `--ops-speed-anchor-ms` is chosen
  from the same distribution. Both defaults (25 ms and 28 ms) are model values
  from shots at P0 / 30 ksps; B2 runs at P3 / 20 ksps.
- `pin_without_mic`: neighbours and practice swings that B2 would have taken.
- `mic_without_pin` with `ops_state` `armed`: shots B2 would have missed.
- `ops_speed_practice_swing` on every capture: how the practice-swing rule
  would have decided. It stays off (`--ops-speed-reject-practice-swings`)
  because it already drops 2 of 197 real shots.

## M4: pin 1 drives the shot

```bash
--shot-trigger ops-speed --ops-speed-pin 22 --ops-speed-magnitude <SM> \
  --ops-speed-ti-delay-ms <median from M3> --ops-speed-anchor-ms <from M3>
```

GPIO27 stays LOW and is never pulsed. The pin edge freezes the camera ring,
records the gate edge and starts the TI `l3dump` the TI delay later. The
microphone only feeds the witness. If three microphone edges in a row come
without a pin edge while the OPS was armed (`--ops-speed-stall-mic-edges`), the
OPS is re-armed once (`trigger_event` reason `ops_speed_stall_rearm`). Two dumps
in a row without a pin edge log `pin_silent`: check the wiring and polarity.

The stall rule cannot tell a stuck board from a busy range: the microphone
hears your neighbours too, and three of their impacts between two of your
swings (while the OPS is armed) make a streak. Each streak re-arms a working
board as well, which leaves it blind for about 0.1–0.6 s (drain, `PA` / `S#n` /
`PA`). Microphone edges while the OPS is blind (from a trip until its dump can
arrive, then the dump and its re-arm) count as `busy` and break a streak. In M4
count the `stall_rearm` entries against the `trigger_event` entries; if they
come often, raise `--ops-speed-stall-mic-edges` or set it to 0 (then only the
300 s long-stall re-arm catches a stuck board).

## M5: without the microphone

Unplug the microphone only after M4 ran clean. A dead pin 1 then loses shots
silently: the pin-silent detector and the idle-level check at start help, but
there is no witness any more.

## Related

- [CLI flags](../reference/cli.md#shot-trigger-ops243-a-133-speed-trigger)
- [Session log](../reference/session-log.md)
- [Rolling buffer](../how-it-works/rolling-buffer.md)
