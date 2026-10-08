# Installing the Thunderbird add-on

The MCP server talks to Thunderbird through a small add-on (`tbmcp-bridge`). It is
built from `addon/` in this clone, so install it from here, not from another copy.

The commands below assume the clone's environment at `.venv` (see Development in the
README: `uv venv && uv pip install -e ".[dev]"`). Run them from the clone root.

## 1. Build the add-on file

```powershell
& .\.venv\Scripts\python.exe -m tbmcp install-addon --manual
```

It prints the path of the built package, e.g.
`C:\Users\<you>\AppData\Local\tbmcp\addon\tbmcp-bridge-1.3.1-5becf2b5a8a9.xpi`.
The suffix is a hash of the add-on contents, so the same name means an identical
add-on.

## 2. Install it in Thunderbird

1. Menu (☰) > **Add-ons and Themes**.
2. Gear icon > **Install Add-on From File…**
3. Pick the `.xpi` from step 1.
4. Accept the permission prompt, then restart Thunderbird.

The add-on is unsigned. Thunderbird release builds allow that
(`MOZ_REQUIRE_SIGNING=false`), so no extra setting is needed.

Installing over an existing version replaces it; no need to remove the old one first.

## 3. Check it

```powershell
& .\.venv\Scripts\python.exe -m tbmcp doctor
```

`doctor` reports the installed add-on version against the one in this clone and
warns if Thunderbird is running an older build, even if its version matches.

## When to reinstall

After any change, run `tbmcp refresh` (or `tools\refresh_live.bat`). It stops a daemon
running old Python code and reinstalls the add-on only when its build differs; a current
add-on is left alone, so Thunderbird does not restart. `doctor` and `tb_status` report
stale add-on and server code.

For unattended refresh after implementation work, add a Tickets Watcher automation rule:
trigger **todo done**, project **Thunderbird MCP**, action **command** with
`tools\refresh_live.bat`. The command restarts Thunderbird only when the add-on changed.
`tools\install_addon.bat` remains the explicit reinstall-and-restart command.
When Thunderbird is closed, `tools\refresh_live.bat` skips its live check and succeeds
without starting it.

## Automatic install

Without `--manual`, `install-addon` installs the package itself and restarts
Thunderbird once (it asks first; `--yes` skips the prompt, `--no-restart` leaves
Thunderbird closed). Pass `--profile <name or directory>` for a non-default profile.
Every open Thunderbird window is asked to close. A window that refuses, such as an
unsaved message, stops the install after a minute and is named in the error. Nothing
is closed by force.

## Portable Thunderbird

`install-addon` and `refresh` take the profile and program from the running Thunderbird.
While Thunderbird is closed, `TBMCP_PROFILE` and `TBMCP_THUNDERBIRD` select the profile
and program, first from the current process and then, on Windows, from the stored user
variables. A program started before those variables were set can still read them.
`tbmcp doctor` shows "profile chosen by" and the daemon's profile.
