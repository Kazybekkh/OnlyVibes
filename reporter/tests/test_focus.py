"""Focus whitelist: only known agent apps / terminals in the process chain get activated."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import reporter as r  # noqa: E402

GHOSTTY_CHAIN = [(10, "/Users/k/.local/bin/agent", "agent"), (9, "/bin/zsh", "zsh"),
                 (8, "/usr/bin/login", "login"), (7, "/Applications/Ghostty.app/Contents/MacOS/ghostty", "ghostty")]


def test_mac_terminal_host_found_by_walking_parents():
    assert r.find_host(GHOSTTY_CHAIN, "Darwin") == {"pid": 7, "app": "Ghostty", "bundle": "/Applications/Ghostty.app"}


def test_mac_outermost_bundle_for_helpers():
    chain = [(5, "/Applications/Cursor.app/Contents/Frameworks/Cursor Helper (Plugin).app/Contents/MacOS/x", "x")]
    assert r.find_host(chain, "Darwin")["bundle"] == "/Applications/Cursor.app"


def test_mac_skips_unknown_bundles_and_rejects_everything_else():
    py = (3, "/Applications/Xcode.app/Contents/Developer/Python.app/Contents/MacOS/Python", "Python")
    assert r.find_host([py, (2, "/bin/zsh", "zsh")], "Darwin") is None
    assert r.find_host([(4, "/Applications/Evil.app/Contents/MacOS/Evil", "Evil")], "Darwin") is None
    assert r.find_host([py, (1, "/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal", "Terminal")],
                       "Darwin")["app"] == "Terminal"


def test_windows_and_linux_whitelists():
    assert r.find_host([(4, "C:\\x\\node.exe", "node.exe"), (3, "C:\\Apps\\WindowsTerminal.exe", "WindowsTerminal.exe")],
                       "Windows")["pid"] == 3
    assert r.find_host([(4, "/usr/bin/bash", "bash"), (3, "/usr/libexec/gnome-terminal-server", "gnome-terminal-server")],
                       "Linux")["pid"] == 3
    assert r.find_host([(4, "/usr/bin/evil", "evil")], "Linux") is None


def test_focus_command_is_fixed_argv():
    assert r.focus_command({"pid": 1, "app": "X", "bundle": "/nonexistent/X.app"}, "Darwin") is None
    cmd = r.focus_command({"pid": 42, "app": "cursor.exe"}, "Windows")
    assert cmd[:3] == ["powershell", "-NoProfile", "-Command"] and cmd[3].endswith("AppActivate(42) | Out-Null")


def test_handle_control_focus_routes_and_unknown_card():
    r._CONTROL.clear()
    assert r.handle_control({"agent": "nope", "action": "focus"})["ok"] is False
    r._CONTROL["x"] = {"agent": "not-an-agent", "kind": "session", "pid": 0, "ctime": 0, "cwd": "", "log": ""}
    assert "no running process" in r.handle_control({"agent": "x", "action": "focus"})["message"]
    assert r._desktop_app_pid("not-an-agent") == 0
    r._CONTROL.clear()
