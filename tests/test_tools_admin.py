"""What the admin toolset says when Thunderbird is not attached.

`tb_status` and `tb_diagnostics` are the two tools a model reaches for the moment
anything else reports that it cannot reach Thunderbird, so their `hint` is the whole
diagnosis as far as the model is concerned. It used to be one fixed sentence —
"ask the user to start Thunderbird" — which was actively wrong on the morning the
add-on was dialling in twice a minute and failing the handshake every time.
"""

from __future__ import annotations

import shutil
import time

import pytest
from mcp import Client

from tbmcp import addon_install
from tbmcp.addon_build import addon_id, build_xpi
from tbmcp.bridge import ATTACH_WAIT_SECONDS
from tbmcp.config import Settings
from tbmcp.errors import NotConnectedError
from tbmcp.server import build_server
from tbmcp.tools.admin import NOT_CONNECTED_HINT, NOT_RUNNING_HINT

pytestmark = pytest.mark.anyio

FAILING_HANDSHAKE = {
    "attempts": 3,
    "lastAttemptAt": time.time() - 5,
    "lastOutcome": "no-upgrade",
    "lastCloseCode": None,
    "lastUserAgent": "Thunderbird/155.0",
    "recentFailures": 3,
    "recentOutcomes": {"no-upgrade": 3},
    "windowSeconds": 60.0,
    "recent": [],
}

QUIET_HANDSHAKE = {
    "attempts": 0,
    "lastAttemptAt": None,
    "lastOutcome": None,
    "lastCloseCode": None,
    "lastUserAgent": None,
    "recentFailures": 0,
    "recentOutcomes": {},
    "windowSeconds": 60.0,
    "recent": [],
}


def _server(bridge):
    return build_server(Settings().merged_with(toolsets=("admin",)), bridge=bridge)


def _daemon_status(handshake: dict | None) -> dict:
    return {
        "daemon": {"pid": 1234, "uptimeSeconds": 60.0, "clients": 1},
        "profile": {"path": "/profile", "name": "default"},
        "thunderbird": None,
        "connected": False,
        "handshake": handshake,
        "logFile": "/state/tbmcp/daemon.log",
    }


async def test_status_names_wrong_profile_before_handshake(fake_bridge, monkeypatch):
    monkeypatch.setattr(addon_install, "is_running", lambda: True)
    status = _daemon_status(FAILING_HANDSHAKE)
    status["profile"] = {"path": "/a", "source": "explicit", "thunderbirdProfiles": ["/b"]}
    bridge = fake_bridge({"daemon.status": status})
    async with Client(_server(bridge)) as client:
        payload = (await client.call_tool("tb_status", {})).structured_content
        diagnostics = (await client.call_tool("tb_diagnostics", {})).structured_content
    for result in (payload, diagnostics):
        assert "/a" in result["hint"] and "/b" in result["hint"]
        assert "TBMCP_PROFILE" in result["hint"]


async def test_tb_status_explains_a_failing_handshake_rather_than_blaming_the_user(
    fake_bridge,
    monkeypatch,
) -> None:
    monkeypatch.setattr(addon_install, "is_running", lambda: True)
    bridge = fake_bridge({"daemon.status": _daemon_status(FAILING_HANDSHAKE)})

    async with Client(_server(bridge)) as client:
        result = await client.call_tool("tb_status", {})

    payload = result.structured_content
    assert payload["handshake"]["recentFailures"] == 3
    assert "never completed the handshake" in payload["hint"]
    assert "Ask the user to start Thunderbird" not in payload["hint"]


async def test_tb_status_keeps_the_usual_hint_when_nothing_has_dialled_in(
    fake_bridge, monkeypatch
) -> None:
    """A running Thunderbird whose add-on has not attached gets the usual advice."""
    monkeypatch.setattr(addon_install, "is_running", lambda: True)
    bridge = fake_bridge({"daemon.status": _daemon_status(QUIET_HANDSHAKE)})

    async with Client(_server(bridge)) as client:
        result = await client.call_tool("tb_status", {})

    payload = result.structured_content
    assert "Ask the user to start Thunderbird" in payload["hint"]
    assert payload["handshake"] == QUIET_HANDSHAKE


