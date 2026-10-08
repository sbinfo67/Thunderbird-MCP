# Tool catalogue

The contract between the three layers. Every row is `python tool` → `bridge method`
→ what implements it. `x.*` methods need the privileged half of the add-on.

Naming: `<domain>_<verb>`. Domain first, because that is what makes tool search find
the right one and what maps to `--toolsets`. Read tools are `list`/`get`/`search`;
mutating tools are `set`/`create`/`update`/`delete`/`send`/`run`.

Toolset membership drives `--toolsets`. **Default set**: `mail`, `folders`,
`compose`, `search`, `admin`. Everything else is opt-in.

As built: **112 tools** over 10 toolsets, backed by **128 bridge methods** (58 on the
official API, 70 privileged). `--read-only` leaves 48. Counts per toolset — mail 15,
folders 15, compose 6, search 7, contacts 13, calendar 12, filters 8, accounts 18,
settings 11, admin 7. `tbmcp tools --toolsets all` prints the live list.

## mail — default

| Tool | Bridge method | Notes |
| --- | --- | --- |
| `mail_search` | `messages.query` | `full_text` uses the global index; paginated |
| `mail_list` | `messages.list` | one folder or every folder of a type, sorted |
| `mail_get` | `messages.read` | `detail=summary\|text\|full` |
| `mail_get_many` | `messages.readMany` | ≤50 ids, progress-reporting |
| `mail_get_source` | `messages.raw` | needs offline copy on IMAP |
| `mail_attachments` | `messages.listAttachments` | |
| `mail_save_attachment` | `messages.saveAttachment` → `x.files.write` | writes to disk; gated, interactive |
| `mail_mark` | `messages.mark` | read/flagged/junk/tags; no confirmation; reports earlier state per message |
| `mail_move` | `messages.move` | gated; folder config rules can skip confirmation; reports each source and observed landing |
| `mail_copy` | `messages.copy` | gated; reports each source and observed copy location |
| `mail_archive` | `messages.archive` | gated; reports each source and observed archive location |
| `mail_delete` | `messages.delete` | gated, `DESTRUCTIVE`, `permanent` flag |
| `mail_tags` | `tags.list` | |
| `mail_tag_upsert` | `tags.upsert` | |
| `mail_tag_delete` | `tags.delete` | gated, destructive |

## folders — default

| Tool | Bridge method |
| --- | --- |
| `folder_list` | `folders.query` — tree or flat, with counts |
| `folder_get` | `folders.get` — one folder plus `MailFolderInfo` |
| `folder_capabilities` | `folders.capabilities` — can it hold messages, be renamed… |
| `folder_create` | `folders.create` (gated; folder config rules can skip confirmation) |
| `folder_rename` | `folders.rename` (gated; folder config rules can skip confirmation) |
| `folder_move` | `folders.move` (gated) |
| `folder_copy` | `folders.copy` (gated) |
| `folder_delete` | `folders.delete` (gated, destructive; folder config rules can skip confirmation for empty subfolders) |
| `folder_mark_read` | `folders.markAsRead` (gated) |
| `folder_set_favorite` | `folders.update` |
| `folder_empty_trash` | `folders.emptyTrash` (gated, destructive) |
| `folder_empty_junk` | `folders.emptyJunk` (gated, destructive) |
| `folder_get_unified` | `folders.getUnified` — Inbox/Drafts/Sent/… across accounts |
| `folder_sync_offline` | `x.admin.downloadForOffline` (gated) |
| `folder_compact` | `x.admin.compactFolders` (gated) |

## compose — default

| Tool | Bridge method | Notes |
| --- | --- | --- |
| `mail_send` | `compose.send` | **draft by default**; `mode=draft\|send\|later`; gated, `OUTBOUND` |
| `mail_reply` | `compose.reply` | `reply_all`, quoting honoured |
| `mail_forward` | `compose.forward` | inline or attached |
| `mail_draft_save` | `compose.save` | draft or template |
| `mail_compose_open` | `compose.open` | opens a window for the user to finish |
| `mail_send_status` | `compose.status` | outbox / send-later queue |

The five writing tools accept `inline_images`: a map from reference name to an
absolute image path. In an HTML body, put `<img src="cid:logo">` where the picture
belongs and pass `inline_images={"logo": "C:\\images\\logo.png"}` with
`is_html=true`. Each name must be referenced. Only image files are accepted, up to
25 MB each; reading paths needs the privileged half of the add-on. Thunderbird
assigns the final `Content-ID`, which may differ from the reference name. A reply
with inline images opens in HTML mode. Inline images may not survive editing in
Thunderbird for Android.

New messages without inline images use `messages.sendMessage` or
`messages.saveMessage` headlessly when available. Replies, forwards, image bodies,
and builds without the headless capability open a compose window briefly. The
window lets Thunderbird store inline images as MIME parts for both drafts and sent
mail. A raw `<img src="data:…">` body also uses the window route.

`messages.sendMessage` needs the `messages.send` permission, which Thunderbird
declares `OptionalOnlyPermission` — unrequestable from the manifest, grantable only
through a user gesture the bridge does not have. The privileged half therefore grants
it to itself at startup; the add-on's startup report shows the outcome as
`features.headlessSend`. Rationale and measurements in
[`VERIFIED-FINDINGS.md`](VERIFIED-FINDINGS.md).

## search — default

| Tool | Bridge method | Notes |
| --- | --- | --- |
| `search_global` | `x.gloda.search` | Gloda ranked search, with contact/conversation facets |
| `search_conversation` | `x.gloda.conversation` | the whole thread for a message |
| `search_index_status` | `x.gloda.stats` | whether the global index is on and how far along — the answer to "why did search find nothing" |
| `search_saved_list` | `x.vfolders.list` | saved searches (virtual folders) |
| `search_saved_create` | `x.vfolders.create` (gated) | |
| `search_saved_update` | `x.vfolders.update` (gated) | |
| `search_saved_delete` | `x.vfolders.delete` (gated, destructive) | |

