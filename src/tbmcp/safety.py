"""Consent, annotations, and the shape every mutating tool follows.

Four independent layers, because no single one is present on every client:

1. Honest `ToolAnnotations` — advisory, but hosts use them to decide when to ask.
2. `_meta["anthropic/requiresUserInteraction"]` — Claude Code prompts on every call,
   even under `bypassPermissions`. Codex ignores it.
3. An explicit `confirm: bool = False` tool parameter — works on every client,
   including ones with no back-channel at all.
4. Elicitation through `Annotated[Consent, Resolve(...)]` — a real prompt where the
   client supports it, and it never appears in the tool's input schema, so the model
   cannot fabricate the approval.

The confirmation resolver degrades in that order: `--yolo` short-circuits, an
explicit `confirm=True` satisfies it, an elicitation-capable client is asked, and
anything else gets a `BlockedError` that tells the model exactly what to pass.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any

from mcp.server.mcpserver import Context, Elicit, Resolve
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from .config import Settings
from .errors import BlockedError, UsageError

# Set once by server.build_server(); resolvers are module-level functions, so they
# need a way to see the active policy.
_settings = Settings()


def set_settings(settings: Settings) -> None:
    global _settings
    _settings = settings


def current_settings() -> Settings:
    return _settings


class Consent(BaseModel):
    """The answer to a confirmation prompt."""

    approve: bool = Field(
        default=False,
        description="Approve this action. Answer no to abort without any change.",
    )
    note: str | None = Field(default=None, description="Optional note recorded in the action log.")


#: Marks "there was no way to ask" as distinct from "the user said no". The
#: difference matters: the first is worth telling the model how to fix, the second
#: must never be presented as something to work around.
NO_CHANNEL = "__tbmcp_no_consent_channel__"


def _elicitation_available(ctx: Context | None) -> bool:
    if ctx is None:
        return False
    try:
        capabilities = ctx.client_capabilities
    except Exception:
        return False
    return bool(capabilities and getattr(capabilities, "elicitation", None))


@dataclass(frozen=True)
class FolderPolicy:
    """A folder rule action that can grant a gated folder tool without a prompt."""

    action: str
    """Rule action to look for, e.g. `"rename_subfolders"`."""
    on_parent: bool = False
    """The tool names its folder by `parent_id` rather than `folder_id`."""
    strictly_below: bool = False
    """The rule's own root folder is never granted, only folders beneath it."""
    need_empty: bool = False
    """The folder must hold no messages and no subfolders."""


def consent_for(
    action: str,
    *,
    folder_policy: FolderPolicy | None = None,
    move_policy: bool = False,
) -> Callable[..., Any]:
    """Build a resolver that gates a tool behind approval.

    `action` is a short imperative phrase used in the prompt, e.g.
    `"send this message"`. The generated resolver reads the tool's own `confirm`
    argument by name, so every gated tool must declare `confirm: bool = False`.

    The resolver never raises. Resolvers run *before* the tool body, so raising here
    would also block a `dry_run_only` preview — which is exactly when a caller most
    wants to look before committing. Instead it reports "no channel", and `require()`
    turns that into an error at the point the tool is about to actually do something.

    `folder_policy` approves without asking when a configured folder rule grants
    that action on the live folder. `move_policy` does the same for a message move:
    into a `move_in` folder, or every message out of `move_out` folders.
    """

    async def decide(
        confirm: bool,
        ctx: Context | None,
        folder_id: str | None = None,
        message_ids: list[int] | None = None,
        destination_folder_id: str | None = None,
    ):  # type: ignore[no-untyped-def]
        if _settings.yolo or confirm:
            return Consent(approve=True)
        if folder_policy is not None:
            from .tools._common import policy_allows

            if await policy_allows(
                folder_policy.action,
                folder_id,
                strictly_below=folder_policy.strictly_below,
                need_empty=folder_policy.need_empty,
            ):
                return Consent(approve=True)
        if move_policy:
            from .tools._common import policy_allows_mail_move, require_ids

            try:
                ids = require_ids(message_ids)
            except (TypeError, ValueError, UsageError):
                ids = []
            if (
                ids
                and destination_folder_id
                and await policy_allows_mail_move(ids, destination_folder_id)
            ):
                return Consent(approve=True)
        if _elicitation_available(ctx):
            return Elicit(f"Allow thunderbird-mcp to {action}?", Consent)
        return Consent(approve=False, note=NO_CHANNEL)

    if move_policy:

        async def resolver_move(
            message_ids: list[int],
            destination_folder_id: str,
            confirm: bool = False,
            ctx: Context | None = None,
        ):  # type: ignore[no-untyped-def]
            return await decide(
                confirm, ctx, message_ids=message_ids, destination_folder_id=destination_folder_id
            )

        return resolver_move

    if folder_policy is not None:
        if folder_policy.on_parent:

            async def resolver_parent(
                parent_id: str | None = None, confirm: bool = False, ctx: Context | None = None
            ):  # type: ignore[no-untyped-def]
                return await decide(confirm, ctx, parent_id)

            return resolver_parent

        async def resolver_folder(
            folder_id: str, confirm: bool = False, ctx: Context | None = None
        ):  # type: ignore[no-untyped-def]
            return await decide(confirm, ctx, folder_id)

        return resolver_folder

    async def resolver(confirm: bool = False, ctx: Context | None = None):  # type: ignore[no-untyped-def]
        return await decide(confirm, ctx)

    return resolver


def require(consent: Consent | None, action: str) -> None:
    """Stop unless the resolved consent actually approved the action.

    Two failure modes, deliberately worded differently: a missing consent channel is
    something the caller can fix by passing `confirm=true`, whereas a user who said
    no must not be handed a workaround.
    """
    if consent is not None and consent.approve:
        return
    if consent is None or consent.note == NO_CHANNEL:
        raise BlockedError(
            f"Refusing to {action} without confirmation.",
            needs="confirm=true",
            code="NEEDS_CONFIRMATION",
            hint="Ask the user first, then re-issue the same call with confirm=true.",
        )
    raise BlockedError(
        f"The user declined to {action}.", code="DECLINED", needs="a new instruction from the user"
    )


def Gate(
    action: str,
    *,
    folder_policy: FolderPolicy | None = None,
    move_policy: bool = False,
) -> Any:
    """`consent: Gate("delete these messages")` in a tool signature."""
    return Annotated[
        Consent, Resolve(consent_for(action, folder_policy=folder_policy, move_policy=move_policy))
    ]


def guard_write(what: str) -> None:
    """Refuse a mutating operation when the server was started read-only."""
    if _settings.read_only:
        raise BlockedError(
            f"This server is running read-only, so it will not {what}.",
            code="READ_ONLY",
            needs="restart without --read-only",
        )


# --------------------------------------------------------------------- annotations

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
"""Reads nothing but Thunderbird's own state; safe to call speculatively."""

MUTATING = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
)
"""Changes state, but nothing is lost if it happens twice."""

IDEMPOTENT_WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
"""Setting a value: repeating the call lands in the same place."""

DESTRUCTIVE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False
)
"""Deletes or permanently rewrites something the user may not be able to recover."""

OUTBOUND = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True
)
"""Leaves the machine — sending mail cannot be undone."""

#: Force a host prompt where the host honours it.
NEEDS_INTERACTION: dict[str, Any] = {"anthropic/requiresUserInteraction": True}


def large_output(chars: int = 400_000) -> dict[str, Any]:
    """Raise a host's per-result cap for tools that legitimately return a lot."""
    return {"anthropic/maxResultSizeChars": min(chars, 500_000)}