async def test_tb_status_survives_a_daemon_too_old_to_report_handshakes(
    fake_bridge, monkeypatch
) -> None:
    monkeypatch.setattr(addon_install, "is_running", lambda: True)
    bridge = fake_bridge({"daemon.status": _daemon_status(None)})

    async with Client(_server(bridge)) as client:
        result = await client.call_tool("tb_status", {})

    payload = result.structured_content
    assert payload["handshake"] is None
    assert "Ask the user to start Thunderbird" in payload["hint"]


@pytest.mark.parametrize("installed", ["stale", "matching", "missing"])
async def test_tb_status_reports_stale_build(fake_bridge, tmp_path, installed) -> None:
    status = _daemon_status(None)
    status["connected"] = True
    status["thunderbird"] = {"addonVersion": "1.3.0", "experiment": True}
    status["profile"]["path"] = str(tmp_path)
    if installed != "missing":
        extensions = tmp_path / "extensions"
        extensions.mkdir()
        target = extensions / f"{addon_id()}.xpi"
        shutil.copyfile(build_xpi(tmp_path / "package"), target)
        if installed == "stale":
            import zipfile

            with zipfile.ZipFile(target, "a") as zf:
                zf.writestr("changed.js", "// stale")
    bridge = fake_bridge({"daemon.status": status})
    async with Client(_server(bridge)) as client:
        payload = (await client.call_tool("tb_status", {})).structured_content
    assert payload["addonBuild"]["stale"] is (installed == "stale")
    assert ("tbmcp install-addon" in payload.get("hint", "")) is (installed == "stale")


async def test_tb_status_without_profile_omits_build(fake_bridge, monkeypatch) -> None:
    monkeypatch.setattr(addon_install, "is_running", lambda: True)
    status = _daemon_status(None)
    status["profile"] = None
    bridge = fake_bridge({"daemon.status": status})
    async with Client(_server(bridge)) as client:
        payload = (await client.call_tool("tb_status", {})).structured_content
    assert "addonBuild" not in payload


async def test_tb_status_waits_for_fresh_daemon_attachment(fake_bridge, monkeypatch) -> None:
    monkeypatch.setattr(addon_install, "is_running", lambda: True)
    status = _daemon_status(None)
    status["daemon"]["uptimeSeconds"] = 0.2
    attached = {**status, "connected": True, "thunderbird": {"experiment": True}}
    bridge = fake_bridge({"daemon.status": status, "daemon.waitForThunderbird": attached})

    async with Client(_server(bridge)) as client:
        payload = (await client.call_tool("tb_status", {})).structured_content

    assert payload["connected"] is True
    assert payload["state"] == "connected"
    assert payload["thunderbirdRunning"] is True
    assert payload["waitedSeconds"] >= 0
    assert bridge.methods() == ["daemon.status", "daemon.waitForThunderbird"]
    assert 19 < bridge.params_for("daemon.waitForThunderbird")["timeout"] <= ATTACH_WAIT_SECONDS


async def test_tb_status_answers_immediately_when_thunderbird_is_closed(
    fake_bridge, monkeypatch
) -> None:
    monkeypatch.setattr(addon_install, "is_running", lambda: False)
    status = _daemon_status(None)
    status["daemon"]["uptimeSeconds"] = 0.2
    bridge = fake_bridge({"daemon.status": status})

    async with Client(_server(bridge)) as client:
        payload = (await client.call_tool("tb_status", {})).structured_content

    assert bridge.methods() == ["daemon.status"]
    assert payload["state"] == "not-running"
    assert payload["thunderbirdRunning"] is False
    assert payload["hint"] == NOT_RUNNING_HINT
    assert payload["waitedSeconds"] == 0


async def test_tb_status_does_not_wait_for_old_daemon(fake_bridge, monkeypatch) -> None:
    monkeypatch.setattr(addon_install, "is_running", lambda: True)
    bridge = fake_bridge({"daemon.status": _daemon_status(None)})

    async with Client(_server(bridge)) as client:
        payload = (await client.call_tool("tb_status", {})).structured_content

    assert bridge.methods() == ["daemon.status"]
    assert payload["state"] == "not-attached"
    assert payload["hint"].startswith(NOT_CONNECTED_HINT)
    assert "/profile" in payload["hint"]
    assert payload["waitedSeconds"] == 0


