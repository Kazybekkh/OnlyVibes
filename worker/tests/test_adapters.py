"""Adapter parsing from captured (real) and synthetic fixtures, command building, detection, run()."""
from __future__ import annotations

import asyncio
import os
import sys
import time

import pytest

from conftest import FIXTURES
import adapters
from adapters import (ClaudeCodeAdapter, CodexAdapter, CursorAdapter, VibeAdapter, describe_tool,
                      detect_agents, make_event, MAX_TEXT)


def parse_fixture(adapter, name: str, cwd: str = "/work/ws") -> list[dict]:
    adapter.cwd = cwd
    events = []
    with open(os.path.join(FIXTURES, name)) as fh:
        for line in fh:
            events += adapter.parse_line(line)
    events += adapter.finish()
    for ev in events:  # every event is protocol-shaped
        assert set(ev) <= {"ts", "kind", "text", "tool", "data"}
        assert ev["kind"] in {"system", "user", "assistant", "thinking", "tool_call", "tool_result", "error", "git"}
        assert len(ev["text"]) <= MAX_TEXT
    return events


def kinds(events):
    return [e["kind"] for e in events]


# ---------------------------------------------------------------- claude-code


def test_claude_real_stream():
    a = ClaudeCodeAdapter()
    ev = parse_fixture(a, "claude_stream.jsonl", cwd="/tmp/ov-fixture-claude")
    assert kinds(ev) == ["system", "tool_call", "tool_result", "assistant", "system"]
    assert ev[1]["text"] == "Bash: echo hi" and ev[1]["tool"] == "Bash"
    assert ev[2]["text"] == "hi" and ev[2]["tool"] == "Bash"
    assert ev[3]["text"] == "done"
    assert ev[4]["text"].startswith("Finished (success) · 2 turns")
    assert a.final_text == "done" and a.last_text() == "done"
    # input = input + cache creation + cache read from the result line
    assert a.usage == {"input": 4 + 13326 + 34522, "output": 77}
    assert not a.error_text


def test_claude_tools_thinking_errors():
    a = ClaudeCodeAdapter()
    ev = parse_fixture(a, "claude_tools.synthetic.jsonl")
    texts = [(e["kind"], e["text"]) for e in ev]
    assert ("thinking", "I should read the README first.") in texts
    assert ("tool_call", "Read README.md") in texts
    assert ("tool_call", "Edit src/app.ts") in texts
    assert ("tool_result", "error: String not found in file") in texts
    assert ("tool_call", "Bash: npm test") in texts
    assert ("tool_call", "Todo: 1/2 done · now: Add tests") in texts
    assert ("assistant", "not json at all") in texts  # unknown lines degrade to text
    assert a.final_text == "All tests pass. Summary: added the timer."
    assert a.usage == {"input": 171, "output": 102}


def test_claude_running_usage_before_result():
    a = ClaudeCodeAdapter()
    with open(os.path.join(FIXTURES, "claude_tools.synthetic.jsonl")) as fh:
        lines = fh.readlines()
    for line in lines[:4]:
        a.parse_line(line)
    assert a.usage == {"input": 10 + 100 + 5 + 50, "output": 50}


def test_claude_error_result():
    a = ClaudeCodeAdapter()
    ev = a.parse_line('{"type":"result","subtype":"error_max_turns","is_error":true,"result":"","num_turns":3}')
    assert kinds(ev) == ["system", "error"]
    assert a.error_text


def test_claude_command_uses_stdin_and_no_shell():
    cmd = ClaudeCodeAdapter().build_command("-p looks like a flag; rm -rf /", "/tmp")
    assert cmd.argv == ["claude", "-p", "--output-format", "stream-json", "--verbose",
                        "--dangerously-skip-permissions"]
    assert cmd.stdin == "-p looks like a flag; rm -rf /"


# ---------------------------------------------------------------- codex


def test_codex_real_exec():
    a = CodexAdapter()
    ev = parse_fixture(a, "codex_exec.jsonl")
    assert kinds(ev) == ["system", "tool_call", "tool_result", "assistant"]
    assert ev[1]["text"] == "Bash: echo hi"
    assert ev[2]["text"] == "hi"
    assert a.final_text == "done"
    assert a.usage == {"input": 28368, "output": 38}


