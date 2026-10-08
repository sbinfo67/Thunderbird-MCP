from __future__ import annotations

from types import SimpleNamespace

import pytest

from tbmcp.errors import TransportError


@pytest.fixture(autouse=True)
def _clean_refresh_env(monkeypatch):
    monkeypatch.delenv("TBMCP_PROFILE", raising=False)
    monkeypatch.delenv("TBMCP_THUNDERBIRD", raising=False)


def _setup(
    monkeypatch,
    tmp_path,
    *,
    installed="same",
    running=False,
    status=None,
    running_profile=True,
    shutdown_error=None,
):
    from tbmcp import addon_install, cli, ipc

    monkeypatch.setattr("tbmcp.profile.list_profiles", lambda: [])
    monkeypatch.setattr(
        addon_install,
        "running_command_lines",
        lambda: [f'thunderbird.exe -profile "{tmp_path}"'] if running_profile else [],
    )
    monkeypatch.setattr(addon_install, "is_running", lambda: running)
    monkeypatch.setattr(ipc, "newest_source_mtime", lambda: 20.0)
    install_calls = []
    monkeypatch.setattr(
        addon_install,
        "install_automatic",
        lambda p, **kw: (
            install_calls.append((p, kw))
            or SimpleNamespace(
                ok=installed != "failed", message="install result", xpi=tmp_path / "xpi"
            )
        ),
    )
    monkeypatch.setattr(
        "tbmcp.addon_build.build_check",
        lambda _path: {"installed": installed, "source": "same"},
    )

    instances = []

    class Bridge:
        def __init__(self, **kwargs):
            self.closed = False
            self.calls = []
            instances.append(self)

        async def status(self):
            if isinstance(status, Exception):
                raise status
            return status or {"daemon": {"uptimeSeconds": 0}}

        async def call(self, method, params=None):
            self.calls.append(method)
            if method == "daemon.shutdown" and shutdown_error:
                raise shutdown_error
            if method == "daemon.shutdown" and isinstance(status, TransportError):
                raise status
            return {}

        async def close(self):
            self.closed = True

    monkeypatch.setattr("tbmcp.bridge.Bridge", Bridge)
    return cli, instances, install_calls


def test_matching_addon_is_left_alone_and_current_daemon_is_not_stopped(
    monkeypatch, tmp_path, capsys
):
    cli, instances, calls = _setup(
        monkeypatch,
        tmp_path,
        status={"daemon": {"uptimeSeconds": 1}, "connected": True},
    )
    monkeypatch.setattr(cli.time, "time", lambda: 10.0)
    monkeypatch.setattr("tbmcp.ipc.newest_source_mtime", lambda: 1.0)
    assert cli.main(["refresh"]) == 0
    assert calls == []
    assert instances[0].closed
    assert instances[0].calls == []
    assert "daemon is current" in capsys.readouterr().out


@pytest.mark.parametrize("installed", ["old", None])
@pytest.mark.parametrize("running", [False, True])
def test_stale_or_missing_addon_install_preserves_running_state(
    monkeypatch, tmp_path, installed, running, capsys
):
    cli, _instances, calls = _setup(monkeypatch, tmp_path, installed=installed, running=running)
    assert cli.main(["refresh"]) == 0
    assert len(calls) == 1
    assert calls[0][1] == {"restart_after": running}
    assert calls[0][0].path == tmp_path
    assert "install result" in capsys.readouterr().out


def test_failed_install_returns_one(monkeypatch, tmp_path):
    cli, _instances, _calls = _setup(monkeypatch, tmp_path, installed="failed")
    assert cli.main(["refresh"]) == 1


def test_missing_profile_still_attempts_install(monkeypatch, tmp_path):
    cli, _instances, calls = _setup(monkeypatch, tmp_path, installed=None, running_profile=False)
    assert cli.main(["refresh"]) == 0
    assert calls == [(None, {"restart_after": False})]


def test_old_daemon_is_shutdown_and_dropped_connection_is_ignored(monkeypatch, tmp_path, capsys):
    cli, instances, _calls = _setup(
        monkeypatch,
        tmp_path,
        status={"daemon": {"uptimeSeconds": 5}, "connected": False},
    )
    monkeypatch.setattr(cli.time, "time", lambda: 100.0)
    monkeypatch.setattr("tbmcp.ipc.newest_source_mtime", lambda: 98.0)
    assert cli.main(["refresh"]) == 0
    assert instances[0].calls == ["daemon.shutdown"]
    assert "next call starts a fresh one" in capsys.readouterr().out


@pytest.mark.parametrize("code", ["DISCONNECTED", "NO_DAEMON"])
def test_shutdown_connection_drop_does_not_fail_refresh(monkeypatch, tmp_path, code):
    cli, instances, _calls = _setup(
        monkeypatch,
        tmp_path,
        status={"daemon": {"uptimeSeconds": 5}},
        shutdown_error=TransportError("connection dropped", code=code),
    )
    monkeypatch.setattr(cli.time, "time", lambda: 100.0)
    monkeypatch.setattr("tbmcp.ipc.newest_source_mtime", lambda: 98.0)
    assert cli.main(["refresh"]) == 0
    assert instances[0].calls == ["daemon.shutdown"]


def test_failed_shutdown_request_fails_refresh(monkeypatch, tmp_path, capsys):
    cli, instances, calls = _setup(
        monkeypatch,
        tmp_path,
        status={"daemon": {"uptimeSeconds": 5}},
        shutdown_error=TransportError("could not reach the daemon", code="WRITE_FAILED"),
    )
    monkeypatch.setattr(cli.time, "time", lambda: 100.0)
    monkeypatch.setattr("tbmcp.ipc.newest_source_mtime", lambda: 98.0)
    assert cli.main(["refresh"]) == 1
    assert instances[0].closed
    assert calls == []
    assert "next call starts a fresh one" not in capsys.readouterr().out


def test_missing_daemon_is_not_an_error(monkeypatch, tmp_path, capsys):
    cli, instances, _calls = _setup(
        monkeypatch, tmp_path, status=TransportError("no daemon", code="NO_DAEMON")
    )
    assert cli.main(["refresh"]) == 0
    assert instances[0].closed
    assert "no daemon running" in capsys.readouterr().out


def test_unreachable_daemon_is_not_reported_as_absent(monkeypatch, tmp_path, capsys):
    cli, instances, calls = _setup(
        monkeypatch,
        tmp_path,
        status=TransportError("the daemon rejected our control token", code="UNAUTHORIZED"),
    )
    assert cli.main(["refresh"]) == 1
    assert instances[0].closed
    assert calls == []
    assert "no daemon running" not in capsys.readouterr().out
