from __future__ import annotations

from tbmcp import addon_install


def test_stop_asks_every_window_and_waits(monkeypatch):
    monkeypatch.setattr(addon_install.sys, "platform", "win32")
    monkeypatch.setattr(addon_install, "open_windows", lambda: [(1, "Inbox"), (2, "Message")])
    asked = []
    monkeypatch.setattr(addon_install, "_ask_to_close", asked.append)
    states = iter([True, True, False])
    monkeypatch.setattr(addon_install, "is_running", lambda: next(states))
    monkeypatch.setattr(addon_install.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(addon_install.subprocess, "run", lambda *_a, **_kw: 1 / 0)
    assert addon_install._stop()
    assert asked == [1, 2]


def test_stop_times_out_without_force_killing(monkeypatch):
    monkeypatch.setattr(addon_install.sys, "platform", "win32")
    monkeypatch.setattr(addon_install, "open_windows", lambda: [(1, "Inbox")])
    asked = []
    monkeypatch.setattr(addon_install, "_ask_to_close", asked.append)
    monkeypatch.setattr(addon_install, "is_running", lambda: True)
    clock = iter([0, 61])
    monkeypatch.setattr(addon_install.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(addon_install.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(addon_install.subprocess, "run", lambda *_a, **_kw: 1 / 0)
    assert not addon_install._stop()
    assert asked == [1]


def test_stop_does_nothing_when_closed(monkeypatch):
    monkeypatch.setattr(addon_install.sys, "platform", "win32")
    monkeypatch.setattr(addon_install, "is_running", lambda: False)
    monkeypatch.setattr(addon_install, "open_windows", lambda: 1 / 0)
    monkeypatch.setattr(addon_install.subprocess, "run", lambda *_a, **_kw: 1 / 0)
    assert addon_install._stop()