def test_codex_tools_reasoning_failure():
    a = CodexAdapter()
    ev = parse_fixture(a, "codex_tools.synthetic.jsonl")
    texts = [(e["kind"], e["text"]) for e in ev]
    assert ("thinking", "**Planning** I will add a file.") in texts
    assert ("tool_call", "Bash: ls -la src") in texts
    assert any(k == "tool_result" and t.startswith("exit 2: ls: cannot access") for k, t in texts)
    assert ("tool_call", "Write src/timer.py, Edit README.md") in texts
    assert any(k == "thinking" and "[x] tests" in t for k, t in texts)
    assert ("error", "stream disconnected") in texts
    assert a.final_text == "Added src/timer.py."
    assert a.error_text == "stream disconnected"
    assert a.usage == {"input": 1000, "output": 50}


def test_codex_command():
    cmd = CodexAdapter().build_command("hello", "/tmp")
    assert cmd.argv[:2] == ["codex", "exec"]
    assert "--json" in cmd.argv and "--skip-git-repo-check" in cmd.argv
    assert cmd.argv[cmd.argv.index("--sandbox") + 1] == "danger-full-access"
    assert cmd.argv[-1] == "-" and cmd.stdin == "hello"


# ---------------------------------------------------------------- cursor / vibe


def test_cursor_stream():
    a = CursorAdapter()
    ev = parse_fixture(a, "cursor_stream.synthetic.jsonl")
    texts = [(e["kind"], e["text"]) for e in ev]
    assert texts[0] == ("system", "Cursor agent started · model gpt-5")
    assert ("thinking", "Let me look at the files.") in texts
    assert ("assistant", "I'll inspect the repo.") in texts
    assert ("tool_call", "Bash: npm test") in texts
    assert ("tool_result", "3 passing") in texts
    assert ("tool_call", "Read README.md") in texts
    assert ("tool_result", "# Title") in texts
    assert ("tool_result", "error: file is read-only") in texts
    assert any(k == "assistant" and "something_new" in t for k, t in texts)  # unknown -> text
    assert a.final_text == "Done: tests pass."


def test_cursor_command_long_prompt_goes_to_file():
    a = CursorAdapter()
    cmd = a.build_command("x" * (adapters.MAX_ARGV_PROMPT + 10), "/tmp")
    assert cmd.argv[:5] == ["cursor-agent", "-p", "--output-format", "stream-json", "--force"]
    assert cmd.cleanup and os.path.exists(cmd.cleanup[0]) and cmd.cleanup[0] in cmd.argv[-1]
    os.unlink(cmd.cleanup[0])


def test_vibe_streaming_and_plain_text():
    a = VibeAdapter()
    ev = parse_fixture(a, "vibe_streaming.synthetic.jsonl")
    texts = [(e["kind"], e["text"]) for e in ev]
    assert ("assistant", "Let me check the tests.") in texts
    assert ("tool_call", "Bash: pytest -q") in texts
    assert ("tool_result", "5 passed in 0.10s") in texts
    assert ("tool_call", "Edit app.py") in texts
    assert ("assistant", "plain text progress line from vibe") in texts
    assert not any(t == "You are Vibe." for _, t in texts)
    assert a.last_text() == "Done, all green."


def test_vibe_command_probes_help(monkeypatch):
    monkeypatch.setattr(VibeAdapter, "_help_cache", "usage: vibe [--prompt P] [--auto-approve] [--output {text,json,streaming}]")
    cmd = VibeAdapter().build_command("do it", "/tmp")
    assert cmd.argv == ["vibe", "--prompt", "do it", "--auto-approve", "--output", "streaming"]
    monkeypatch.setattr(VibeAdapter, "_help_cache", "usage: vibe [--prompt P] [--auto-approve]")
    assert VibeAdapter().build_command("x", "/tmp").argv == ["vibe", "--prompt", "x", "--auto-approve"]


# ---------------------------------------------------------------- helpers / detection


