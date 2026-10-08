import os
from pathlib import Path

from tbmcp import ipc


def test_newest_source_mtime_returns_latest_readable_file(monkeypatch, tmp_path):
    older = tmp_path / "old.py"
    newer = tmp_path / "new.py"
    older.touch()
    newer.touch()
    os.utime(older, (1, 1))
    os.utime(newer, (2, 2))
    monkeypatch.setattr(ipc, "__file__", str(tmp_path / "ipc.py"))
    monkeypatch.setattr(Path, "rglob", lambda _self, _pattern: [older, newer])
    assert ipc.newest_source_mtime() == 2


def test_newest_source_mtime_returns_zero_when_no_sources_are_readable(monkeypatch, tmp_path):
    monkeypatch.setattr(ipc, "__file__", str(tmp_path / "ipc.py"))
    monkeypatch.setattr(Path, "rglob", lambda _self, _pattern: [tmp_path / "missing.py"])
    assert ipc.newest_source_mtime() == 0.0
