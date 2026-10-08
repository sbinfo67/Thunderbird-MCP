"""Write calls keep their reply and undo data in the private state folder."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from mcp import Client

from tbmcp import action_log, ipc
from tbmcp.config import Settings
from tbmcp.errors import ThunderbirdError
from tbmcp.safety import NO_CHANNEL, Consent
from tbmcp.server import build_server

pytestmark = pytest.mark.anyio


def _server(bridge):
    return build_server(Settings().merged_with(toolsets=("mail",)), bridge=bridge)


def _entries():
    return [json.loads(line) for line in action_log.path().read_text(encoding="utf-8").splitlines()]


def _text(result):
    return " ".join(getattr(block, "text", "") for block in result.content)


async def test_write_is_logged_once_and_read_is_not(fake_bridge):
    bridge = fake_bridge({"messages.delete": {"deleted": 1}, "tags.list": {"tags": []}})
    async with Client(_server(bridge)) as client:
        write = await client.call_tool("mail_delete", {"message_ids": [42], "confirm": True})
        read = await client.call_tool("mail_tags", {})
    assert not write.is_error and not read.is_error
    assert action_log.path().name == f"actions-{datetime.now(UTC):%Y-%m-%d}.jsonl"
    entries = _entries()
    assert len(entries) == 1
    assert entries[0] == {
        "time": entries[0]["time"],
        "tool": "mail_delete",
        "arguments": {
            "message_ids": [42],
            "confirm": True,
            "permanent": False,
            "dry_run_only": False,
        },
        "outcome": "done",
        "reply": write.structured_content,
    }


async def test_dry_run_and_refusal_are_logged_without_changing_the_error(fake_bridge):
    bridge = fake_bridge()
    async with Client(_server(bridge)) as client:
        dry = await client.call_tool("mail_delete", {"message_ids": [42], "dry_run_only": True})
        refused = await client.call_tool("mail_delete", {"message_ids": [42]})
    assert not dry.is_error and refused.is_error
    assert "confirm=true" in _text(refused)
    assert bridge.calls == []
    entries = _entries()
    assert [entry["outcome"] for entry in entries] == ["dry_run", "refused"]
    assert entries[0]["reply"] == dry.structured_content
    assert entries[1]["code"] == "NEEDS_CONFIRMATION"
    assert "confirm=true" in entries[1]["error"]


async def test_bridge_error_is_logged_and_still_reaches_client(fake_bridge):
    def fail(_params):
        raise ThunderbirdError("Thunderbird refused deletion", code="NO_PERMISSION")

    bridge = fake_bridge({"messages.delete": fail})
    async with Client(_server(bridge)) as client:
        result = await client.call_tool("mail_delete", {"message_ids": [42], "confirm": True})
    assert result.is_error and "Thunderbird refused deletion" in _text(result)
    entry = _entries()[0]
    assert entry["outcome"] == "failed"
    assert entry["code"] == "NO_PERMISSION"
    assert "Thunderbird refused deletion" in entry["error"]


async def test_clips_arguments_but_keeps_reply_and_consent_note(fake_bridge):
    fake_bridge()

    async def fake_tool(text: str, consent: Consent):
        return {"previous": text}

    long_text = "x" * 5_000
    result = await action_log.recorded(fake_tool)(
        text=long_text, consent=Consent(approve=True, note="because")
    )
    assert result["previous"] == long_text
    entry = _entries()[0]
    assert entry["arguments"]["text"].startswith("x" * 2_000)
    assert len(entry["arguments"]["text"]) < 2_100
    assert "5000" in entry["arguments"]["text"]
    assert "consent" not in entry["arguments"]
    assert entry["reply"]["previous"] == long_text
    assert entry["consentNote"] == "because"

    await action_log.recorded(fake_tool)(text="short", consent=Consent(note=NO_CHANNEL))
    assert "consentNote" not in _entries()[1]


async def test_log_failure_does_not_fail_the_tool(monkeypatch, tmp_path):
    monkeypatch.setattr(action_log, "path", lambda: tmp_path)

    async def fake_tool():
        return {"changed": True}

    assert await action_log.recorded(fake_tool)() == {"changed": True}


def test_first_write_of_a_day_removes_files_older_than_30_days():
    today = datetime.now(UTC)
    state = ipc.state_dir()
    old_daily = state / f"actions-{today - timedelta(days=31):%Y-%m-%d}.jsonl"
    kept_daily = state / f"actions-{today - timedelta(days=30):%Y-%m-%d}.jsonl"
    old_monthly = state / "actions-2020-01.jsonl"
    kept_monthly = state / f"actions-{today:%Y-%m}.jsonl"
    unrelated = state / "actions-2020-backup.jsonl"
    daemon_log = state / "daemon.log"
    for file in (old_daily, kept_daily, old_monthly, kept_monthly, unrelated, daemon_log):
        file.touch()

    action_log.append({"tool": "x"})

    assert not old_daily.exists()
    assert not old_monthly.exists()
    assert all(file.exists() for file in (kept_daily, kept_monthly, unrelated, daemon_log))
    assert _entries() == [{"tool": "x"}]


def test_cleanup_failure_does_not_lose_the_entry(monkeypatch):
    old_daily = ipc.state_dir() / f"actions-{datetime.now(UTC) - timedelta(days=31):%Y-%m-%d}.jsonl"
    old_daily.touch()

    def fail_unlink(_self, *, missing_ok=False):
        raise PermissionError("locked")

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    action_log.append({"tool": "x"})

    assert old_daily.exists()
    assert _entries() == [{"tool": "x"}]


async def test_cancellation_is_logged_and_still_propagates(fake_bridge):
    fake_bridge()

    async def cancelled_tool():
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await action_log.recorded(cancelled_tool)()

    entries = _entries()
    assert entries == [
        {
            "time": entries[0]["time"],
            "tool": "cancelled_tool",
            "arguments": {},
            "outcome": "failed",
            "error": "Tool call was cancelled",
        }
    ]
