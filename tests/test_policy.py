from pathlib import Path

import pytest

from tbmcp.policy import FolderRule, allows, grants, load_rules


def test_load_rules(tmp_path: Path):
    assert load_rules(tmp_path / "missing.json") == ()
    path = tmp_path / "settings.json"
    path.write_text(
        '{"folders": [{"path": "@BKToDo/", "account": "account1", "allow": ["move_in"]}]}'
    )
    assert load_rules(path) == (FolderRule("/@BKToDo", frozenset({"move_in"}), "account1"),)
    path.write_text(" \n")
    assert load_rules(path) == ()

    for bad, message in (
        ('{"folders": [{"path": "/x", "allow": ["typo"]}]}', "typo"),
        ('{"folders": [{"allow": ["move_in"]}]}', "path"),
        ("{", "settings.json"),
        ('{"folders": [{"path": " ", "allow": []}]}', "path"),
        ('{"folders": [{"path": "/x", "allow": "move_in"}]}', "allow"),
        ('{"folders": [{"path": "/x", "account": " ", "allow": []}]}', "account"),
        ("[]", "expected a JSON object"),
    ):
        path.write_text(bad)
        with pytest.raises(SystemExit, match=message):
            load_rules(path)


def test_example_settings_are_valid():
    path = Path(__file__).resolve().parents[1] / "settings.example.json"
    assert load_rules(path) == (
        FolderRule(
            "/@BKToDo",
            frozenset(
                {
                    "create_subfolders",
                    "rename_subfolders",
                    "delete_subfolders",
                    "move_in",
                    "move_out",
                }
            ),
        ),
    )


def test_default_config_path_is_repo_settings(monkeypatch):
    from tbmcp.policy import default_config_path

    monkeypatch.delenv("TBMCP_CONFIG", raising=False)
    repo = Path(__file__).resolve().parents[1]
    assert default_config_path() == repo / "settings.json"
    custom = repo / "custom.json"
    monkeypatch.setenv("TBMCP_CONFIG", str(custom))
    assert default_config_path() == custom


def test_allows_uses_segments_and_account():
    rules = (FolderRule("/@BKToDo", frozenset({"move_in"}), "account1"),)
    assert grants(rules, "move_in")
    assert allows(rules, "move_in", {"path": "/@BKToDo", "accountId": "account1"})
    assert allows(rules, "move_in", {"path": "/@BKToDo/sub", "accountId": "account1"})
    assert not allows(rules, "move_in", {"path": "/@BKToDoOld", "accountId": "account1"})
    assert not allows(
        rules, "move_in", {"path": "/@BKToDo", "accountId": "account1"}, strictly_below=True
    )
    assert not allows(rules, "move_in", {"path": "/@BKToDo", "accountId": "account2"})
    assert not allows(rules, "move_in", {"accountId": "account1"})
    assert not allows(rules, "move_out", {"path": "/@BKToDo", "accountId": "account1"})
    assert allows((FolderRule("/", frozenset({"move_in"})),), "move_in", {"path": "/Inbox"})
