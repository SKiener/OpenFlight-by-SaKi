"""Tests for the Pi 5 USB power cycle that recovers a wedged TI radar."""

from __future__ import annotations

import subprocess

import pytest

from openflight.iwr6843.usb_power import (
    AUTO_PORT_GLOB,
    DEFAULT_OFF_S,
    UHUBCTL,
    UsbPowerCycleError,
    UsbPowerCycler,
    usb_mounted_block_devices,
    wait_for_path,
    wait_for_tty,
)

OFF_2 = ["sudo", "-n", UHUBCTL, "-l", "2", "-a", "0"]
OFF_4 = ["sudo", "-n", UHUBCTL, "-l", "4", "-a", "0"]
ON_2 = ["sudo", "-n", UHUBCTL, "-l", "2", "-a", "1"]
ON_4 = ["sudo", "-n", UHUBCTL, "-l", "4", "-a", "1"]


class FakeClock:
    def __init__(self, now: float = 100.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Recorder:
    """Records runner calls and sleeps in one ordered event list."""

    def __init__(self, outcomes: dict | None = None):
        self.events: list = []
        self.kwargs: list[dict] = []
        self.outcomes = outcomes or {}
        self.clock = FakeClock()

    def run(self, command, **kwargs):
        self.events.append(("run", list(command)))
        self.kwargs.append(kwargs)
        outcome = self.outcomes.get(tuple(command))
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome is None:
            outcome = (0, "")
        returncode, stderr = outcome
        return subprocess.CompletedProcess(command, returncode, stdout="", stderr=stderr)

    def sleep(self, seconds):
        self.events.append(("sleep", seconds))
        self.clock.sleep(seconds)

    def cycler(self, **kwargs) -> UsbPowerCycler:
        return UsbPowerCycler(runner=self.run, sleep=self.sleep, clock=self.clock, **kwargs)

    @property
    def commands(self):
        return [event[1] for event in self.events if event[0] == "run"]


def test_commands_use_sudo_without_prompt_by_default():
    cycler = UsbPowerCycler()

    assert cycler.commands(0) == [OFF_2, OFF_4]
    assert cycler.commands(1) == [ON_2, ON_4]
    assert cycler.off_s == DEFAULT_OFF_S


def test_commands_without_sudo():
    cycler = UsbPowerCycler(use_sudo=False)

    assert cycler.commands(0) == [
        [UHUBCTL, "-l", "2", "-a", "0"],
        [UHUBCTL, "-l", "4", "-a", "0"],
    ]
    assert cycler.commands(1) == [
        [UHUBCTL, "-l", "2", "-a", "1"],
        [UHUBCTL, "-l", "4", "-a", "1"],
    ]


def test_cycle_runs_off_then_sleeps_then_on_and_returns_elapsed():
    recorder = Recorder()

    elapsed = recorder.cycler(off_s=2.5, command_timeout_s=7.0).cycle()

    assert recorder.events == [
        ("run", OFF_2),
        ("run", OFF_4),
        ("sleep", 2.5),
        ("run", ON_2),
        ("run", ON_4),
    ]
    assert elapsed == pytest.approx(2.5)
    assert all(
        kwargs == {"check": False, "capture_output": True, "text": True, "timeout": 7.0}
        for kwargs in recorder.kwargs
    )


@pytest.mark.parametrize(
    ("outcome", "message"),
    [
        ((1, "Permission denied: sudo: a password is required"), "exited 1: Permission denied"),
        (OSError("No such file or directory"), "raised OSError: No such file"),
        (subprocess.TimeoutExpired(OFF_2, 10.0), "timed out after 10s"),
    ],
    ids=["nonzero", "oserror", "timeout"],
)
def test_power_is_restored_when_an_off_command_fails(outcome, message):
    recorder = Recorder({tuple(OFF_2): outcome})

    with pytest.raises(UsbPowerCycleError) as info:
        recorder.cycler().cycle()

    assert recorder.commands == [OFF_2, OFF_4, ON_2, ON_4]
    assert " ".join(OFF_2) in str(info.value)
    assert message in str(info.value)


def test_power_is_restored_when_off_raises_a_non_exception():
    recorder = Recorder({tuple(OFF_2): KeyboardInterrupt()})

    with pytest.raises(KeyboardInterrupt):
        recorder.cycler().cycle()

    assert recorder.commands[-2:] == [ON_2, ON_4]


def test_failing_on_command_raises_after_all_on_commands_ran():
    recorder = Recorder({tuple(ON_2): (2, "x" * 500)})

    with pytest.raises(UsbPowerCycleError) as info:
        recorder.cycler().cycle()

    assert recorder.commands == [OFF_2, OFF_4, ON_2, ON_4]
    assert " ".join(ON_2) in str(info.value)
    assert "x" * 200 in str(info.value)
    assert "x" * 201 not in str(info.value)


def test_error_names_the_first_failing_command():
    recorder = Recorder({tuple(OFF_4): (1, "off failed"), tuple(ON_4): (1, "on failed")})

    with pytest.raises(UsbPowerCycleError, match="off failed") as info:
        recorder.cycler().cycle()

    assert "on failed" not in str(info.value)


def test_wait_for_path_returns_true_once_present():
    clock = FakeClock()
    checks = iter([False, False, True])

    assert wait_for_path(
        "/dev/serial/by-id/ti",
        present=True,
        timeout_s=5.0,
        exists=lambda _path: next(checks),
        clock=clock,
        sleep=clock.sleep,
    )
    assert clock.now == pytest.approx(100.2)


def test_wait_for_path_returns_true_once_absent():
    clock = FakeClock()
    checks = iter([True, False])

    assert wait_for_path(
        "/dev/serial/by-id/ti",
        present=False,
        timeout_s=5.0,
        exists=lambda _path: next(checks),
        clock=clock,
        sleep=clock.sleep,
    )


def test_wait_for_path_times_out():
    clock = FakeClock()

    assert not wait_for_path(
        "/dev/serial/by-id/ti",
        present=True,
        timeout_s=1.0,
        poll_s=0.25,
        exists=lambda _path: False,
        clock=clock,
        sleep=clock.sleep,
    )
    assert clock.now == pytest.approx(101.0)


def test_wait_for_path_checks_once_with_zero_timeout():
    clock = FakeClock()

    assert wait_for_path(
        "/x", present=True, timeout_s=0.0, exists=lambda _path: True, clock=clock, sleep=clock.sleep
    )
    assert not wait_for_path(
        "/x",
        present=True,
        timeout_s=0.0,
        exists=lambda _path: False,
        clock=clock,
        sleep=clock.sleep,
    )
    assert clock.now == 100.0


def _sudo_list(command):
    return ["sudo", "-n", "-l", *command[2:]]


def test_permitted_checks_all_four_commands_with_sudo_list():
    recorder = Recorder()

    assert recorder.cycler().permitted()
    assert recorder.commands == [_sudo_list(c) for c in (OFF_2, OFF_4, ON_2, ON_4)]
    assert recorder.events == [("run", c) for c in recorder.commands]


def test_permitted_is_false_when_sudo_refuses_one_command():
    recorder = Recorder({tuple(_sudo_list(ON_4)): (1, "sudo: a password is required")})

    assert not recorder.cycler().permitted()


@pytest.mark.parametrize(
    "error", [OSError("sudo: not found"), subprocess.TimeoutExpired(["sudo"], 10.0)]
)
def test_permitted_is_false_when_sudo_cannot_run(error):
    recorder = Recorder({tuple(_sudo_list(OFF_2)): error})

    assert not recorder.cycler().permitted()
    assert len(recorder.commands) == 1


def test_permitted_without_sudo_runs_nothing():
    recorder = Recorder()

    assert recorder.cycler(use_sudo=False).permitted()
    assert recorder.events == []


def test_wait_for_tty_waits_for_a_path_port():
    clock = FakeClock()
    checks = iter([False, True])

    assert wait_for_tty(
        "/dev/serial/by-id/ti",
        exists=lambda _path: next(checks),
        matches=lambda _pattern: pytest.fail("no glob for a path port"),
        clock=clock,
        sleep=clock.sleep,
    )
    assert clock.now == pytest.approx(100.1)


def test_wait_for_tty_does_not_wait_for_a_non_path_port():
    clock = FakeClock()

    assert wait_for_tty("COM7", exists=lambda _path: False, clock=clock, sleep=clock.sleep)
    assert clock.now == 100.0


def test_wait_for_tty_without_port_waits_for_any_ttyusb_then_settles():
    clock = FakeClock()
    globs = []
    answers = iter([[], [], ["/dev/ttyUSB0"]])

    def matches(pattern):
        globs.append(pattern)
        return next(answers)

    assert wait_for_tty(None, settle_s=1.0, matches=matches, clock=clock, sleep=clock.sleep)
    assert set(globs) == {AUTO_PORT_GLOB}
    assert clock.now == pytest.approx(101.2)


def test_wait_for_tty_without_port_times_out_without_settling():
    clock = FakeClock()

    assert not wait_for_tty(
        None, timeout_s=2.0, matches=lambda _pattern: [], clock=clock, sleep=clock.sleep
    )
    assert 102.0 <= clock.now < 102.2


def test_usb_mounted_block_devices_finds_only_usb_backed_mounts(tmp_path):
    devices = tmp_path / "devices" / "platform"
    usb_disk = devices / "usb2" / "2-1" / "host0" / "block" / "sda" / "sda1"
    sd_card = devices / "mmc1" / "mmcblk0" / "mmcblk0p2"
    for directory in (usb_disk, sd_card):
        directory.mkdir(parents=True)
    sys_block = tmp_path / "class" / "block"
    sys_block.mkdir(parents=True)
    (sys_block / "sda1").symlink_to(usb_disk)
    (sys_block / "mmcblk0p2").symlink_to(sd_card)
    mounts = tmp_path / "mounts"
    mounts.write_text(
        "/dev/mmcblk0p2 / ext4 rw 0 0\n"
        "proc /proc proc rw 0 0\n"
        "/dev/sda1 /media/usb vfat rw 0 0\n"
        "/dev/sda1 /media/again vfat rw 0 0\n"
        "/dev/sdz9 /media/gone vfat rw 0 0\n",
        encoding="utf-8",
    )

    found = usb_mounted_block_devices(mounts_path=str(mounts), sys_class_block=str(sys_block))

    assert found == ["/dev/sda1"]


def test_usb_mounted_block_devices_without_proc_mounts(tmp_path):
    assert usb_mounted_block_devices(mounts_path=str(tmp_path / "missing")) == []