async def test_tb_status_flags_server_started_before_source_changed(fake_bridge, monkeypatch):
    from tbmcp import ipc
    from tbmcp.tools import admin

    monkeypatch.setattr(admin, "SERVER_STARTED_AT", 10.0)
    monkeypatch.setattr(ipc, "newest_source_mtime", lambda: 20.0)
    status = _daemon_status(None)
    status["connected"] = True
    bridge = fake_bridge({"daemon.status": status})
    async with Client(_server(bridge)) as client:
        payload = (await client.call_tool("tb_status", {})).structured_content
    assert payload["serverCodeStale"] is True
    assert "reconnect the server" in payload["hint"]


async def test_tb_status_does_not_flag_current_server(fake_bridge, monkeypatch):
    from tbmcp import ipc
    from tbmcp.tools import admin

    monkeypatch.setattr(admin, "SERVER_STARTED_AT", 20.0)
    monkeypatch.setattr(ipc, "newest_source_mtime", lambda: 10.0)
    bridge = fake_bridge({"daemon.status": _daemon_status(None)})
    async with Client(_server(bridge)) as client:
        payload = (await client.call_tool("tb_status", {})).structured_content
    assert "serverCodeStale" not in payload


async def test_tb_status_preserves_existing_hint_when_server_is_stale(fake_bridge, monkeypatch):
    from tbmcp import ipc
    from tbmcp.tools import admin

    monkeypatch.setattr(admin, "SERVER_STARTED_AT", 10.0)
    monkeypatch.setattr(ipc, "newest_source_mtime", lambda: 20.0)
    monkeypatch.setattr(addon_install, "is_running", lambda: False)
    bridge = fake_bridge({"daemon.status": _daemon_status(None)})
    async with Client(_server(bridge)) as client:
        payload = (await client.call_tool("tb_status", {})).structured_content
    assert payload["serverCodeStale"] is True
    assert payload["hint"] == NOT_RUNNING_HINT


async def test_tb_status_rereads_handshake_after_grace_wait_expires(
    fake_bridge, monkeypatch
) -> None:
    monkeypatch.setattr(addon_install, "is_running", lambda: True)
    status = _daemon_status(None)
    status["daemon"]["uptimeSeconds"] = 0.2
    reads = 0

    def read_status(_params):
        nonlocal reads
        reads += 1
        return status if reads == 1 else _daemon_status(FAILING_HANDSHAKE)

    def fail_wait(_params):
        raise NotConnectedError()

    bridge = fake_bridge({"daemon.status": read_status, "daemon.waitForThunderbird": fail_wait})
    async with Client(_server(bridge)) as client:
        payload = (await client.call_tool("tb_status", {})).structured_content

    assert bridge.methods() == ["daemon.status", "daemon.waitForThunderbird", "daemon.status"]
    assert payload["state"] == "not-attached"
    assert "never completed the handshake" in payload["hint"]


async def test_tb_status_skips_process_check_when_connected(fake_bridge, monkeypatch) -> None:
    def unexpected_process_check():
        raise AssertionError("connected status must not inspect processes")

    monkeypatch.setattr(addon_install, "is_running", unexpected_process_check)
    status = _daemon_status(None)
    status["connected"] = True
    bridge = fake_bridge({"daemon.status": status})

    async with Client(_server(bridge)) as client:
        payload = (await client.call_tool("tb_status", {})).structured_content

    assert bridge.methods() == ["daemon.status"]
    assert payload["state"] == "connected"
    assert payload["thunderbirdRunning"] is True
    assert payload["waitedSeconds"] == 0


async def test_tb_diagnostics_carries_the_handshake_record_and_the_diagnosis(
    fake_bridge,
) -> None:
    bridge = fake_bridge({"daemon.status": _daemon_status(FAILING_HANDSHAKE)})

    async with Client(_server(bridge)) as client:
        result = await client.call_tool("tb_diagnostics", {})

    payload = result.structured_content
    assert payload["connected"] is False
    assert payload["handshake"]["lastOutcome"] == "no-upgrade"
    assert "never completed the handshake" in payload["hint"]
    # The privileged half was never asked: there is nothing to ask it through.
    assert bridge.methods() == ["daemon.status"]
