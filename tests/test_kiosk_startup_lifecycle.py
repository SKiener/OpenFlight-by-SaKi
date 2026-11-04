"""Exercise launcher timing without starting a server or touching hardware."""

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/start-kiosk.sh"


def _run(fragment: str, setup: str, tail: str = "") -> str:
    result = subprocess.run(
        ["bash", "-c", setup + "\n" + fragment + "\n" + tail],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


# The Pi has no RTC: it boots on the fake-hwclock time and NTP later steps the
# wall clock, e.g. 00:27 -> 21:39 (+76,320 s) about 25 s into startup. Bash's
# SECONDS follows that wall clock; the launcher's deadlines must not.
CLOCK_STEPS = [(None, 0), (8, 76_320), (8, -76_320)]
CLOCK_STEP_IDS = ["steady-clock", "ntp-steps-forward", "ntp-steps-back"]


def _clock_setup(ticks_per_sleep: int, jump_at_tick: int | None, jump_by: int) -> str:
    """Fake `sleep`: 4 ticks per monotonic second; SECONDS jumps like the wall clock."""
    jump = (
        ""
        if jump_at_tick is None
        else f"(( ticks >= {jump_at_tick} )) && wall=$((wall + {jump_by}))"
    )
    return f"""
SECONDS=0
wall=0
monotonic_seconds() {{ printf '%s\\n' "$((ticks / 4))"; }}
sleep() {{
    ticks=$((ticks + {ticks_per_sleep}))
    wall=$((ticks / 4))
    {jump}
    SECONDS=$wall
}}
"""


def _monotonic_helper() -> str:
    script = SCRIPT.read_text(encoding="utf-8")
    start = script.index("monotonic_seconds() {")
    return script[start : script.index("\n}\n", start) + 3]


def test_monotonic_seconds_reads_uptime_not_the_wall_clock():
    output = _run(
        _monotonic_helper(),
        "SECONDS=86400",
        'printf "now=%s\\n" "$(monotonic_seconds)"',
    )
    if Path("/proc/uptime").is_file():
        uptime = int(float(Path("/proc/uptime").read_text().split()[0]))
        now = int(output.split("now=")[1])
        assert uptime <= now <= uptime + 2
    else:
        assert "now=86400" in output


def test_launcher_deadlines_never_use_the_wall_clock():
    script = SCRIPT.read_text(encoding="utf-8")
    code = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))
    assert "date +%s" not in code
    assert [line.strip() for line in code.splitlines() if "SECONDS" in line] == [
        "printf '%s\\n' \"$SECONDS\""
    ]


def _startup() -> str:
    script = SCRIPT.read_text(encoding="utf-8")
    start = script.index('log "Waiting for server to start')
    end = script.index('\nif [ -n "$STARTUP_STATUS_FILE" ]; then', start)
    return script[start:end]


@pytest.mark.parametrize(
    ("ready_after", "exit_at", "expected"),
    [(36, 9999, "READY"), (9999, 4, "exited during startup"), (9999, 9999, "timed out")],
)
@pytest.mark.parametrize(("jump_at_tick", "jump_by"), CLOCK_STEPS, ids=CLOCK_STEP_IDS)
def test_startup_allows_slow_hardware_but_still_detects_exit_and_timeout(
    ready_after, exit_at, expected, jump_at_tick, jump_by
):
    output = _run(
        _startup(),
        f"""
set -e
ticks=0
attempts=0
HOST=localhost
WEB_PORT=8080
SERVER_PID=999999
log() {{ :; }}
{_clock_setup(2, jump_at_tick, jump_by)}
curl() {{ attempts=$((attempts + 1)); (( attempts >= {ready_after} )); }}
kill() {{ (( ticks < {exit_at} )); }}
wait() {{ :; }}
show_startup_failure() {{ printf '%s seconds=%s\\n' "$2" "$(monotonic_seconds)"; exit 0; }}
""",
        'printf "READY\\n"; printf "seconds=%s\\n" "$(monotonic_seconds)"',
    )
    assert expected in output
    if expected == "READY":
        assert "seconds=17" in output
    elif expected == "timed out":
        assert "seconds=90" in output


def test_startup_health_requests_have_a_timeout():
    output = _run(
        _monotonic_helper() + "\n" + _startup(),
        """
set -e
HOST=localhost
WEB_PORT=8080
SERVER_PID=999999
log() { :; }
kill() { return 0; }
curl() {
    case " $* " in
        *" --max-time 2 "*) printf 'bounded request\n' >&2; return 0 ;;
        *) printf 'unbounded request\n' >&2; exit 9 ;;
    esac
}
show_startup_failure() { exit 8; }
""",
        "printf 'READY\\n'",
    )
    assert "READY" in output


@pytest.mark.parametrize(
    ("api_ready_at", "exit_ticks", "expected_term", "expected_kill"),
    [
        (4, 12, False, False),
        (0, 12, False, False),
        (9999, 12, True, False),
        (9999, 9999, True, True),
    ],
)
@pytest.mark.parametrize(("jump_at_tick", "jump_by"), CLOCK_STEPS, ids=CLOCK_STEP_IDS)
def test_shutdown_waits_for_hardware_even_before_http_is_ready(
    api_ready_at, exit_ticks, expected_term, expected_kill, jump_at_tick, jump_by
):
    script = SCRIPT.read_text(encoding="utf-8")
    shutdown = script[
        script.index("shutdown_server() {") : script.index("stop_startup_splash_server() {")
    ]
    output = _run(
        shutdown,
        f"""
set -e
ticks=0
requested=-1
terminated=-1
forced=0
HOST=localhost
WEB_PORT=8080
SERVER_PID=999999
log() {{ :; }}
warn() {{ :; }}
{_clock_setup(1, jump_at_tick, jump_by)}
wait() {{ :; }}
curl() {{
    if (( ticks >= {api_ready_at} )); then requested=$ticks; return 0; fi
    return 1
}}
kill() {{
    case "$1" in
        -TERM) terminated=$ticks; return 0 ;;
        -KILL) forced=1; return 0 ;;
        -0)
            if (( requested >= 0 && ticks - requested >= {exit_ticks} )); then return 1; fi
            if (( terminated >= 0 && ticks - terminated >= {exit_ticks} )); then return 1; fi
            return 0 ;;
    esac
}}
""",
        """
shutdown_server
printf 'terminated=%s forced=%s pid=%s seconds=%s\n' "$terminated" "$forced" "$SERVER_PID" "$(monotonic_seconds)"
""",
    )
    assert ("terminated=-1 " not in output) is expected_term
    assert ("forced=1 " in output) is expected_kill
    assert "pid= " in output
    if expected_kill:
        assert "seconds=40" in output
