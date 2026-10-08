# Folder rules

Folder rules let selected mail moves and folder changes run without confirmation. With no rules, the existing confirmation behavior applies.

Copy `settings.example.json` to `settings.json` in the repository root. Git ignores your copy, so your personal rules stay local. The server loads `settings.json` by default. `TBMCP_CONFIG` can point to another file, and `tbmcp serve --config PATH` takes precedence over both. A wheel install has no repository settings file; set `TBMCP_CONFIG` or use `--config` there. A missing file means no rules. Invalid JSON, a missing path, or an unknown action stops startup with an error.

Use `folder_list` to find the folder's `path` and, if you want to restrict the rule to one account, its `accountId`. Add an optional `"account": "account1"` key with that account ID. For example:

```json
{
  "folders": [
    {
      "path": "/@BKToDo",
      "allow": ["create_subfolders", "rename_subfolders", "delete_subfolders", "move_in", "move_out"]
    }
  ]
}
```

The rule covers the named folder and its descendants, case sensitively and by whole path segment. `"/@BKToDo"` does not cover `"/@BKToDoOld"`. Add another object to the `folders` array for each additional folder. Restart the MCP server after editing the file.

| Action | What can run without confirmation |
| --- | --- |
| `create_subfolders` | Create a folder inside the named folder or any descendant. |
| `rename_subfolders` | Rename a folder strictly below the named folder. The named folder itself still needs confirmation. |
| `delete_subfolders` | Delete a folder strictly below the named folder only when it has no messages or subfolders. The server checks the count and lists messages before skipping confirmation. |
| `move_in` | Move messages into the named folder or any descendant. |
| `move_out` | Move messages when every source folder is the named folder or a descendant. The server verifies every message's source folder first. |

A rule applies only to `folder_create`, `folder_rename`, `folder_delete`, and `mail_move`. Other tools retain their normal confirmation behavior. A nonempty folder still requires confirmation before deletion.

**Client approval trade-off:** An MCP client's approval flag is set for a whole tool, before it knows which folder a call targets. If any rule grants one of these actions, the server removes that tool's client approval flag for *all* folders. For calls outside the rule, the server still requires `confirm=true` or an approval prompt from clients that support one. A model in a client such as Claude Code may itself supply `confirm=true` after asking the user, so only configure rules for tools you trust that client to call. Without rules, the client approval flags stay as before.
