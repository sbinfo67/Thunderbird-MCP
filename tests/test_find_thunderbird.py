from __future__ import annotations

import pytest

from tbmcp import addon_install

REAL_SAVED_USER_VARIABLE = addon_install.saved_user_variable


@pytest.fixture(autouse=True)
def _without_executable_override(monkeypatch):
    monkeypatch.delenv("TBMCP_THUNDERBIRD", raising=False)
    monkeypatch.setattr(addon_install, "running_command_lines", lambda: [])
    monkeypatch.setattr(addon_install.shutil, "which", lambda _name: None)


def _running_exe(monkeypatch, tmp_path, command):
    exe = tmp_path / "ThunderbirdPortable" / "App" / "thunderbird64" / "thunderbird.exe"
    exe.parent.mkdir(parents=True)
    exe.touch()
    monkeypatch.setattr(addon_install, "running_command_lines", lambda: [command(str(exe))])
    monkeypatch.setattr(addon_install.sys, "platform", "win32")
    for key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        monkeypatch.setenv(key, str(tmp_path / key.replace("/", "_")))
    return exe


def test_saved_user_variable_is_windows_only(monkeypatch):
    monkeypatch.setattr(addon_install.sys, "platform", "linux")
    assert REAL_SAVED_USER_VARIABLE("TBMCP_PROFILE") is None


@pytest.mark.skipif(addon_install.sys.platform != "win32", reason="Windows registry only")
def test_saved_user_variable_reads_registry(monkeypatch):
    import winreg
    from contextlib import nullcontext

    monkeypatch.setattr(winreg, "OpenKey", lambda *_args: nullcontext(object()))
    monkeypatch.setattr(winreg, "QueryValueEx", lambda *_args: (r"D:\profile", winreg.REG_SZ))
    assert REAL_SAVED_USER_VARIABLE("TBMCP_PROFILE") == r"D:\profile"
    monkeypatch.setattr(
        winreg, "QueryValueEx", lambda *_args: (_ for _ in ()).throw(FileNotFoundError())
    )
    assert REAL_SAVED_USER_VARIABLE("TBMCP_PROFILE") is None


def test_finds_unquoted_portable_executable_with_spaces(monkeypatch, tmp_path):
    exe = _running_exe(monkeypatch, tmp_path, lambda path: f"{path} -profile D:\\profile")
    assert addon_install.find_thunderbird() == exe


def test_finds_quoted_executable_with_spaces(monkeypatch, tmp_path):
    exe = _running_exe(monkeypatch, tmp_path, lambda path: f'"{path}" -profile D:\\profile')
    assert addon_install.find_thunderbird() == exe


def test_skips_content_process_and_missing_executable(monkeypatch, tmp_path):
    exe = _running_exe(monkeypatch, tmp_path, lambda path: f"{path} -contentproc")
    monkeypatch.setattr(
        addon_install, "running_command_lines", lambda: [str(exe) + " -contentproc"]
    )
    assert addon_install.running_executable() is None
    monkeypatch.setattr(
        addon_install, "running_command_lines", lambda: [str(tmp_path / "thunderbird.exe")]
    )
    assert addon_install.running_executable() is None


def test_executable_override_wins_and_missing_install_returns_none(monkeypatch, tmp_path):
    override = tmp_path / "override.exe"
    override.touch()
    running = tmp_path / "thunderbird.exe"
    running.touch()
    monkeypatch.setattr(addon_install, "running_command_lines", lambda: [f'"{running}"'])
    monkeypatch.setenv("TBMCP_THUNDERBIRD", str(override))
    assert addon_install.find_thunderbird() == override
    monkeypatch.delenv("TBMCP_THUNDERBIRD")
    monkeypatch.setattr(addon_install, "running_command_lines", lambda: [])
    for key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        monkeypatch.setenv(key, str(tmp_path / key.replace("/", "_")))
    monkeypatch.setattr(addon_install.sys, "platform", "win32")
    assert addon_install.find_thunderbird() is None


def test_saved_executable_is_used_only_when_running_one_is_missing(monkeypatch, tmp_path):
    saved = tmp_path / "saved.exe"
    saved.touch()
    running = tmp_path / "thunderbird.exe"
    running.touch()
    monkeypatch.setattr(addon_install.sys, "platform", "win32")
    monkeypatch.setattr(addon_install, "saved_user_variable", lambda _name: str(saved))
    monkeypatch.setattr(addon_install, "running_executable", lambda: None)
    assert addon_install.find_thunderbird() == saved
    monkeypatch.setattr(addon_install, "running_executable", lambda: running)
    assert addon_install.find_thunderbird() == running
    saved.unlink()
    monkeypatch.setattr(addon_install, "running_executable", lambda: None)
    for key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        monkeypatch.setenv(key, str(tmp_path / key))
    assert addon_install.find_thunderbird() is None
