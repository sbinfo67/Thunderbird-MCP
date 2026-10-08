"""Compose image paths through the public MCP tools."""

import pytest
from mcp import Client

from tbmcp.config import Settings
from tbmcp.server import build_server

pytestmark = pytest.mark.anyio


def _server(bridge):
    return build_server(Settings().merged_with(toolsets=("compose",)), bridge=bridge)


def _text(result):
    return " ".join(getattr(block, "text", "") for block in result.content)


async def test_draft_inline_images_and_result(fake_bridge):
    bridge = fake_bridge({"compose.save": {"messageId": 42, "folderPath": "/Drafts"}})
    async with Client(_server(bridge)) as client:
        result = await client.call_tool(
            "mail_draft_save",
            {
                "subject": "photo",
                "body": '<img src="cid:pic">',
                "is_html": True,
                "inline_images": {"pic": "C:/photo.jpg"},
            },
        )
    assert not result.is_error, _text(result)
    assert bridge.params_for("compose.save")["inlineImages"] == [
        {"cid": "pic", "path": "C:/photo.jpg"}
    ]
    assert result.structured_content["draftId"] == 42
    assert result.structured_content["folderPath"] == "/Drafts"


@pytest.mark.parametrize(
    "change,hint",
    [
        ({"is_html": False}, "is_html"),
        ({"inline_images": {"bad name": "C:/photo.jpg"}}, "name"),
        ({"body": "<p>missing</p>"}, "cid:pic"),
    ],
)
async def test_invalid_inline_images_never_reach_bridge(fake_bridge, change, hint):
    bridge = fake_bridge()
    args = {
        "body": '<img src="cid:pic">',
        "is_html": True,
        "inline_images": {"pic": "C:/photo.jpg"},
    }
    args.update(change)
    async with Client(_server(bridge)) as client:
        result = await client.call_tool("mail_draft_save", args)
    assert result.is_error
    assert hint in _text(result)
    assert bridge.calls == []


async def test_missing_draft_location_suggests_search(fake_bridge):
    bridge = fake_bridge({"compose.save": {"messageId": None, "folderPath": None}})
    async with Client(_server(bridge)) as client:
        result = await client.call_tool("mail_draft_save", {"subject": "photo"})
    assert not result.is_error
    assert "mail_search" in result.structured_content["note"]


async def test_reply_carries_inline_images(fake_bridge):
    bridge = fake_bridge({"compose.reply": {"messageId": 42, "folderPath": "/Drafts"}})
    async with Client(_server(bridge)) as client:
        result = await client.call_tool(
            "mail_reply",
            {
                "message_id": 7,
                "body": '<img src="cid:pic">',
                "is_html": True,
                "inline_images": {"pic": "C:/photo.jpg"},
                "confirm": True,
            },
        )
    assert not result.is_error, _text(result)
    assert bridge.params_for("compose.reply")["inlineImages"] == [
        {"cid": "pic", "path": "C:/photo.jpg"}
    ]
