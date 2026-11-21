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


# ---- redirect (voice prompt): interrupt + new prompt, whitelisted paths only
class FakeProc:
    def __init__(self, pid=4242, exits=True):
        self.pid, self.exits, self.waited = pid, exits, 0

    def wait(self, timeout=None):
        self.waited += 1
        if not self.exits:
            raise r.psutil.TimeoutExpired(timeout)


CLAUDE = {"agent": "claude-code", "kind": "session", "pid": 4242, "ctime": 0, "cwd": "/tmp",
          "log": "/x/0b5c1e9a-1111-2222-3333-444455556666.jsonl"}


def test_redirect_claude_terminal_sigint_then_resume(monkeypatch):
    kills, resumed = [], []
    monkeypatch.setattr(r.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    monkeypatch.setattr(r, "_tmux_pane", lambda proc: None)
    monkeypatch.setattr(r, "_resume_claude", lambda info, prompt, agent: resumed.append(prompt) or {"ok": True, "message": "x"})
    out = r.redirect_agent(CLAUDE, "add a test", "claude-code · demo", FakeProc())
    assert kills == [(4242, r.signal.SIGINT)] and resumed == ["add a test"]
    assert out["ok"] and "interrupted" in out["message"]


def test_redirect_claude_in_tmux_types_into_pane(monkeypatch):
    calls = []
    monkeypatch.setattr(r, "_tmux_pane", lambda proc: "%3")
    monkeypatch.setattr(r.shutil, "which", lambda name: "/usr/bin/tmux" if name == "tmux" else None)
    monkeypatch.setattr(r.subprocess, "run", lambda argv, **kw: calls.append(argv))
    monkeypatch.setattr(r.os, "kill", lambda *a: (_ for _ in ()).throw(AssertionError("no kill in tmux mode")))
    monkeypatch.setattr(r.time, "sleep", lambda s: None)
    out = r.redirect_agent(CLAUDE, "rm -rf / ; echo hi", "claude-code · demo", FakeProc())
    assert out["ok"]
    assert calls[0] == ["/usr/bin/tmux", "send-keys", "-t", "%3", "Escape"]
    assert calls[1] == ["/usr/bin/tmux", "send-keys", "-t", "%3", "-l", "--", "rm -rf / ; echo hi"]  # literal text
    assert calls[2] == ["/usr/bin/tmux", "send-keys", "-t", "%3", "Enter"]


def test_redirect_claude_headless_resumes_directly(monkeypatch):
    resumed = []
    monkeypatch.setattr(r, "_resume_claude", lambda info, prompt, agent: resumed.append(prompt) or {"ok": True, "message": "x"})
    assert r.redirect_agent(CLAUDE, "go", "c", None)["ok"] and resumed == ["go"]


def test_redirect_other_agents_interrupt_only(monkeypatch):
    kills = []
    monkeypatch.setattr(r.os, "kill", lambda pid, sig: kills.append(pid))
    info = {**CLAUDE, "agent": "codex", "log": ""}
    out = r.redirect_agent(info, "go", "codex", FakeProc(pid=7))
    assert out["ok"] is False and kills == [7] and "can't take a voice prompt" in out["message"]
    assert r.redirect_agent(info, "go", "codex", None)["ok"] is False


def test_resume_claude_rejects_bad_session_id(monkeypatch):
    monkeypatch.setattr(r.shutil, "which", lambda name: "/bin/claude")
    assert r._resume_claude({**CLAUDE, "log": "/x/../../etc.jsonl"}, "go", "c")["ok"] is False