@pytest.mark.parametrize("name,args,expected", [
    ("Bash", {"command": "npm test"}, "Bash: npm test"),
    ("Edit", {"file_path": "/w/src/app.ts"}, "Edit src/app.ts"),
    ("Read", {"file_path": "/elsewhere/README.md"}, "Read /elsewhere/README.md"),
    ("Write", {"file_path": "/w/a.py", "content": "x"}, "Write a.py"),
    ("Grep", {"pattern": "TODO", "path": "/w/src"}, "Grep TODO in src"),
    ("WebFetch", {"url": "https://x.dev"}, "WebFetch https://x.dev"),
    ("Task", {"description": "review code"}, "Task: review code"),
    ("mcp__foo__bar", {"q": 1}, 'mcp__foo__bar {"q": 1}'),
    ("shell", {"command": ["bash", "-lc", "ls"]}, "Bash: bash -lc ls"),
])
def test_describe_tool(name, args, expected):
    assert describe_tool(name, args, "/w")[1] == expected


def test_event_truncation_and_data_limit():
    ev = make_event("assistant", "a" * 10000, data={"blob": "b" * 20000})
    assert len(ev["text"]) == MAX_TEXT and ev["text"].endswith("…")
    assert "data" not in ev
    assert make_event("tool_call", "x", tool="Bash", data={"a": 1})["data"] == {"a": 1}


def test_detect_agents(monkeypatch):
    monkeypatch.setattr(adapters.shutil, "which", lambda b: "/usr/bin/" + b if b in ("claude", "codex") else None)
    assert detect_agents("") == ["claude-code", "codex"]
    assert detect_agents("codex,mock,claude-code,bogus,cursor") == ["codex", "mock", "claude-code"]
    assert detect_agents("mock") == ["mock"]


# ---------------------------------------------------------------- run() with a fake CLI


def _fake_cli(tmp_path, body: str) -> str:
    path = tmp_path / "fake-cli"
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(0o755)
    return str(path)


def test_run_streams_fixture_and_reads_stdin(tmp_path):
    fixture = os.path.join(FIXTURES, "claude_stream.jsonl")
    cli = _fake_cli(tmp_path, f"""
import sys
prompt = sys.stdin.read()
open({str(tmp_path / 'prompt.txt')!r}, 'w').write(prompt)
sys.stderr.write('warming up\\n')
sys.stdout.write(open({fixture!r}).read())
""")
    a = ClaudeCodeAdapter()
    a.binary = cli
    events = []
    res = asyncio.run(a.run("build the thing", str(tmp_path), dict(os.environ), events.append))
    assert res.ok and res.exit_code == 0 and res.final_text == "done"
    assert (tmp_path / "prompt.txt").read_text() == "build the thing"
    assert ("system", "stderr: warming up") in [(e["kind"], e["text"]) for e in events]
    assert "tool_call" in kinds(events)


def test_run_nonzero_exit_reports_stderr(tmp_path):
    cli = _fake_cli(tmp_path, "import sys\nsys.stderr.write('auth failed\\n')\nsys.exit(3)\n")
    a = ClaudeCodeAdapter()
    a.binary = cli
    res = asyncio.run(a.run("x", str(tmp_path), dict(os.environ), lambda e: None))
    assert not res.ok and res.exit_code == 3 and "auth failed" in res.error


def test_cancel_kills_whole_process_group(tmp_path):
    pidfile = tmp_path / "child.pid"
    cli = _fake_cli(tmp_path, f"""
import subprocess, sys, time
child = subprocess.Popen(['sleep', '60'])
open({str(pidfile)!r}, 'w').write(str(child.pid))
print('{{"type":"system","subtype":"init","model":"m"}}', flush=True)
time.sleep(60)
""")
    a = ClaudeCodeAdapter()
    a.binary = cli

    async def go():
        events = []
        t = asyncio.create_task(a.run("x", str(tmp_path), dict(os.environ), events.append))
        for _ in range(100):
            await asyncio.sleep(0.05)
            if pidfile.exists() and events:
                break
        t.cancel()
        start = time.monotonic()
        with pytest.raises(asyncio.CancelledError):
            await t
        return time.monotonic() - start

    elapsed = asyncio.run(go())
    assert elapsed < 5
    child = int(pidfile.read_text())
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)
    assert a.proc.returncode is not None
