"""Folder rule consent through the MCP client."""

import pytest
from mcp import Client

from tbmcp.config import Settings
from tbmcp.policy import ACTIONS, FolderRule
from tbmcp.server import build_server

pytestmark = pytest.mark.anyio
RULES = (FolderRule("/@BKToDo", frozenset(ACTIONS)),)


def server(bridge, rules=RULES):
    return build_server(Settings(toolsets=("folders",), folder_rules=rules), bridge=bridge)


def response(path, *, count=0, children=None):
    folder = {"path": path, "accountId": "account1", "totalMessageCount": count}
    if children is not None:
        folder["children"] = children
    return {"folder": folder}


def error_text(result):
    return " ".join(getattr(block, "text", "") for block in result.content)


@pytest.mark.parametrize(
    ("tool", "args", "path", "mutation", "allowed"),
    [
        ("folder_create", {"name": "x", "parent_id": "id"}, "/@BKToDo", "folders.create", True),
        ("folder_create", {"name": "x", "parent_id": "id"}, "/Inbox", "folders.create", False),
        (
            "folder_rename",
            {"folder_id": "id", "new_name": "y"},
            "/@BKToDo/x",
            "folders.rename",
            True,
        ),
        (
            "folder_rename",
            {"folder_id": "id", "new_name": "y"},
            "/@BKToDo",
            "folders.rename",
            False,
        ),
        ("folder_delete", {"folder_id": "id"}, "/@BKToDo/x", "folders.delete", True),
    ],
)
async def test_folder_rules(fake_bridge, tool, args, path, mutation, allowed):
    bridge = fake_bridge({"folders.get": response(path), "messages.list": {"messages": []}})
    async with Client(server(bridge)) as client:
        result = await client.call_tool(tool, args)
    assert (not result.is_error) is allowed, error_text(result)
    assert (mutation in bridge.methods()) is allowed
    if not allowed:
        assert "NEEDS_CONFIRMATION" in error_text(result)


@pytest.mark.parametrize(
    ("folder", "listing"),
    [
        (response("/@BKToDo/x", count=3), {"messages": []}),
        (response("/@BKToDo/x", children=[{"path": "/@BKToDo/x/y"}]), {"messages": []}),
        ({"folder": {"path": "/@BKToDo/x", "accountId": "account1"}}, {"messages": []}),
        (response("/@BKToDo/x"), {"messages": [{"id": 42}]}),
        (response("/@BKToDo/x"), {}),
    ],
)
async def test_delete_requires_verified_empty_folder(fake_bridge, folder, listing):
    bridge = fake_bridge({"folders.get": folder, "messages.list": listing})
    async with Client(server(bridge)) as client:
        result = await client.call_tool("folder_delete", {"folder_id": "id"})
    assert result.is_error
    assert "folders.delete" not in bridge.methods()


async def test_failed_folder_lookup_falls_back_to_confirmation(fake_bridge):
    def fail(_params):
        raise RuntimeError("Thunderbird disconnected")

    bridge = fake_bridge({"folders.get": fail})
    async with Client(server(bridge)) as client:
        result = await client.call_tool("folder_delete", {"folder_id": "id"})
    assert result.is_error
    assert "NEEDS_CONFIRMATION" in error_text(result)
    assert "folders.delete" not in bridge.methods()


async def test_no_rules_preserve_host_prompt_and_skip_policy_reads(fake_bridge):
    bridge = fake_bridge()
    async with Client(server(bridge, ())) as client:
        tools = await client.list_tools()
        result = await client.call_tool("folder_create", {"name": "x", "parent_id": "id"})
    create = next(tool for tool in tools.tools if tool.name == "folder_create")
    assert create.meta["anthropic/requiresUserInteraction"]
    assert result.is_error
    assert bridge.calls == []


async def test_rules_remove_host_prompt(fake_bridge):
    bridge = fake_bridge()
    async with Client(server(bridge)) as client:
        tools = await client.list_tools()
    create = next(tool for tool in tools.tools if tool.name == "folder_create")
    assert not create.meta or "anthropic/requiresUserInteraction" not in create.meta
