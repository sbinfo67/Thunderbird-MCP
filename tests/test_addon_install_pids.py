from __future__ import annotations

import os
import subprocess

from tbmcp import addon_install

# Taken at import, before conftest's autouse fixture stubs it out for every test.
REAL_COMMAND_LINES = addon_install.running_command_lines


def _fake_run(outputs: dict[str, str], seen: list[list[str]]):
    def run(argv, **_kwargs):
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout=outputs.get(argv[0], ""))

    return run


def test_pids_match_the_process_name_not_the_command_line(monkeypatch):
    """`pgrep -f thunderbird` also matched …/thunderbird-mcp/venv/bin/python, so
    install-addon sent SIGTERM to itself and left Thunderbird closed."""
    monkeypatch.setattr(addon_install.sys, "platform", "linux")
    seen: list[list[str]] = []
    monkeypatch.setattr(addon_install.subprocess, "run", _fake_run({"pgrep": "4242\n"}, seen))
    assert addon_install.running_pids() == [4242]
    assert seen == [["pgrep", "-x", "thunderbird|thunderbird-bin"]]


def test_pids_never_include_this_process(monkeypatch):
    monkeypatch.setattr(addon_install.sys, "platform", "linux")
    own = os.getpid()
    monkeypatch.setattr(addon_install.subprocess, "run", _fake_run({"pgrep": f"{own}\n4242\n"}, []))
    assert addon_install.running_pids() == [4242]


def test_command_lines_come_only_from_thunderbird_processes(monkeypatch):
    """`ps -A` listed `tbmcp serve --profile …` too, and its flag was read as
    Thunderbird's own -profile."""
    monkeypatch.setattr(addon_install.sys, "platform", "linux")
    seen: list[list[str]] = []
    outputs = {"pgrep": "4242\n4243\n", "ps": "/usr/bin/thunderbird -profile /p\n"}
    monkeypatch.setattr(addon_install.subprocess, "run", _fake_run(outputs, seen))
    assert REAL_COMMAND_LINES() == ["/usr/bin/thunderbird -profile /p"]
    assert seen[-1] == ["ps", "-o", "args=", "-p", "4242,4243"]


def test_no_command_lines_when_thunderbird_is_closed(monkeypatch):
    monkeypatch.setattr(addon_install.sys, "platform", "linux")
    seen: list[list[str]] = []
    monkeypatch.setattr(addon_install.subprocess, "run", _fake_run({"pgrep": ""}, seen))
    assert REAL_COMMAND_LINES() == []
    assert [argv[0] for argv in seen] == ["pgrep"]
