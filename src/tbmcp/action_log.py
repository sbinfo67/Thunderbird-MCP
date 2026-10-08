"""Persistent record of write tool calls and their undo information."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from . import ipc
from .errors import BlockedError
from .safety import NO_CHANNEL, Consent

log = logging.getLogger("tbmcp.actions")
ARG_TEXT_LIMIT = 2_000
RETENTION_DAYS = 30


def _name(day: datetime) -> str:
    return f"actions-{day:%Y-%m-%d}.jsonl"


def path() -> Path:
    return ipc.state_dir() / _name(datetime.now(UTC))


def _prune() -> None:
    cutoff = _name(datetime.now(UTC) - timedelta(days=RETENTION_DAYS))
    for file in ipc.state_dir().glob("actions-*.jsonl"):
        # A monthly name sorts after its own month's daily names and before the next month.
        if re.fullmatch(r"actions-\d{4}-\d{2}(?:-\d{2})?\.jsonl", file.name) and file.name < cutoff:
            with contextlib.suppress(OSError):
                file.unlink(missing_ok=True)


def _clip(value: Any) -> Any:
    if isinstance(value, str) and len(value) > ARG_TEXT_LIMIT:
        return f"{value[:ARG_TEXT_LIMIT]}… [original length: {len(value)}]"
    if isinstance(value, dict):
        return {key: _clip(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clip(item) for item in value]
    return value


def _json_default(value: Any) -> Any:
    return value.model_dump() if isinstance(value, BaseModel) else str(value)


def append(entry: dict[str, Any]) -> None:
    try:
        target = path()
        created = not target.exists()
        line = json.dumps(entry, ensure_ascii=False, default=_json_default) + "\n"
        # ponytail: concurrent serve processes can interleave large lines; add a lock if observed.
        with target.open("a", encoding="utf-8") as stream:
            stream.write(line)
        if created:
            ipc._restrict_permissions(target)
            _prune()
    except Exception:
        log.warning("Could not append action log entry", exc_info=True)


def recorded(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    async def wrapper(**kwargs: Any) -> Any:
        consent = kwargs.get("consent")
        entry: dict[str, Any] = {
            "time": datetime.now(UTC).isoformat(),
            "tool": fn.__name__,
            "arguments": _clip({key: value for key, value in kwargs.items() if key != "consent"}),
        }
        if isinstance(consent, Consent) and consent.note and consent.note != NO_CHANNEL:
            entry["consentNote"] = consent.note
        try:
            reply = await fn(**kwargs)
        except (Exception, asyncio.CancelledError) as exc:
            entry["outcome"] = "refused" if isinstance(exc, BlockedError) else "failed"
            entry["error"] = str(exc) or "Tool call was cancelled"
            if getattr(exc, "code", None):
                entry["code"] = exc.code
            append(entry)
            raise
        entry["outcome"] = (
            "dry_run" if isinstance(reply, dict) and reply.get("dryRun") is True else "done"
        )
        entry["reply"] = reply
        append(entry)
        return reply

    return wrapper