## contacts — opt-in

`contact_list`, `contact_search`, `contact_get`, `contact_create`, `contact_update`,
`contact_delete`, `addressbook_list`, `addressbook_create`, `addressbook_delete`,
`mailinglist_list`, `mailinglist_create`, `mailinglist_add_member`,
`mailinglist_remove_member` → `contacts.*` / `addressbooks.*` (official API, vCard in
and out).

## calendar — opt-in

| Tool | Bridge method |
| --- | --- |
| `calendar_list` | `x.calendar.listCalendars` |
| `calendar_create` / `calendar_update` / `calendar_delete` | `x.calendar.*Calendar` (gated) |
| `event_list` | `x.calendar.listItems` (`kind=event`) |
| `event_get` | `x.calendar.getItem` |
| `event_create` / `event_update` | `x.calendar.createEvent` / `updateEvent` (gated) |
| `event_delete` | `x.calendar.deleteItem` (gated, destructive) |
| `task_list` / `task_create` / `task_update` | `x.calendar.*Task` |

Recurring items: operations address the **series** unless `occurrence_date` is given.
Items are built with `CalEvent` / `CalTodo` (`cal.createEvent` no longer exists).

## filters — opt-in

`filter_list`, `filter_get`, `filter_create`, `filter_update`, `filter_delete`,
`filter_reorder`, `filter_set_enabled`, `filter_run` → `x.filters.*`
(`nsIMsgFilterList`). `filter_run` applies filters to a folder on demand.

## accounts — opt-in

| Tool | Bridge method |
| --- | --- |
| `account_list` | `accounts.list` + `x.accounts.list` (merged) |
| `account_get_server` | `x.accounts.serverSettings` |
| `account_set_server` | `x.accounts.setServerSetting` (gated, interactive) |
| `account_get_junk` / `account_set_junk` | `x.accounts.junkSettings` |
| `account_get_folders` / `account_set_folders` | `x.accounts.copiesAndFolders` |
| `account_get_sync` / `account_set_sync` | `x.accounts.syncSettings` |
| `identity_list` / `identity_get` | `x.identities.get` |
| `identity_set` | `x.identities.set` (gated) |
| `identity_set_signature` | `x.identities.setSignature` (gated) |
| `smtp_list` | `x.smtp.list` |
| `smtp_create` / `smtp_update` / `smtp_delete` / `smtp_set_default` | `x.smtp.*` (gated) |

Passwords are never read or written. Server *hostnames* read back `null` from
`incomingServer.hostName` on 153 — read `mail.server.<key>.hostname` instead.

## settings — opt-in

| Tool | Bridge method | Notes |
| --- | --- | --- |
| `pref_get` | `x.prefs.get` | value + type + whether user-set + locked |
| `pref_get_many` | `x.prefs.getMany` | |
| `pref_list` | `x.prefs.list` | by prefix, `only_user_set` |
| `pref_user_set` | `x.prefs.userSet` | everything changed from default; credentials redacted |
| `pref_set` | `x.prefs.set` | flat `type: "bool"\|"int"\|"string"` discriminator — **never** a root-level union; gated, interactive |
| `pref_reset` | `x.prefs.reset` | back to default; gated |
| `settings_describe` | — | curated map of common settings to pref names, so the model does not guess |
| `junk_get` / `junk_set` | `x.junk.*` | global bayes settings |
| `junk_train` | `x.junk.train` | mark ham/spam and retrain |
| `openpgp_list_keys` | `x.openpgp.listKeys` | read-only |

Two lists guard writes: `config.PREF_ALLOWLIST` (reviewed branches, the default) and
`config.PREF_DENYLIST` (credentials, `network.proxy.*`, `security.*`, the add-on trust
model — refused even with `--unsafe-prefs`). The privileged module re-checks the
denylist, because that is the only layer with real privilege.

## admin — default

| Tool | Bridge method | Notes |
| --- | --- | --- |
| `tb_status` | `daemon.status` | is Thunderbird attached, which halves loaded |
| `tb_wait` | `daemon.waitForThunderbird` | block until it connects |
| `tb_events` | `daemon.events` | drain the event buffer (new mail, folder changes) |
| `tb_diagnostics` | `x.admin.diagnostics` | versions, profile, module availability |
| `tb_console` | `x.admin.consoleMessages` | recent error-console lines, for debugging |
| `tb_addons` | `x.admin.addons` | installed add-ons |
| `tb_restart` | `x.admin.restart` | gated, interactive |

`tb_status`, `tb_wait` and `tb_events` are daemon-local. `tb_status` distinguishes
`not-running` from `not-attached`: when Thunderbird is running and the daemon just
started, it waits briefly for the add-on to attach. Otherwise it answers immediately,
including when Thunderbird is closed. `tb_wait` explicitly blocks until attachment.

## Conventions every tool follows

- Return a dict. `_common.page()` for lists, `_common.changed()` for writes,
  `_common.dry_run()` when `dry_run_only=True`.
- Every write tool call is appended to the action log in the state directory.
- Mutating tools: `confirm: bool = False`, `consent: Gate("…") = None`,
  `guard_write("…")` first, `require(consent, "…")` before the write.
- `Gate(...)` params never appear in the input schema, so the model cannot forge
  approval; `confirm=true` is the cross-client escape hatch for hosts without
  elicitation (Codex).
- Raise `UsageError` for anything the caller could have got right.
- Toolset modules must not use `from __future__ import annotations`.
